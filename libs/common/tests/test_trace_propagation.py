"""One trace across two pipeline stages, through the outbox and a real Redpanda broker."""

import threading
import uuid
from datetime import date

import pytest
from confluent_kafka import Consumer, TopicPartition
from confluent_kafka.admin import AdminClient
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from sqlalchemy.engine import Engine

from filings_common.consumer import Received, make_consumer, run_consumer
from filings_common.db import session_factory
from filings_common.events import FilingDiscovered, FilingParsed
from filings_common.kafka import KafkaPublisher, ensure_topics
from filings_common.outbox import enqueue, relay_batch
from filings_common.telemetry import context_from_headers

pytestmark = pytest.mark.integration

tracer = trace.get_tracer(__name__)

ACCESSION = "0000320193-26-000101"


def named(spans: InMemorySpanExporter, name: str) -> ReadableSpan:
    [span] = [s for s in spans.get_finished_spans() if s.name == name]
    return span


def parent_of(span: ReadableSpan) -> int | None:
    return span.parent.span_id if span.parent is not None else None


def test_one_trace_follows_a_filing_through_outbox_kafka_and_the_next_stage(
    clean_db: Engine, kafka_bootstrap: str, spans: InMemorySpanExporter
) -> None:
    run = uuid.uuid4().hex[:8]
    discovered, parsed = f"test.{run}.discovered", f"test.{run}.parsed"
    topics = [discovered, parsed, f"{discovered}.dlq"]
    ensure_topics(kafka_bootstrap, topics, partitions=1, replication_factor=1)
    publisher = KafkaPublisher(kafka_bootstrap)
    sessions = session_factory(clean_db)
    try:
        # Stage 1 (discovery) records the filing and its event in one transaction, in a span.
        with tracer.start_as_current_span("discover filing"), sessions.begin() as session:
            event = FilingDiscovered(
                accession_no=ACCESSION,
                cik=320193,
                form_type="8-K",
                filed_at=date(2026, 9, 30),
                raw_object_key=f"filings/320193/{ACCESSION}/doc.htm",
            )
            enqueue(session, discovered, event)
        assert relay_batch(sessions, publisher) == 1

        # Stage 2 (a parser) consumes it with the real loop; its handler announces the next event.
        stop = threading.Event()

        def handler(received: Received) -> None:
            with sessions.begin() as session:
                event = FilingParsed(
                    accession_no=ACCESSION, cik=320193, form_type="8-K", parser_version=1
                )
                enqueue(session, parsed, event)
            stop.set()

        group = f"test-{run}"
        worker = threading.Thread(
            target=run_consumer,
            args=(make_consumer(kafka_bootstrap, group), discovered, handler, publisher, stop),
            kwargs={"consumer_group": group},
        )
        worker.start()
        worker.join(timeout=30)
        assert not worker.is_alive()
        assert relay_batch(sessions, publisher) == 1

        checker = Consumer({"bootstrap.servers": kafka_bootstrap, "group.id": f"check-{run}"})
        try:
            checker.assign([TopicPartition(parsed, 0, 0)])
            next_message = checker.poll(10)
            assert next_message is not None
            raw_headers = next_message.headers()
            assert isinstance(raw_headers, list)  # what a consumer returns
            next_headers = [(k, v) for k, v in raw_headers if isinstance(v, bytes)]
        finally:
            checker.close()
    finally:
        publisher.close()
        admin = AdminClient({"bootstrap.servers": kafka_bootstrap})
        for future in admin.delete_topics(topics).values():
            future.result()

    root = named(spans, "discover filing")
    send_discovered = named(spans, f"send {discovered}")
    process = named(spans, f"process {discovered}")
    send_parsed = named(spans, f"send {parsed}")

    # One trace, and each span is the child of the one before it.
    chain = [root, send_discovered, process, send_parsed]
    assert {s.context.trace_id for s in chain} == {root.context.trace_id}
    assert [parent_of(s) for s in chain] == [None] + [s.context.span_id for s in chain[:-1]]
    assert [s.kind for s in chain[1:]] == [SpanKind.PRODUCER, SpanKind.CONSUMER, SpanKind.PRODUCER]
    assert (process.attributes or {})["messaging.consumer.group.name"] == group

    # The next stage's message carries the trace on, from the second publish span.
    upstream = trace.get_current_span(context_from_headers(next_headers)).get_span_context()
    assert (upstream.trace_id, upstream.span_id) == (
        root.context.trace_id,
        send_parsed.context.span_id,
    )
