import threading
import uuid
from collections.abc import Sequence
from typing import Literal

import pytest
from confluent_kafka import Consumer, KafkaError, Message, TopicPartition
from confluent_kafka.admin import AdminClient
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from filings_common.consumer import (
    MessageSource,
    PermanentError,
    Received,
    make_consumer,
    run_consumer,
)
from filings_common.kafka import KafkaPublisher, ensure_topics
from filings_common.outbox import OutboundMessage
from filings_common.telemetry import current_trace_context, kafka_headers

TOPIC = "filings.discovered"
tracer = trace.get_tracer(__name__)


class FakeSource:
    """Serves queued messages, then sets `stop` once drained so the loop ends."""

    def __init__(self, stop: threading.Event, *messages: Message) -> None:
        self.pending = list(messages)
        self.stop = stop
        self.committed: list[int | None] = []
        self.closed = False

    def subscribe(self, topics: list[str]) -> None:
        assert topics == [TOPIC]

    def poll(self, timeout: float = -1) -> Message | None:
        if not self.pending:
            self.stop.set()
            return None
        return self.pending.pop(0)

    def commit(self, *, message: Message, asynchronous: Literal[False]) -> object:
        self.committed.append(message.offset())
        return None

    def close(self) -> None:
        self.closed = True


class RecordingPublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[OutboundMessage] = []
        self.fail = fail

    def publish(self, messages: Sequence[OutboundMessage]) -> None:
        if self.fail:
            raise RuntimeError("DLQ unavailable")
        self.messages.extend(messages)


def message(offset: int, key: str, headers: Sequence[tuple[str, bytes]] = ()) -> Message:
    return Message(
        topic=TOPIC,
        partition=0,
        offset=offset,
        key=key.encode(),
        value=b"{}",
        headers=list(headers) or None,
    )


def relay_headers() -> tuple[trace.Span, tuple[tuple[str, bytes], ...]]:
    """A finished publish span, as the relay's, and the headers it sent."""
    with tracer.start_as_current_span("send filings.discovered", kind=SpanKind.PRODUCER) as span:
        return span, kafka_headers(span)


def consume(source: FakeSource, handler: object, dlq: RecordingPublisher, **kwargs: float) -> None:
    run_consumer(source, TOPIC, handler, dlq, source.stop, backoff_seconds=0.001, **kwargs)  # type: ignore[arg-type]


def test_handles_in_order_and_commits_each_message_after_handling() -> None:
    stop = threading.Event()
    seen: list[str | None] = []
    source = FakeSource(stop, message(0, "a"), message(1, "b"))
    consume(source, lambda r: seen.append(r.key), RecordingPublisher())
    assert seen == ["a", "b"]
    assert source.committed == [0, 1]
    assert source.closed


def test_transient_failures_are_retried() -> None:
    stop = threading.Event()
    calls: list[int] = []

    def flaky(received: Received) -> None:
        calls.append(received.offset)
        if len(calls) < 3:
            raise ConnectionError("database restarting")

    dlq = RecordingPublisher()
    source = FakeSource(stop, message(7, "a"))
    consume(source, flaky, dlq, max_attempts=3)
    assert calls == [7, 7, 7]
    assert dlq.messages == []
    assert source.committed == [7]


def test_exhausted_retries_dead_letter_the_message_and_move_on() -> None:
    stop = threading.Event()

    def handler(received: Received) -> None:
        if received.key == "bad":
            raise ValueError("cannot parse")

    dlq = RecordingPublisher()
    source = FakeSource(stop, message(0, "bad"), message(1, "good"))
    consume(source, handler, dlq, max_attempts=2)
    assert source.committed == [0, 1]
    [dead] = dlq.messages
    assert (dead.topic, dead.key, dead.value) == ("filings.discovered.dlq", "bad", b"{}")
    headers = dict(dead.headers)
    assert headers["dlq.source"] == b"filings.discovered/0/0"
    assert headers["dlq.attempts"] == b"2"
    assert b"cannot parse" in headers["dlq.error"]


def test_permanent_errors_skip_retries() -> None:
    stop = threading.Event()
    calls: list[int] = []

    def handler(received: Received) -> None:
        calls.append(received.offset)
        raise PermanentError("unsupported schema_version")

    dlq = RecordingPublisher()
    consume(FakeSource(stop, message(0, "a")), handler, dlq, max_attempts=5)
    assert calls == [0]
    assert dict(dlq.messages[0].headers)["dlq.attempts"] == b"1"


def test_stopping_during_backoff_leaves_the_message_uncommitted() -> None:
    stop = threading.Event()

    def handler(received: Received) -> None:
        stop.set()  # e.g. SIGTERM arrives while the handler is failing
        raise ConnectionError("down")

    source = FakeSource(stop, message(0, "a"))
    run_consumer(source, TOPIC, handler, RecordingPublisher(), stop, backoff_seconds=10)
    assert source.committed == []
    assert source.closed


def test_broker_errors_are_skipped() -> None:
    stop = threading.Event()
    broken = Message(topic=TOPIC, partition=0, offset=0)
    broken.set_error(KafkaError(KafkaError._TRANSPORT))
    seen: list[int] = []
    source = FakeSource(stop, broken, message(1, "a"))
    consume(source, lambda r: seen.append(r.offset), RecordingPublisher())
    assert seen == [1]
    assert source.committed == [1]


def test_dlq_failure_stops_the_consumer_without_committing() -> None:
    stop = threading.Event()

    def handler(received: Received) -> None:
        raise PermanentError("bad")

    source = FakeSource(stop, message(0, "a"))
    with pytest.raises(RuntimeError, match="DLQ unavailable"):
        consume(source, handler, RecordingPublisher(fail=True))
    assert source.committed == []
    assert source.closed


def test_each_message_is_handled_in_a_consumer_span_that_continues_its_trace(
    spans: InMemorySpanExporter,
) -> None:
    send, headers = relay_headers()
    stop = threading.Event()
    seen: list[dict[str, str] | None] = []
    source = FakeSource(stop, message(5, "0000320193-26-000101", headers))
    run_consumer(
        source,
        TOPIC,
        lambda r: seen.append(current_trace_context()),  # what the handler's enqueue would store
        RecordingPublisher(),
        stop,
        consumer_group="parser",
    )

    [process] = [s for s in spans.get_finished_spans() if s.name == "process filings.discovered"]
    assert process.kind is SpanKind.CONSUMER
    assert process.parent is not None
    assert process.parent.span_id == send.get_span_context().span_id
    assert process.context.trace_id == send.get_span_context().trace_id
    assert dict(process.attributes or {}) == {
        "messaging.system": "kafka",
        "messaging.operation.type": "process",
        "messaging.destination.name": TOPIC,
        "messaging.destination.partition.id": "0",
        "messaging.kafka.offset": 5,
        "messaging.consumer.group.name": "parser",
        "messaging.kafka.message.key": "0000320193-26-000101",
        "filings.attempts": 1,
        "filings.outcome": "handled",
    }
    [stored] = seen
    assert stored is not None
    assert trace.format_span_id(process.context.span_id) in stored["traceparent"]


def test_dead_letters_keep_their_trace_headers(spans: InMemorySpanExporter) -> None:
    _, headers = relay_headers()
    stop = threading.Event()

    def handler(received: Received) -> None:
        raise PermanentError("rejected https://hooks.slack.com/services/T1/B1/secret")

    dlq = RecordingPublisher()
    consume(FakeSource(stop, message(0, "a", headers)), handler, dlq)

    [dead] = dlq.messages
    assert dead.headers[: len(headers)] == headers  # the trace headers, unchanged
    [process] = [s for s in spans.get_finished_spans() if s.name == "process filings.discovered"]
    assert process.status.status_code is StatusCode.ERROR
    assert process.status.description == "filings_common.consumer.PermanentError"
    assert (process.attributes or {})["filings.outcome"] == "dead_lettered"
    assert "secret" not in process.to_json()  # failures are recorded by type only


def test_messages_without_trace_headers_start_a_new_trace(spans: InMemorySpanExporter) -> None:
    stop = threading.Event()
    consume(FakeSource(stop, message(0, "a")), lambda r: None, RecordingPublisher())
    [process] = spans.get_finished_spans()
    assert process.parent is None


@pytest.mark.integration
def test_round_trip_with_redpanda_commits_offsets_and_dead_letters(kafka_bootstrap: str) -> None:
    topic = f"test.{uuid.uuid4().hex[:8]}"
    ensure_topics(kafka_bootstrap, [topic, f"{topic}.dlq"], partitions=1, replication_factor=1)
    publisher = KafkaPublisher(kafka_bootstrap)
    _, trace_headers = relay_headers()
    publisher.publish(
        [OutboundMessage(topic, k, b"{}", trace_headers) for k in ("good-1", "bad", "good-2")]
    )

    group = f"test-{uuid.uuid4().hex[:8]}"
    source: MessageSource = make_consumer(kafka_bootstrap, group)  # the real consumer fits
    handled: list[str | None] = []
    stop = threading.Event()

    def handler(received: Received) -> None:
        if received.key == "bad":
            raise PermanentError("malformed")
        handled.append(received.key)
        if received.key == "good-2":
            stop.set()

    worker = threading.Thread(
        target=run_consumer,
        args=(source, topic, handler, publisher, stop),
    )
    worker.start()
    worker.join(timeout=30)
    assert not worker.is_alive()
    assert handled == ["good-1", "good-2"]

    checker = Consumer({"bootstrap.servers": kafka_bootstrap, "group.id": group})
    try:
        [committed] = checker.committed([TopicPartition(topic, 0)], timeout=10)
        assert committed.offset == 3
        checker.assign([TopicPartition(f"{topic}.dlq", 0, 0)])
        dead = checker.poll(10)
        assert dead is not None
        assert dead.key() == b"bad"
        dead_headers = dict(dead.headers() or [])
        assert dead_headers["dlq.source"] == f"{topic}/0/1".encode()
        assert dead_headers["traceparent"] == dict(trace_headers)["traceparent"]
    finally:
        checker.close()
        admin = AdminClient({"bootstrap.servers": kafka_bootstrap})
        for future in admin.delete_topics([topic, f"{topic}.dlq"]).values():
            future.result()
