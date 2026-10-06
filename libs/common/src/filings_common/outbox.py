"""Transactional outbox: write an event in the same transaction as the change it announces.

A crash can then never leave a change without its event, or an event without its change. The
relay publishes outbox rows to Redpanda afterwards, in id order.

Delivery is at least once: if the relay crashes after publishing but before marking rows
published, those rows are published again. Consumers are idempotent on the accession number, so a
repeat is harmless. Run a single relay replica to keep publish order; `SKIP LOCKED` only guarantees
that two relays never publish the same row concurrently.
"""

import json
import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from filings_common.db import OutboxEvent
from filings_common.events import Event

logger = logging.getLogger(__name__)

ERROR_BACKOFF_SECONDS = 5.0


@dataclass(frozen=True)
class OutboundMessage:
    topic: str
    key: str
    value: bytes


class Publisher(Protocol):
    def publish(self, messages: Sequence[OutboundMessage]) -> None:
        """Publish every message, blocking until all are acknowledged. Raise on any failure."""
        ...


def enqueue(session: Session, topic: str, event: Event) -> OutboxEvent:
    """Add an event to the outbox as part of the session's current transaction."""
    row = OutboxEvent(topic=topic, key=event.key, payload=event.model_dump(mode="json"))
    session.add(row)
    return row


def encode_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def relay_batch(
    sessions: sessionmaker[Session], publisher: Publisher, batch_size: int = 100
) -> int:
    """Publish the oldest unpublished rows, then mark them published. Returns how many."""
    with sessions.begin() as session:
        rows = session.scalars(
            sa.select(OutboxEvent)
            .where(OutboxEvent.published_at.is_(None))
            .order_by(OutboxEvent.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        ).all()
        if not rows:
            return 0
        publisher.publish(
            [OutboundMessage(r.topic, r.key, encode_payload(r.payload)) for r in rows]
        )
        session.execute(
            sa.update(OutboxEvent)
            .where(OutboxEvent.id.in_([r.id for r in rows]))
            .values(published_at=sa.func.now())
        )
    return len(rows)


def run_relay(
    sessions: sessionmaker[Session],
    publisher: Publisher,
    stop: threading.Event,
    *,
    batch_size: int = 100,
    idle_seconds: float = 1.0,
) -> None:
    """Relay until `stop` is set. Failed batches stay unpublished and are retried after a pause."""
    while not stop.is_set():
        try:
            published = relay_batch(sessions, publisher, batch_size)
        except Exception:
            logger.exception("outbox relay batch failed; retrying in %.0fs", ERROR_BACKOFF_SECONDS)
            stop.wait(ERROR_BACKOFF_SECONDS)
            continue
        if published:
            logger.info("published %d outbox events", published)
        else:
            stop.wait(idle_seconds)
