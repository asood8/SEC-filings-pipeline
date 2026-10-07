"""Shared pytest fixtures.

Integration tests (marked `integration`) use the local Compose stack from `make up`. When a service
is unreachable they are skipped, unless REQUIRE_SERVICES=1 (set in CI), which makes them fail.

Tests never export telemetry. Those that check spans record them in memory (the `spans` fixture).
"""

import os
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, NoReturn

import pytest
import redis
import sqlalchemy as sa
from sqlalchemy.engine import Engine, make_url

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from pymongo.database import Database

# Tracing stays off in any process a test starts, whatever a developer's .env says: environment
# variables take precedence over .env, and a blank endpoint means "unset".
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""

# Defaults match .env.example. Tests use their own Redis DB and a throwaway Postgres database.
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
TEST_POSTGRES_URL = os.environ.get(
    "TEST_POSTGRES_URL", "postgresql+psycopg://filings:filings-local-dev@localhost:5432/filings"
)
TEST_KAFKA_BOOTSTRAP = os.environ.get("TEST_KAFKA_BOOTSTRAP", "localhost:19092")
TEST_MONGODB_URL = os.environ.get(
    "TEST_MONGODB_URL", "mongodb://filings:filings-local-dev@localhost:27017/?authSource=admin"
)


def _unavailable(service: str, exc: Exception) -> NoReturn:
    message = f"{service} is not reachable ({exc.__class__.__name__}); start it with `make up`"
    if os.environ.get("REQUIRE_SERVICES") == "1":
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture(scope="session")
def _span_exporter() -> "InMemorySpanExporter":
    """Installs a tracer provider that keeps finished spans in memory, once per test run: a
    process's global tracer provider can only be set once."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture
def spans(_span_exporter: "InMemorySpanExporter") -> Iterator["InMemorySpanExporter"]:
    """The spans the test finishes (`spans.get_finished_spans()`). Spans finished earlier, by
    other tests, are dropped first."""
    _span_exporter.clear()
    yield _span_exporter
    _span_exporter.clear()


@pytest.fixture(scope="session")
def redis_client() -> Iterator[redis.Redis]:
    client = redis.Redis.from_url(TEST_REDIS_URL)
    try:
        client.ping()
    except redis.ConnectionError as exc:
        _unavailable("Valkey", exc)
    yield client
    client.close()


@pytest.fixture(scope="session")
def kafka_bootstrap() -> str:
    from confluent_kafka import KafkaException
    from confluent_kafka.admin import AdminClient

    try:
        AdminClient({"bootstrap.servers": TEST_KAFKA_BOOTSTRAP}).list_topics(timeout=5)
    except KafkaException as exc:
        _unavailable("Redpanda", exc)
    return TEST_KAFKA_BOOTSTRAP


@pytest.fixture
def mongo_db() -> Iterator["Database[dict[str, Any]]"]:
    """A throwaway MongoDB database, dropped after the test."""
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError

    client: MongoClient[dict[str, Any]] = MongoClient(
        TEST_MONGODB_URL, serverSelectionTimeoutMS=3000
    )
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        client.close()
        _unavailable("MongoDB", exc)
    name = f"filings_test_{uuid.uuid4().hex[:12]}"
    try:
        yield client[name]
    finally:
        client.drop_database(name)
        client.close()


@pytest.fixture(scope="session")
def migrated_db_url() -> Iterator[str]:
    """A fresh Postgres database with all Alembic migrations applied, dropped after the run."""
    from filings_common.db import run_migrations

    server = make_url(TEST_POSTGRES_URL)
    admin = sa.create_engine(server, isolation_level="AUTOCOMMIT")
    name = f"filings_test_{uuid.uuid4().hex[:12]}"
    try:
        with admin.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    except sa.exc.OperationalError as exc:
        admin.dispose()
        _unavailable("Postgres", exc)
    url = server.set(database=name).render_as_string(hide_password=False)
    try:
        run_migrations(url)
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def db_engine(migrated_db_url: str) -> Iterator[Engine]:
    engine = sa.create_engine(migrated_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def clean_db(db_engine: Engine) -> Engine:
    """The migrated test database, emptied before the test."""
    from filings_common.db import Base

    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with db_engine.begin() as conn:
        conn.execute(sa.text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return db_engine
