import json
import threading
from collections.abc import Sequence
from datetime import date

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from filings_common.db import OutboxEvent, session_factory
from filings_common.events import FilingDiscovered, Topic
from filings_common.outbox import OutboundMessage, enqueue, relay_batch, run_relay

pytestmark = pytest.mark.integration


class RecordingPublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.batches: list[list[OutboundMessage]] = []
        self.fail = fail

    def publish(self, messages: Sequence[OutboundMessage]) -> None:
        if self.fail:
            raise RuntimeError("broker unavailable")
        self.batches.append(list(messages))


def add_events(engine: Engine, count: int) -> list[str]:
    accessions = [f"0000320193-26-{n:06d}" for n in range(1, count + 1)]
    with session_factory(engine).begin() as session:
        for accession_no in accessions:
            event = FilingDiscovered(
                accession_no=accession_no,
                cik=320193,
                form_type="8-K",
                filed_at=date(2026, 9, 30),
                raw_object_key=f"filings/320193/{accession_no}/doc.htm",
            )
            enqueue(session, Topic.FILINGS_DISCOVERED, event)
    return accessions


def unpublished(engine: Engine) -> int:
    with engine.connect() as conn:
        query = sa.select(sa.func.count()).where(OutboxEvent.published_at.is_(None))
        return conn.execute(query).scalar_one()


def test_publishes_in_order_then_marks_published(clean_db: Engine) -> None:
    accessions = add_events(clean_db, 3)
    publisher = RecordingPublisher()
    sessions = session_factory(clean_db)

    assert relay_batch(sessions, publisher, batch_size=2) == 2
    assert relay_batch(sessions, publisher, batch_size=2) == 1
    assert relay_batch(sessions, publisher, batch_size=2) == 0

    messages = [m for batch in publisher.batches for m in batch]
    assert [m.key for m in messages] == accessions
    assert {m.topic for m in messages} == {"filings.discovered"}
    assert json.loads(messages[0].value)["accession_no"] == accessions[0]
    assert unpublished(clean_db) == 0


def test_failed_publish_leaves_rows_for_retry(clean_db: Engine) -> None:
    add_events(clean_db, 2)
    with pytest.raises(RuntimeError, match="broker unavailable"):
        relay_batch(session_factory(clean_db), RecordingPublisher(fail=True))
    assert unpublished(clean_db) == 2


def test_rows_locked_by_another_relay_are_skipped(clean_db: Engine) -> None:
    accessions = add_events(clean_db, 3)
    publisher = RecordingPublisher()
    with clean_db.connect() as other_relay, other_relay.begin():
        # Another relay holds the oldest row mid-batch.
        oldest = sa.select(OutboxEvent.id).order_by(OutboxEvent.id).limit(1).with_for_update()
        other_relay.execute(oldest)
        assert relay_batch(session_factory(clean_db), publisher) == 2
    assert [m.key for m in publisher.batches[0]] == accessions[1:]


def test_run_relay_drains_the_outbox_until_stopped(clean_db: Engine) -> None:
    add_events(clean_db, 5)
    publisher = RecordingPublisher()
    stop = threading.Event()
    worker = threading.Thread(
        target=run_relay,
        args=(session_factory(clean_db), publisher, stop),
        kwargs={"batch_size": 2, "idle_seconds": 0.05},
    )
    worker.start()
    try:
        for _ in range(100):
            if unpublished(clean_db) == 0:
                break
            stop.wait(0.05)
    finally:
        stop.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert [len(b) for b in publisher.batches] == [2, 2, 1]
