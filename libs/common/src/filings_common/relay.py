"""The outbox relay process (`filings-outbox-relay`): create missing topics, then relay, and prune
published events past their retention."""

import signal
import threading
from datetime import timedelta

from filings_common.config import (
    DatabaseSettings,
    KafkaSettings,
    OutboxRelaySettings,
    OutboxSettings,
)
from filings_common.db import create_engine, session_factory
from filings_common.kafka import PIPELINE_TOPICS, KafkaPublisher, ensure_topics
from filings_common.outbox import OutboxPruner, run_relay
from filings_common.telemetry import setup_telemetry


def main() -> int:
    telemetry = setup_telemetry("filings-outbox-relay", distribution="filings-common")
    kafka = KafkaSettings()
    settings = OutboxRelaySettings()
    retention = timedelta(days=OutboxSettings().retention_days)
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
    sessions = session_factory(engine)
    publisher = KafkaPublisher(kafka.bootstrap_servers)
    try:
        run_relay(
            sessions,
            publisher,
            stop,
            batch_size=settings.batch_size,
            idle_seconds=settings.idle_seconds,
            pruner=OutboxPruner(sessions, retention),
        )
    finally:
        publisher.close()
        engine.dispose()
        telemetry.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
