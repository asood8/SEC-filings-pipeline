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

Between messages, at most every `offsets_interval` seconds, the loop also measures the group's lag
on its partitions and how many messages its dead-letter topic holds (`OffsetMonitor`).

Given a `Heartbeat`, the loop beats on every pass, and before each attempt at a message promises
to beat again within `attempt_seconds`, so the liveness probe tells a long attempt from a hung
loop (see `health`).
"""

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from confluent_kafka import Consumer, KafkaException, Message, TopicPartition
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, StatusCode

from filings_common import metrics, telemetry
from filings_common.health import Heartbeat
from filings_common.outbox import OutboundMessage, Publisher

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# librdkafka's default max.poll.interval.ms, in seconds: how long handling one message may take.
DEFAULT_MAX_POLL_INTERVAL_SECONDS = 300


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

    # For OffsetMonitor:

    def assignment(self) -> list[TopicPartition]: ...

    def position(self, partitions: list[TopicPartition]) -> list[TopicPartition]: ...

    def committed(
        self, partitions: list[TopicPartition], timeout: float = -1
    ) -> list[TopicPartition]: ...

    def get_watermark_offsets(
        self, partition: TopicPartition, timeout: float = -1, cached: bool = False
    ) -> tuple[int, int]: ...

    def list_topics(self, topic: str | None = None, timeout: float = -1) -> Any: ...


def make_consumer(
    bootstrap_servers: str,
    group_id: str,
    *,
    max_poll_interval_seconds: int = DEFAULT_MAX_POLL_INTERVAL_SECONDS,
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
    offsets_interval: float = 30.0,
    heartbeat: Heartbeat | None = None,
    attempt_seconds: float = DEFAULT_MAX_POLL_INTERVAL_SECONDS,
) -> None:
    """Consume `topic` until `stop` is set. Closes the source on exit.

    `consumer_group` labels the spans and metrics (the source already belongs to its group); lag
    is measured only when it is given. `attempt_seconds` is the longest one attempt at handling a
    message may legitimately take; the heartbeat allows that much before each attempt. By default
    it is the poll interval, past which the broker gives the partition away anyway.
    """
    source.subscribe([topic])
    monitor = OffsetMonitor(source, topic, consumer_group, interval=offsets_interval)
    metrics.DLQ_MESSAGES.add(0, metrics.topic(dead_letter_topic(topic)))
    try:
        while not stop.is_set():
            if heartbeat is not None:
                heartbeat.beat()
            monitor.measure_if_due()
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
                    received,
                    handler,
                    dead_letters,
                    stop,
                    max_attempts,
                    backoff_seconds,
                    span,
                    heartbeat,
                    attempt_seconds,
                )
                if settled:
                    _commit(source, message)
    finally:
        monitor.close()
        source.close()


def _commit(source: MessageSource, message: Message) -> None:
    """Commit the message's offset. A failure (e.g. the group took the partition away after a
    rebalance) is logged rather than fatal: the message is redelivered, which is harmless because
    every handler is idempotent, and the loop rejoins the group on its next poll."""
    try:
        source.commit(message=message, asynchronous=False)
    except KafkaException as exc:
        logger.warning(
            "could not commit %s[%s]@%s (%s); it will be redelivered",
            message.topic(),
            message.partition(),
            message.offset(),
            exc,
        )


def _metrics_exported() -> bool:
    """True when an SDK meter provider is installed (telemetry is on, or a test installed an
    in-memory reader). The API's default provider drops every measurement, so measuring offsets
    then would only cost broker round trips."""
    provider = otel_metrics.get_meter_provider()
    return type(provider).__module__.startswith("opentelemetry.sdk")


def dead_letter_topic(topic: str) -> str:
    return f"{topic}.dlq"


class OffsetMonitor:
    """Measures, at most every `interval` seconds, the consumer group's lag on the partitions
    assigned to this consumer and the depth of the topic's dead-letter topic, for the
    `filings.consumer.lag` and `filings.dlq.depth` gauges.

    It runs between messages, so while a handler works (an LLM call, retries) the gauges keep
    their last values. Measuring takes a few small broker requests; when they fail, the gauges
    keep their last values too.
    """

    def __init__(
        self,
        source: MessageSource,
        topic: str,
        consumer_group: str | None,
        *,
        interval: float,
        timeout: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source = source
        self._topic = topic
        self._group = consumer_group
        self._interval = interval
        self._timeout = timeout
        self._clock = clock
        self._due = clock()

    def measure_if_due(self) -> None:
        now = self._clock()
        if now < self._due or not _metrics_exported():
            return
        self._due = now + self._interval
        dlq = dead_letter_topic(self._topic)
        try:
            lag = consumer_lag(self._source, timeout=self._timeout) if self._group else {}
            depth = topic_depth(self._source, dlq, timeout=self._timeout)
        except KafkaException as exc:
            logger.warning("could not measure the offsets of %s: %s", self._topic, exc)
            return
        if self._group:
            metrics.CONSUMER_LAG_VALUES.replace(
                self,
                [
                    (messages, self._partition_attributes(self._group, name, partition))
                    for (name, partition), messages in sorted(lag.items())
                ],
            )
        if depth is not None:
            metrics.DLQ_DEPTH_VALUES.replace(self, [(depth, metrics.topic(dlq))])

    @staticmethod
    def _partition_attributes(group: str, topic: str, partition: int) -> dict[str, str]:
        return {
            telemetry.MESSAGING_CONSUMER_GROUP: group,
            telemetry.MESSAGING_DESTINATION: topic,
            telemetry.MESSAGING_PARTITION: str(partition),
        }

    def close(self) -> None:
        metrics.CONSUMER_LAG_VALUES.remove(self)
        metrics.DLQ_DEPTH_VALUES.remove(self)


def consumer_lag(source: MessageSource, *, timeout: float) -> dict[tuple[str, int], int]:
    """Messages not yet consumed, per assigned (topic, partition): the high watermark minus the
    consumer's position (the offset after the last message it received).

    Partitions it hasn't received from since they were assigned count from the group's committed
    offset, or from the oldest message when the group has never committed (it then starts there).
    """
    assigned = source.assignment()
    if not assigned:
        return {}
    positions = source.position(assigned)
    unknown = [tp for tp in positions if tp.offset < 0]
    committed = {
        (tp.topic, tp.partition): tp.offset
        for tp in (source.committed(unknown, timeout=timeout) if unknown else [])
    }
    lag = {}
    for tp in positions:
        low, high = source.get_watermark_offsets(tp, timeout=timeout)
        start = tp.offset if tp.offset >= 0 else committed.get((tp.topic, tp.partition), -1)
        lag[(tp.topic, tp.partition)] = max(0, high - (start if start >= 0 else low))
    return lag


def topic_depth(source: MessageSource, topic: str, *, timeout: float) -> int | None:
    """How many messages `topic` holds (over all its partitions), or None if it doesn't exist."""
    found = source.list_topics(topic, timeout=timeout).topics.get(topic)
    if found is None or found.error is not None:
        return None
    depth = 0
    for partition in found.partitions:
        low, high = source.get_watermark_offsets(TopicPartition(topic, partition), timeout=timeout)
        depth += high - low
    return depth


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
    heartbeat: Heartbeat | None,
    attempt_seconds: float,
) -> bool:
    """Handle or dead-letter the message. False if stopped first (it will be redelivered)."""
    for attempt in range(1, max_attempts + 1):
        span.set_attribute("filings.attempts", attempt)
        if heartbeat is not None:
            heartbeat.beat(busy_for=attempt_seconds)
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
            if heartbeat is not None:
                heartbeat.beat(busy_for=delay)
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
    dlq = dead_letter_topic(received.topic)
    dead_letters.publish(
        [
            OutboundMessage(
                topic=dlq,
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
    metrics.DLQ_MESSAGES.add(1, metrics.topic(dlq))
    span.set_attribute(telemetry.OUTCOME, "dead_lettered")
    span.set_status(StatusCode.ERROR, telemetry.exception_type(exc))
