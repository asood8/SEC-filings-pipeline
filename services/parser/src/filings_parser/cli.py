"""The parser process (`filings-parser`): consume `filings.discovered` until stopped."""

import logging
import signal
import threading
from typing import Any

from pymongo import MongoClient

from filings_common.config import (
    DatabaseSettings,
    KafkaSettings,
    MongoSettings,
    ObjectStoreSettings,
)
from filings_common.consumer import make_consumer, run_consumer
from filings_common.db import create_engine, session_factory
from filings_common.events import Topic
from filings_common.kafka import KafkaPublisher
from filings_common.mongo import ParsedFilingStore
from filings_common.storage import object_store_from_settings
from filings_parser.forms import PARSERS
from filings_parser.handler import FilingHandler

CONSUMER_GROUP = "parser"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    kafka = KafkaSettings()
    mongo_settings = MongoSettings()

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    engine = create_engine(DatabaseSettings().database_url.get_secret_value())
    mongo: MongoClient[dict[str, Any]] = MongoClient(mongo_settings.mongodb_url.get_secret_value())
    dead_letters = KafkaPublisher(kafka.bootstrap_servers)
    try:
        parsed = ParsedFilingStore(mongo[mongo_settings.mongodb_database])
        parsed.ensure_indexes()
        handler = FilingHandler(
            session_factory(engine),
            object_store_from_settings(ObjectStoreSettings()),
            parsed,
            PARSERS,
        )
        run_consumer(
            make_consumer(kafka.bootstrap_servers, CONSUMER_GROUP),
            Topic.FILINGS_DISCOVERED,
            handler,
            dead_letters,
            stop,
        )
    finally:
        dead_letters.close()
        mongo.close()
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
