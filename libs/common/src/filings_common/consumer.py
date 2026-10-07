"""The consume loop shared by the pipeline's consumers (parser, summarizer, notifier).

Offsets are committed manually, only after a message is settled: handled successfully, or sent to
the topic's dead-letter topic (`<topic>.dlq`). A crash mid-message therefore means redelivery, not
loss, so handlers must be idempotent on the accession number.

Failures are retried with exponential backoff. After `max_attempts`, or at once for a
`PermanentError`, the message goes to the DLQ with headers saying where it came from and why it
failed, and the consumer moves on. The message keeps its trace headers there.

Each message is handled in a CONSUMER span that continues the trace in its headers, so whatever
the handler does, its outbox writes included, belongs to the filing's trace. The span records
failed attempts by exception type only: messages can quote private data, and the log lines (which
carry the trace ID) have the details.
"""

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal, Protocol

from confluent_kafka import Consumer, Message
from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, StatusCode

from filings_common import telemetry
from filings_common.outbox import OutboundMessage, Publisher

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class PermanentError(Exception):
    """Retrying cannot help (e.g. a malformed event): dead-letter the message immediately."""


@dataclass(frozen=True)
class Received:
    topic: str
    partition: int
    offset: int
    key: str | None
    value: bytes
    # Including the trace context (traceparent, tracestate) set by the relay.
    headers: tuple[tuple[str, bytes], ...] = ()


Handler = Callable[[Received], None]


class MessageSource(Protocol):
    """The parts of confluent_kafka.Consumer the loop uses (tests substitute a fake)."""

    def subscribe(self, topics: list[str]) -> None: ...

    def poll(self, timeout: float = -1) -> Message | None: ...

    def commit(self, *, message: Message, asynchronous: Literal[False]) -> object: ...

    def close(self) -> None: ...


def make_consumer(
    bootstrap_servers: str, group_id: str, *, max_poll_interval_seconds: int = 300
) -> Consumer:
    """A consumer that commits manually. Handling one message, retries included, must finish
    within `max_poll_interval_seconds`, or the broker assumes the consumer died and rebalances."""
    return Consumer(
        {
            "bootstrap.servers": bootstrap_servers,
            "group.id": group_id,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
            "max.poll.interval.ms": max_poll_interval_seconds * 1000,
        }
    )


def run_consumer(
    source: MessageSource,
    topic: str,
    handler: Handler,
    dead_letters: Publisher,
    stop: threading.Event,
    *,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
    poll_timeout: float = 1.0,
    consumer_group: str | None = None,
) -> None:
    """Consume `topic` until `stop` is set. Closes the source on exit.

    `consumer_group` only labels the spans; the source already belongs to its group.
    """
    source.subscribe([topic])
    try:
        while not stop.is_set():
            message = source.poll(poll_timeout)
            if message is None:
                continue
            error = message.error()
            if error is not None:
                logger.warning("consumer error on %s: %s", topic, error)
                continue
            received = Received(
                topic=message.topic() or topic,
                partition=message.partition() or 0,
                offset=message.offset() or 0,
                key=(message.key() or b"").decode() or None,
                value=message.value() or b"",
                headers=_headers(message),
            )
            with _process_span(received, consumer_group) as span:
                settled = _settle(
                    received, handler, dead_letters, stop, max_attempts, backoff_seconds, span
                )
                if settled:
                    source.commit(message=message, asynchronous=False)
    finally:
        source.close()


def _headers(message: Message) -> tuple[tuple[str, bytes], ...]:
    """The message's headers. A consumer returns them as a list of (key, bytes or None); the
    client's type also allows a dict and str values, which are normalized the same way."""
    raw = message.headers() or []
    pairs = raw.items() if isinstance(raw, dict) else raw
    return tuple((k, v.encode() if isinstance(v, str) else v or b"") for k, v in pairs)


@contextmanager
def _process_span(received: Received, consumer_group: str | None) -> Iterator[Span]:
    """The span the message is handled in: a child of the relay's publish span."""
    attributes: dict[str, str | int] = {
        telemetry.MESSAGING_SYSTEM: "kafka",
        telemetry.MESSAGING_OPERATION: "process",
        telemetry.MESSAGING_DESTINATION: received.topic,
        telemetry.MESSAGING_PARTITION: str(received.partition),
        telemetry.MESSAGING_OFFSET: received.offset,
    }
    if consumer_group is not None:
        attributes[telemetry.MESSAGING_CONSUMER_GROUP] = consumer_group
    if received.key is not None:
        attributes[telemetry.MESSAGING_KEY] = received.key
    with telemetry.redacted_span(
        tracer,
        f"process {received.topic}",
        context=telemetry.context_from_headers(received.headers),
        kind=SpanKind.CONSUMER,
        attributes=attributes,
    ) as span:
        yield span


def _settle(
    received: Received,
    handler: Handler,
    dead_letters: Publisher,
    stop: threading.Event,
    max_attempts: int,
    backoff_seconds: float,
    span: Span,
) -> bool:
    """Handle or dead-letter the message. False if stopped first (it will be redelivered)."""
    for attempt in range(1, max_attempts + 1):
        span.set_attribute("filings.attempts", attempt)
        try:
            handler(received)
            span.set_attribute(telemetry.OUTCOME, "handled")
            return True
        except Exception as exc:
            telemetry.record_exception(
                span, exc, redact=True, attributes={"filings.attempt": attempt}
            )
            if isinstance(exc, PermanentError) or attempt == max_attempts:
                _dead_letter(received, exc, attempt, dead_letters, span)
                return True
            delay = backoff_seconds * 2 ** (attempt - 1)
            logger.warning(
                "handling %s@%d failed (attempt %d/%d): %r; retrying in %.1fs",
                received.topic,
                received.offset,
                attempt,
                max_attempts,
                exc,
                delay,
            )
            if stop.wait(delay):
                span.set_attribute(telemetry.OUTCOME, "stopped")
                return False
    return True


def _dead_letter(
    received: Received, exc: Exception, attempts: int, dead_letters: Publisher, span: Span
) -> None:
    logger.error(
        "dead-lettering %s@%d after %d attempt(s): %r",
        received.topic,
        received.offset,
        attempts,
        exc,
    )
    source = f"{received.topic}/{received.partition}/{received.offset}"
    dead_letters.publish(
        [
            OutboundMessage(
                topic=f"{received.topic}.dlq",
                key=received.key or "",
                value=received.value,
                # The original headers come along, so the dead letter still names its trace.
                headers=(
                    *received.headers,
                    ("dlq.source", source.encode()),
                    ("dlq.attempts", str(attempts).encode()),
                    ("dlq.error", repr(exc)[:1000].encode()),
                ),
            )
        ]
    )
    span.set_attribute(telemetry.OUTCOME, "dead_lettered")
    span.set_status(StatusCode.ERROR, telemetry.exception_type(exc))
