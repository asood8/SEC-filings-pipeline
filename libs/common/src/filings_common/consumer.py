"""The consume loop shared by the pipeline's consumers (parser, summarizer, notifier).

Offsets are committed manually, only after a message is settled: handled successfully, or sent to
the topic's dead-letter topic (`<topic>.dlq`). A crash mid-message therefore means redelivery, not
loss, so handlers must be idempotent on the accession number.

Failures are retried with exponential backoff. After `max_attempts`, or at once for a
`PermanentError`, the message goes to the DLQ with headers saying where it came from and why it
failed, and the consumer moves on.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from confluent_kafka import Consumer, Message

from filings_common.outbox import OutboundMessage, Publisher

logger = logging.getLogger(__name__)


class PermanentError(Exception):
    """Retrying cannot help (e.g. a malformed event): dead-letter the message immediately."""


@dataclass(frozen=True)
class Received:
    topic: str
    partition: int
    offset: int
    key: str | None
    value: bytes


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
) -> None:
    """Consume `topic` until `stop` is set. Closes the source on exit."""
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
            )
            if _settle(received, handler, dead_letters, stop, max_attempts, backoff_seconds):
                source.commit(message=message, asynchronous=False)
    finally:
        source.close()


def _settle(
    received: Received,
    handler: Handler,
    dead_letters: Publisher,
    stop: threading.Event,
    max_attempts: int,
    backoff_seconds: float,
) -> bool:
    """Handle or dead-letter the message. False if stopped first (it will be redelivered)."""
    for attempt in range(1, max_attempts + 1):
        try:
            handler(received)
            return True
        except PermanentError as exc:
            _dead_letter(received, exc, attempt, dead_letters)
            return True
        except Exception as exc:
            if attempt == max_attempts:
                _dead_letter(received, exc, attempt, dead_letters)
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
                return False
    return True


def _dead_letter(
    received: Received, exc: Exception, attempts: int, dead_letters: Publisher
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
                headers=(
                    ("dlq.source", source.encode()),
                    ("dlq.attempts", str(attempts).encode()),
                    ("dlq.error", repr(exc)[:1000].encode()),
                ),
            )
        ]
    )
