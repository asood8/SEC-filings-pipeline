import threading
import uuid
from collections.abc import Sequence
from typing import Any, Literal

import pytest
from confluent_kafka import Consumer, KafkaError, KafkaException, Message, TopicPartition
from confluent_kafka.admin import AdminClient, ClusterMetadata, PartitionMetadata, TopicMetadata
from opentelemetry import metrics as metrics_api
from opentelemetry import trace
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from filings_common.consumer import (
    MessageSource,
    OffsetMonitor,
    PermanentError,
    Received,
    consumer_lag,
    make_consumer,
    run_consumer,
    topic_depth,
)
from filings_common.kafka import KafkaPublisher, ensure_topics
from filings_common.outbox import OutboundMessage
from filings_common.telemetry import current_trace_context, kafka_headers

TOPIC = "filings.discovered"
DLQ = "filings.discovered.dlq"
INVALID = -1001  # librdkafka's "no offset"
tracer = trace.get_tracer(__name__)


class FakeSource:
    """Serves queued messages, then sets `stop` once drained so the loop ends.

    For offsets: `positions` are the assigned partitions' positions, `committed_offsets` the
    group's commits, `watermarks` the (low, high) offsets by (topic, partition), and `topics` the
    partition counts of the topics that exist.
    """

    def __init__(self, stop: threading.Event, *messages: Message) -> None:
        self.pending = list(messages)
        self.stop = stop
        self.commits: list[int | None] = []
        self.closed = False
        self.positions: dict[int, int] = {}
        self.committed_offsets: dict[int, int] = {}
        self.watermarks: dict[tuple[str, int], tuple[int, int]] = {}
        self.topics: dict[str, int] = {}
        self.broker_down = False

    def subscribe(self, topics: list[str]) -> None:
        assert topics == [TOPIC]

    def poll(self, timeout: float = -1) -> Message | None:
        if not self.pending:
            self.stop.set()
            return None
        return self.pending.pop(0)

    def commit(self, *, message: Message, asynchronous: Literal[False]) -> object:
        self.commits.append(message.offset())
        return None

    def close(self) -> None:
        self.closed = True

    def assignment(self) -> list[TopicPartition]:
        return [TopicPartition(TOPIC, p) for p in self.positions]

    def position(self, partitions: list[TopicPartition]) -> list[TopicPartition]:
        return [
            TopicPartition(tp.topic, tp.partition, self.positions[tp.partition])
            for tp in partitions
        ]

    def committed(
        self, partitions: list[TopicPartition], timeout: float = -1
    ) -> list[TopicPartition]:
        offsets = self.committed_offsets
        return [
            TopicPartition(tp.topic, tp.partition, offsets.get(tp.partition, INVALID))
            for tp in partitions
        ]

    def get_watermark_offsets(
        self, partition: TopicPartition, timeout: float = -1, cached: bool = False
    ) -> tuple[int, int]:
        if self.broker_down:
            raise KafkaException(KafkaError(KafkaError._TRANSPORT))
        return self.watermarks[(partition.topic, partition.partition)]

    def list_topics(self, topic: str | None = None, timeout: float = -1) -> Any:
        cluster = ClusterMetadata()
        for name, count in self.topics.items():
            found = TopicMetadata()
            found.topic = name
            found.partitions = {p: PartitionMetadata() for p in range(count)}
            cluster.topics[name] = found
        return cluster


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
    assert source.commits == [0, 1]
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
    assert source.commits == [7]


def test_exhausted_retries_dead_letter_the_message_and_move_on() -> None:
    stop = threading.Event()

    def handler(received: Received) -> None:
        if received.key == "bad":
            raise ValueError("cannot parse")

    dlq = RecordingPublisher()
    source = FakeSource(stop, message(0, "bad"), message(1, "good"))
    consume(source, handler, dlq, max_attempts=2)
    assert source.commits == [0, 1]
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
    assert source.commits == []
    assert source.closed


def test_broker_errors_are_skipped() -> None:
    stop = threading.Event()
    broken = Message(topic=TOPIC, partition=0, offset=0)
    broken.set_error(KafkaError(KafkaError._TRANSPORT))
    seen: list[int] = []
    source = FakeSource(stop, broken, message(1, "a"))
    consume(source, lambda r: seen.append(r.offset), RecordingPublisher())
    assert seen == [1]
    assert source.commits == [1]


def test_dlq_failure_stops_the_consumer_without_committing() -> None:
    stop = threading.Event()

    def handler(received: Received) -> None:
        raise PermanentError("bad")

    source = FakeSource(stop, message(0, "a"))
    with pytest.raises(RuntimeError, match="DLQ unavailable"):
        consume(source, handler, RecordingPublisher(fail=True))
    assert source.commits == []
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


# --- Metrics -------------------------------------------------------------------------------------


def test_lag_counts_from_the_position_then_the_committed_offset_then_the_oldest_message() -> None:
    source = FakeSource(threading.Event())
    source.positions = {0: 5, 1: INVALID, 2: INVALID}  # partitions 1 and 2: nothing received yet
    source.committed_offsets = {1: 4}  # partition 2: the group never committed
    source.watermarks = {(TOPIC, 0): (0, 8), (TOPIC, 1): (0, 10), (TOPIC, 2): (2, 7)}
    assert consumer_lag(source, timeout=1) == {(TOPIC, 0): 3, (TOPIC, 1): 6, (TOPIC, 2): 5}
    source.positions = {}  # before the group assigns partitions
    assert consumer_lag(source, timeout=1) == {}


def test_dlq_depth_sums_its_partitions_and_is_unknown_for_a_missing_topic() -> None:
    source = FakeSource(threading.Event())
    source.topics = {DLQ: 2}
    source.watermarks = {(DLQ, 0): (0, 3), (DLQ, 1): (1, 2)}
    assert topic_depth(source, DLQ, timeout=1) == 4
    assert topic_depth(source, "filings.parsed.dlq", timeout=1) is None


def test_the_loop_reports_lag_and_dlq_depth_until_it_stops(measured: Any) -> None:
    stop = threading.Event()
    source = FakeSource(stop, message(5, "a"))
    source.positions = {0: 5, 1: INVALID}
    source.watermarks = {(TOPIC, 0): (0, 9), (TOPIC, 1): (0, 2), (DLQ, 0): (0, 1)}
    source.topics = {DLQ: 1}
    seen: list[tuple[dict[str, float], dict[str, float]]] = []

    def handler(received: Received) -> None:
        seen.append((measured.gauge("filings.consumer.lag"), measured.gauge("filings.dlq.depth")))

    run_consumer(
        source,
        TOPIC,
        handler,
        RecordingPublisher(),
        stop,
        consumer_group="parser",
        offsets_interval=0,
    )
    assert seen == [
        ({"parser/filings.discovered/0": 4, "parser/filings.discovered/1": 2}, {DLQ: 1}),
    ]
    # A stopped consumer's series end, rather than repeat a stale value.
    assert measured.gauge("filings.consumer.lag") == {}
    assert measured.gauge("filings.dlq.depth") == {}


def test_offsets_are_measured_at_most_once_per_interval_and_survive_broker_errors(
    measured: Any,
) -> None:
    now = [0.0]
    source = FakeSource(threading.Event())
    source.positions = {0: 1}
    source.watermarks = {(TOPIC, 0): (0, 3)}
    monitor = OffsetMonitor(source, TOPIC, "parser", interval=30, clock=lambda: now[0])
    lag = "filings.consumer.lag"
    try:
        monitor.measure_if_due()
        assert measured.gauge(lag) == {"parser/filings.discovered/0": 2}
        source.watermarks = {(TOPIC, 0): (0, 10)}
        now[0] = 29.0
        monitor.measure_if_due()  # not due yet
        assert measured.gauge(lag) == {"parser/filings.discovered/0": 2}
        source.broker_down = True
        now[0] = 31.0
        monitor.measure_if_due()  # logs a warning and keeps the last value
        assert measured.gauge(lag) == {"parser/filings.discovered/0": 2}
        source.broker_down = False
        now[0] = 62.0
        monitor.measure_if_due()
        assert measured.gauge(lag) == {"parser/filings.discovered/0": 9}
    finally:
        monitor.close()


def test_dead_letters_are_counted_by_dead_letter_topic(measured: Any) -> None:
    stop = threading.Event()
    consume(FakeSource(stop, message(0, "a")), lambda r: None, RecordingPublisher())
    # Started at zero, so that the first dead letter shows as an increase.
    assert measured.attribute_sets("filings.dlq.messages") == [{"messaging.destination.name": DLQ}]
    assert measured.total("filings.dlq.messages") == 0

    def handler(received: Received) -> None:
        raise PermanentError("bad")

    stop = threading.Event()
    consume(FakeSource(stop, message(0, "a"), message(1, "b")), handler, RecordingPublisher())
    assert measured.total("filings.dlq.messages", {"messaging.destination.name": DLQ}) == 2


@pytest.mark.integration
def test_lag_and_dlq_depth_from_redpanda(kafka_bootstrap: str) -> None:
    topic = f"test.{uuid.uuid4().hex[:8]}"
    dlq = f"{topic}.dlq"
    ensure_topics(kafka_bootstrap, [topic, dlq], partitions=3, replication_factor=1)
    publisher = KafkaPublisher(kafka_bootstrap)
    publisher.publish([OutboundMessage(topic, f"key-{i}", b"{}") for i in range(6)])
    publisher.publish([OutboundMessage(dlq, f"dead-{i}", b"{}") for i in range(2)])
    partitions = [TopicPartition(topic, p) for p in range(3)]
    group = f"test-{uuid.uuid4().hex[:8]}"
    first = make_consumer(kafka_bootstrap, group)
    second = make_consumer(kafka_bootstrap, group)
    try:
        first.assign(partitions)
        received = first.poll(10)
        assert received is not None
        assert received.offset() == 0
        lag = consumer_lag(first, timeout=10)
        assert sum(lag.values()) == 5  # all six messages but the one received
        first.commit(message=received, asynchronous=False)

        # A consumer that has received nothing yet counts from the group's committed offset where
        # there is one, and from the oldest message elsewhere: the same lag.
        second.assign(partitions)
        assert consumer_lag(second, timeout=10) == lag
        assert topic_depth(second, dlq, timeout=10) == 2
        assert topic_depth(second, f"{topic}.missing", timeout=10) is None
    finally:
        first.close()
        second.close()
        admin = AdminClient({"bootstrap.servers": kafka_bootstrap})
        for future in admin.delete_topics([topic, dlq]).values():
            future.result()


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


def test_a_failed_commit_is_logged_and_the_loop_carries_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After a rebalance the commit fails; the message comes back later instead of the
    consumer exiting (which would crash-loop on the same message)."""

    class LosesAssignment(FakeSource):
        def commit(self, *, message: Message, asynchronous: Literal[False]) -> object:
            if message.offset() == 0:
                raise KafkaException(KafkaError(KafkaError.UNKNOWN_MEMBER_ID))
            return super().commit(message=message, asynchronous=asynchronous)

    stop = threading.Event()
    seen: list[str | None] = []
    source = LosesAssignment(stop, message(0, "a"), message(1, "b"))
    consume(source, lambda r: seen.append(r.key), RecordingPublisher())
    assert seen == ["a", "b"]
    assert source.commits == [1]
    assert "will be redelivered" in caplog.text


def test_offsets_are_not_measured_when_metrics_are_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """With telemetry off the gauges go nowhere, so the monitor makes no broker calls."""
    monkeypatch.setattr(metrics_api, "get_meter_provider", NoOpMeterProvider)
    calls: list[str] = []

    class Counting(FakeSource):
        def assignment(self) -> list[TopicPartition]:
            calls.append("assignment")
            return super().assignment()

        def list_topics(self, topic: str | None = None, timeout: float = -1) -> Any:
            calls.append("list_topics")
            return super().list_topics(topic, timeout)

    OffsetMonitor(Counting(threading.Event()), TOPIC, "parser", interval=0).measure_if_due()
    assert calls == []
