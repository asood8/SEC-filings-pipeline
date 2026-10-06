import uuid

import pytest
from confluent_kafka import Consumer
from confluent_kafka.admin import AdminClient

from filings_common.kafka import PIPELINE_TOPICS, KafkaPublisher, PublishError, ensure_topics
from filings_common.outbox import OutboundMessage


def delete_topics(bootstrap: str, names: list[str]) -> None:
    admin = AdminClient({"bootstrap.servers": bootstrap})  # must outlive its futures
    for future in admin.delete_topics(names).values():
        future.result()


def test_pipeline_topics_include_dead_letter_topics() -> None:
    assert PIPELINE_TOPICS == [
        "filings.discovered",
        "filings.parsed",
        "filings.ready",
        "filings.discovered.dlq",
        "filings.parsed.dlq",
        "filings.ready.dlq",
    ]


def test_publish_raises_when_the_broker_is_unreachable() -> None:
    publisher = KafkaPublisher("localhost:1", flush_timeout=1.0)
    with pytest.raises(PublishError, match=r"1 undelivered|1 failed"):
        publisher.publish([OutboundMessage("t", "k", b"v")])


@pytest.mark.integration
def test_ensure_topics_creates_only_missing_topics(kafka_bootstrap: str) -> None:
    names = [f"test.{uuid.uuid4().hex[:8]}.{i}" for i in range(2)]
    created = ensure_topics(kafka_bootstrap, names, partitions=3, replication_factor=1)
    assert sorted(created) == sorted(names)
    assert ensure_topics(kafka_bootstrap, names, partitions=3, replication_factor=1) == []

    metadata = AdminClient({"bootstrap.servers": kafka_bootstrap}).list_topics(timeout=10)
    assert all(len(metadata.topics[n].partitions) == 3 for n in names)
    delete_topics(kafka_bootstrap, names)


@pytest.mark.integration
def test_published_messages_can_be_consumed_with_their_keys(kafka_bootstrap: str) -> None:
    topic = f"test.{uuid.uuid4().hex[:8]}"
    ensure_topics(kafka_bootstrap, [topic], partitions=1, replication_factor=1)
    sent = [OutboundMessage(topic, f"key-{i}", f"value-{i}".encode()) for i in range(3)]
    KafkaPublisher(kafka_bootstrap).publish(sent)

    consumer = Consumer(
        {
            "bootstrap.servers": kafka_bootstrap,
            "group.id": f"test-{uuid.uuid4().hex[:8]}",
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe([topic])
    received: list[tuple[bytes | None, bytes | None]] = []
    try:
        for _ in range(30):
            msg = consumer.poll(1.0)
            if msg is not None and msg.error() is None:
                received.append((msg.key(), msg.value()))
            if len(received) == len(sent):
                break
    finally:
        consumer.close()
        delete_topics(kafka_bootstrap, [topic])
    assert received == [(m.key.encode(), m.value) for m in sent]
