"""The summarizer process (`filings-summarizer`): consume `filings.parsed` until stopped."""

import logging
import signal
import threading
from typing import Any

import redis
from pymongo import MongoClient

from filings_common.config import (
    DatabaseSettings,
    EmbeddingSettings,
    GeminiSettings,
    KafkaSettings,
    LlmSettings,
    MongoSettings,
    RedisSettings,
)
from filings_common.consumer import make_consumer, run_consumer
from filings_common.db import create_engine, session_factory
from filings_common.embeddings import embedder_from_settings
from filings_common.events import Topic
from filings_common.kafka import KafkaPublisher
from filings_common.mongo import ParsedFilingStore
from filings_summarizer.handler import SummaryHandler
from filings_summarizer.summarizers import build_summarizers

CONSUMER_GROUP = "summarizer"
# LLM outages and free-tier 429s are waited out: up to 6 attempts with backoff of 10s, 20s, 40s,
# 80s, 160s (about 5 minutes) before a filing is dead-lettered (replay recovers it).
MAX_ATTEMPTS = 6
BACKOFF_SECONDS = 10.0
MAX_POLL_INTERVAL_SECONDS = 15 * 60


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    kafka = KafkaSettings()
    mongo_settings = MongoSettings()

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    engine = create_engine(DatabaseSettings().database_url.get_secret_value())
    mongo: MongoClient[dict[str, Any]] = MongoClient(mongo_settings.mongodb_url.get_secret_value())
    redis_client = redis.Redis.from_url(RedisSettings().redis_url)
    dead_letters = KafkaPublisher(kafka.bootstrap_servers)
    try:
        handler = SummaryHandler(
            session_factory(engine),
            ParsedFilingStore(mongo[mongo_settings.mongodb_database]),
            build_summarizers(LlmSettings(), GeminiSettings(), redis_client),
            embedder_from_settings(EmbeddingSettings()),
        )
        consumer = make_consumer(
            kafka.bootstrap_servers,
            CONSUMER_GROUP,
            max_poll_interval_seconds=MAX_POLL_INTERVAL_SECONDS,
        )
        run_consumer(
            consumer,
            Topic.FILINGS_PARSED,
            handler,
            dead_letters,
            stop,
            max_attempts=MAX_ATTEMPTS,
            backoff_seconds=BACKOFF_SECONDS,
        )
    finally:
        dead_letters.close()
        redis_client.close()
        mongo.close()
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
