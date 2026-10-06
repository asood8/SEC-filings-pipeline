"""The outbox relay process (`filings-outbox-relay`): create missing topics, then relay."""

import logging
import signal
import threading

from filings_common.config import DatabaseSettings, KafkaSettings, OutboxRelaySettings
from filings_common.db import create_engine, session_factory
from filings_common.kafka import PIPELINE_TOPICS, KafkaPublisher, ensure_topics
from filings_common.outbox import run_relay


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    kafka = KafkaSettings()
    settings = OutboxRelaySettings()
    ensure_topics(
        kafka.bootstrap_servers,
        PIPELINE_TOPICS,
        partitions=kafka.topic_partitions,
        replication_factor=kafka.topic_replication_factor,
    )

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):  # SIGTERM is how Kubernetes stops a pod
        signal.signal(sig, lambda *_: stop.set())

    engine = create_engine(DatabaseSettings().database_url.get_secret_value())
    publisher = KafkaPublisher(kafka.bootstrap_servers)
    try:
        run_relay(
            session_factory(engine),
            publisher,
            stop,
            batch_size=settings.batch_size,
            idle_seconds=settings.idle_seconds,
        )
    finally:
        publisher.close()
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
