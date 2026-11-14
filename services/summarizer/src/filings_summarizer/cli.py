"""The summarizer process (`filings-summarizer`): consume `filings.parsed` until stopped."""

import signal
import threading
from dataclasses import dataclass
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
    SummarizerSettings,
)
from filings_common.consumer import make_consumer, run_consumer
from filings_common.db import create_engine, session_factory
from filings_common.embeddings import embedder_from_settings
from filings_common.events import Topic
from filings_common.health import Heartbeat
from filings_common.kafka import KafkaPublisher
from filings_common.mongo import ParsedFilingStore
from filings_common.telemetry import setup_telemetry
from filings_summarizer.handler import SummaryHandler
from filings_summarizer.summarizers import build_summarizers

# The main queue's names. A form with its own queue (SUMMARIZER_SEPARATE_FORMS, e.g. "4") gets
# "-form4" added to both: its heartbeat is filings-summarizer-form4, its group summarizer-form4.
PROCESS_NAME = "filings-summarizer"
CONSUMER_GROUP = "summarizer"
# LLM outages and free-tier 429s are waited out: up to 6 attempts with backoff of 10s, 20s, 40s,
# 80s, 160s (about 5 minutes) before a filing is dead-lettered (replay recovers it).
MAX_ATTEMPTS = 6
BACKOFF_SECONDS = 10.0
# librdkafka's limits for max.poll.interval.ms, in seconds.
MIN_POLL_INTERVAL_SECONDS = 15 * 60
MAX_POLL_INTERVAL_SECONDS = 24 * 60 * 60
POLL_INTERVAL_MARGIN_SECONDS = 60.0
# On top of the models' timeouts in one attempt: the databases, and waiting for Gemini's pacing.
ATTEMPT_MARGIN_SECONDS = 60.0


@dataclass(frozen=True)
class Queue:
    """One consumer of filings.parsed inside the summarizer: its group, its heartbeat's name, and
    the forms it takes (`forms`, empty for any) or leaves to the other queues (`skip`)."""

    process_name: str
    consumer_group: str
    forms: frozenset[str] = frozenset()
    skip: frozenset[str] = frozenset()

    @property
    def uses_llm(self) -> bool:
        return not self.forms or bool(self.forms - TEMPLATE_FORMS)


# Summarized by a template, so a queue taking only these waits on no LLM.
TEMPLATE_FORMS = frozenset({"4"})


def queues(settings: SummarizerSettings) -> list[Queue]:
    """The main queue, taking every form but the separated ones, and one queue per separated
    form."""
    separate = settings.separate_families
    main = Queue(PROCESS_NAME, CONSUMER_GROUP, skip=frozenset(separate))
    return [main] + [
        Queue(f"{PROCESS_NAME}-form{form}", f"{CONSUMER_GROUP}-form{form}", forms=frozenset({form}))
        for form in separate
    ]


def model_timeouts(llm: LlmSettings, gemini: GeminiSettings, embedding: EmbeddingSettings) -> float:
    """The longest one attempt at a message can wait on the models: each configured provider's
    full timeout, and the embedding call's."""
    return (
        (llm.timeout_seconds if llm.model else 0.0)
        + (gemini.timeout_seconds if gemini.api_key is not None else 0.0)
        + (embedding.timeout_seconds if embedding.model else 0.0)
    )


def attempt_seconds(
    llm: LlmSettings, gemini: GeminiSettings, embedding: EmbeddingSettings
) -> float:
    """How long one attempt at a message may legitimately take. The heartbeat allows this much
    before each attempt, so the liveness probe never restarts the summarizer mid-summary, yet
    catches an attempt that outlives every timeout it could be waiting on."""
    return model_timeouts(llm, gemini, embedding) + ATTEMPT_MARGIN_SECONDS


def poll_interval_seconds(
    llm: LlmSettings, gemini: GeminiSettings, embedding: EmbeddingSettings
) -> int:
    """How long handling one message may take, every retry included.

    Past this the broker assumes the consumer died and takes its partitions away: the message is
    redelivered and the same thing happens again, so it is never dead-lettered and blocks its
    partition. The worst case is every attempt waiting out each configured provider's full
    timeout (and the embedding call's), plus the backoff between attempts.
    """
    per_attempt = model_timeouts(llm, gemini, embedding)
    backoff = sum(BACKOFF_SECONDS * 2**attempt for attempt in range(MAX_ATTEMPTS - 1))
    worst = MAX_ATTEMPTS * per_attempt + backoff + POLL_INTERVAL_MARGIN_SECONDS
    return int(min(max(worst, MIN_POLL_INTERVAL_SECONDS), MAX_POLL_INTERVAL_SECONDS))


def main() -> int:
    lanes = queues(SummarizerSettings())
    # Created first, before anything slow: each removes a stale file a previous container left.
    heartbeats = {lane: Heartbeat.for_process(lane.process_name) for lane in lanes}
    telemetry = setup_telemetry(PROCESS_NAME)
    kafka = KafkaSettings()
    mongo_settings = MongoSettings()

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    engine = create_engine(DatabaseSettings().database_url.get_secret_value())
    mongo: MongoClient[dict[str, Any]] = MongoClient(mongo_settings.mongodb_url.get_secret_value())
    redis_client = redis.Redis.from_url(RedisSettings().redis_url)
    dead_letters = KafkaPublisher(kafka.bootstrap_servers)
    llm, gemini, embedding = LlmSettings(), GeminiSettings(), EmbeddingSettings()
    # A queue that never calls the LLM doesn't allow for its timeouts.
    no_llm = (LlmSettings(_env_file=None, model=None), GeminiSettings(_env_file=None, api_key=""))
    failed: list[BaseException] = []
    try:
        summarizers = build_summarizers(llm, gemini, redis_client)
        embedder = embedder_from_settings(embedding)
        sessions = session_factory(engine)
        store = ParsedFilingStore(mongo[mongo_settings.mongodb_database])

        def consume(lane: Queue) -> None:
            models = (llm, gemini) if lane.uses_llm else no_llm
            try:
                run_consumer(
                    make_consumer(
                        kafka.bootstrap_servers,
                        lane.consumer_group,
                        max_poll_interval_seconds=poll_interval_seconds(*models, embedding),
                    ),
                    Topic.FILINGS_PARSED,
                    SummaryHandler(
                        sessions, store, summarizers, embedder, forms=lane.forms, skip=lane.skip
                    ),
                    dead_letters,
                    stop,
                    max_attempts=MAX_ATTEMPTS,
                    backoff_seconds=BACKOFF_SECONDS,
                    consumer_group=lane.consumer_group,
                    heartbeat=heartbeats[lane],
                    attempt_seconds=attempt_seconds(*models, embedding),
                )
            except BaseException as exc:  # one queue failing stops the process: it restarts
                failed.append(exc)
                stop.set()
                raise

        threads = [
            threading.Thread(target=consume, args=(lane,), name=lane.consumer_group)
            for lane in lanes
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        dead_letters.close()
        redis_client.close()
        mongo.close()
        engine.dispose()
        telemetry.shutdown()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
