import json
import threading
from collections.abc import Sequence
from datetime import date
from typing import Any

import pytest
import sqlalchemy as sa
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from sqlalchemy.engine import Engine

from filings_common.db import OutboxEvent, session_factory
from filings_common.events import FilingDiscovered, FilingReady, Topic
from filings_common.outbox import OutboundMessage, enqueue, relay_batch, run_relay
from filings_common.telemetry import MESSAGING_DESTINATION as DESTINATION
from filings_common.telemetry import context_from_headers

pytestmark = pytest.mark.integration

tracer = trace.get_tracer(__name__)


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


def test_published_rows_are_counted_by_topic(clean_db: Engine, measured: Any) -> None:
    add_events(clean_db, 3)
    with session_factory(clean_db).begin() as session:
        ready = FilingReady(
            accession_no="0000320193-26-000009", cik=320193, form_type="8-K", summary_id=1
        )
        enqueue(session, Topic.FILINGS_READY, ready)
    relay_batch(session_factory(clean_db), RecordingPublisher())

    assert measured.total("filings.outbox.published", {DESTINATION: "filings.discovered"}) == 3
    assert measured.total("filings.outbox.published", {DESTINATION: "filings.ready"}) == 1
    # By topic only: never by accession number.
    assert measured.attribute_sets("filings.outbox.published") == [
        {DESTINATION: "filings.discovered"},
        {DESTINATION: "filings.ready"},
    ]


def test_failed_publish_leaves_rows_for_retry(clean_db: Engine, measured: Any) -> None:
    add_events(clean_db, 2)
    with pytest.raises(RuntimeError, match="broker unavailable"):
        relay_batch(session_factory(clean_db), RecordingPublisher(fail=True))
    assert unpublished(clean_db) == 2
    assert measured.total("filings.outbox.published") == 0


def test_rows_locked_by_another_relay_are_skipped(clean_db: Engine) -> None:
    accessions = add_events(clean_db, 3)
    publisher = RecordingPublisher()
    with clean_db.connect() as other_relay, other_relay.begin():
        # Another relay holds the oldest row mid-batch.
        oldest = sa.select(OutboxEvent.id).order_by(OutboxEvent.id).limit(1).with_for_update()
        other_relay.execute(oldest)
        assert relay_batch(session_factory(clean_db), publisher) == 2
    assert [m.key for m in publisher.batches[0]] == accessions[1:]


def test_run_relay_drains_the_outbox_until_stopped(clean_db: Engine, measured: Any) -> None:
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
    # Every topic's series exists from the start, at zero until something is published to it.
    assert measured.attribute_sets("filings.outbox.published") == [
        {DESTINATION: topic.value} for topic in sorted(Topic)
    ]
    assert measured.total("filings.outbox.published", {DESTINATION: "filings.discovered"}) == 5
    assert measured.total("filings.outbox.published", {DESTINATION: "filings.ready"}) == 0


def stored_trace_contexts(engine: Engine) -> list[dict[str, str] | None]:
    with engine.connect() as conn:
        query = sa.select(OutboxEvent.trace_context).order_by(OutboxEvent.id)
        return list(conn.execute(query).scalars())


def test_relay_continues_the_trace_that_wrote_each_event(
    clean_db: Engine, spans: InMemorySpanExporter
) -> None:
    with tracer.start_as_current_span("record filing") as record:
        [accession_no] = add_events(clean_db, 1)
    writer = record.get_span_context()
    [stored] = stored_trace_contexts(clean_db)
    assert stored is not None
    assert stored["traceparent"].split("-")[1:3] == [
        trace.format_trace_id(writer.trace_id),
        trace.format_span_id(writer.span_id),
    ]

    publisher = RecordingPublisher()
    relay_batch(session_factory(clean_db), publisher)
    [send] = [s for s in spans.get_finished_spans() if s.name == "send filings.discovered"]
    assert send.kind is SpanKind.PRODUCER
    assert send.parent is not None
    assert send.parent.span_id == writer.span_id
    assert send.context.trace_id == writer.trace_id
    attributes = send.attributes or {}
    assert attributes["messaging.destination.name"] == "filings.discovered"
    assert attributes["messaging.kafka.message.key"] == accession_no

    # The message carries the publish span's context: consumers continue from it.
    [[message]] = publisher.batches
    received = trace.get_current_span(context_from_headers(message.headers)).get_span_context()
    assert (received.trace_id, received.span_id) == (writer.trace_id, send.context.span_id)


def test_events_written_outside_a_trace_start_one_at_the_relay(
    clean_db: Engine, spans: InMemorySpanExporter
) -> None:
    add_events(clean_db, 2)  # e.g. by a script with tracing off
    assert stored_trace_contexts(clean_db) == [None, None]
    relay_batch(session_factory(clean_db), RecordingPublisher())
    sends = spans.get_finished_spans()
    assert [s.parent for s in sends] == [None, None]
    assert len({s.context.trace_id for s in sends}) == 2  # one trace per filing, not per batch


def test_failed_publish_marks_the_publish_spans_failed(
    clean_db: Engine, spans: InMemorySpanExporter
) -> None:
    add_events(clean_db, 1)
    with pytest.raises(RuntimeError):
        relay_batch(session_factory(clean_db), RecordingPublisher(fail=True))
    [send] = spans.get_finished_spans()
    assert send.status.status_code is StatusCode.ERROR
    assert send.status.description == "RuntimeError: broker unavailable"
