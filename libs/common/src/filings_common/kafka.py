"""Redpanda (Kafka API) helpers: topic setup and a publisher that confirms delivery."""

import logging
from collections.abc import Iterable, Sequence

from confluent_kafka import KafkaError, KafkaException, Message, Producer
from confluent_kafka.admin import AdminClient, NewTopic

from filings_common.events import Topic
from filings_common.outbox import OutboundMessage

logger = logging.getLogger(__name__)

# Every pipeline topic and its dead-letter topic.
PIPELINE_TOPICS = [t.value for t in Topic] + [t.dlq for t in Topic]
# Events are small (IDs and pointers), so keeping them forever is cheap, and it lets replay
# re-read the full history by resetting a consumer group.
TOPIC_CONFIG = {"retention.ms": "-1"}


class PublishError(RuntimeError):
    pass


def ensure_topics(
    bootstrap_servers: str,
    topics: Iterable[str],
    *,
    partitions: int,
    replication_factor: int,
    timeout: float = 30.0,
) -> list[str]:
    """Create any missing topics. Returns the names that were created."""
    admin = AdminClient({"bootstrap.servers": bootstrap_servers})
    new = [
        NewTopic(
            t,
            num_partitions=partitions,
            replication_factor=replication_factor,
            config=TOPIC_CONFIG,
        )
        for t in topics
    ]
    created = []
    for name, future in admin.create_topics(new, request_timeout=timeout).items():
        try:
            future.result()
        except KafkaException as exc:
            if exc.args[0].code() == KafkaError.TOPIC_ALREADY_EXISTS:
                continue
            raise
        created.append(name)
    if created:
        logger.info("created topics: %s", ", ".join(sorted(created)))
    return created


class KafkaPublisher:
    """Publishes a batch and blocks until the broker acknowledges every message."""

    def __init__(self, bootstrap_servers: str, *, flush_timeout: float = 30.0) -> None:
        self._flush_timeout = flush_timeout
        self._producer = Producer(
            {
                "bootstrap.servers": bootstrap_servers,
                # Retries inside the client never duplicate or reorder messages.
                "enable.idempotence": True,
                "acks": "all",
                "message.timeout.ms": int(flush_timeout * 1000),
            }
        )

    def publish(self, messages: Sequence[OutboundMessage]) -> None:
        errors: list[KafkaError] = []

        def on_delivery(err: KafkaError | None, _msg: Message) -> None:
            if err is not None:
                errors.append(err)

        for m in messages:
            self._producer.produce(
                m.topic,
                key=m.key.encode(),
                value=m.value,
                headers=list(m.headers),
                on_delivery=on_delivery,
            )
        undelivered = self._producer.flush(self._flush_timeout)
        if undelivered or errors:
            raise PublishError(
                f"{undelivered} undelivered, {len(errors)} failed of {len(messages)}: {errors[:3]}"
            )

    def close(self) -> None:
        self._producer.flush(self._flush_timeout)
