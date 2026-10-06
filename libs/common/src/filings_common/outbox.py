"""Transactional outbox: write an event in the same transaction as the change it announces.

A crash can then never leave a change without its event, or an event without its change. The
outbox relay publishes the rows to Redpanda afterwards.
"""

from sqlalchemy.orm import Session

from filings_common.db import OutboxEvent
from filings_common.events import Event


def enqueue(session: Session, topic: str, event: Event) -> OutboxEvent:
    """Add an event to the outbox as part of the session's current transaction."""
    row = OutboxEvent(topic=topic, key=event.key, payload=event.model_dump(mode="json"))
    session.add(row)
    return row
