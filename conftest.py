"""Shared pytest fixtures.

Integration tests (marked `integration`) use the local Compose stack from `make up`. When a service
is unreachable they are skipped, unless REQUIRE_SERVICES=1 (set in CI), which makes them fail.

Tests never export telemetry. Those that check spans or metrics record them in memory (the `spans`
and `measured` fixtures).
"""

import os
import uuid
from collections import defaultdict
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any, NoReturn

import pytest
import redis
import sqlalchemy as sa
from sqlalchemy.engine import Engine, make_url

if TYPE_CHECKING:
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
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
def _metric_reader() -> "InMemoryMetricReader":
    """Installs a meter provider (with the services' views) that a test can read, once per run.

    Counters and histograms are read as deltas: each read returns what was recorded since the
    previous one, so values recorded by earlier tests never leak into a test.
    """
    from opentelemetry import metrics
    from opentelemetry.sdk.metrics import Counter, Histogram, MeterProvider
    from opentelemetry.sdk.metrics.export import AggregationTemporality, InMemoryMetricReader

    from filings_common.telemetry import metric_views

    delta = AggregationTemporality.DELTA
    reader = InMemoryMetricReader(preferred_temporality={Counter: delta, Histogram: delta})
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader], views=metric_views()))
    return reader


class Measured:
    """What a test measured, by metric name. Attributes match when they include the given ones."""

    def __init__(self, reader: "InMemoryMetricReader") -> None:
        self._reader = reader
        self._points: dict[str, list[tuple[dict[str, Any], Any]]] = defaultdict(list)

    def read(self) -> dict[str, list[tuple[dict[str, Any], Any]]]:
        """Collect what was measured since the last read (observable gauges are measured now), and
        return it by metric name."""
        data = self._reader.get_metrics_data()
        new: dict[str, list[tuple[dict[str, Any], Any]]] = defaultdict(list)
        for resource in data.resource_metrics if data is not None else []:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    for point in metric.data.data_points:
                        new[metric.name].append((dict(point.attributes or {}), point))
        for name, points in new.items():
            self._points[name].extend(points)
        return new

    def _matching(self, name: str, attributes: Mapping[str, Any] | None) -> list[Any]:
        self.read()
        wanted = (attributes or {}).items()
        return [point for attrs, point in self._points[name] if wanted <= attrs.items()]

    def total(self, name: str, attributes: Mapping[str, Any] | None = None) -> float:
        """A counter's total."""
        return float(sum(point.value for point in self._matching(name, attributes)))

    def count(self, name: str, attributes: Mapping[str, Any] | None = None) -> int:
        """How many values a histogram recorded."""
        return int(sum(point.count for point in self._matching(name, attributes)))

    def gauge(self, name: str, attributes: Mapping[str, Any] | None = None) -> dict[str, float]:
        """An observable gauge's values now, by the value of its other attributes (joined with
        "/"): e.g. {"parser/filings.discovered/0": 3.0} for consumer lag by group, topic, and
        partition."""
        wanted = (attributes or {}).items()
        return {
            "/".join(str(v) for k, v in sorted(attrs.items()) if k not in dict(wanted)): point.value
            for attrs, point in self.read()[name]
            if wanted <= attrs.items()
        }

    def attribute_sets(self, name: str) -> list[dict[str, Any]]:
        """The distinct attribute sets (time series) a metric recorded."""
        self.read()
        unique = {tuple(sorted(attrs.items())) for attrs, _ in self._points[name]}
        return [dict(items) for items in sorted(unique)]

    def names(self) -> set[str]:
        self.read()
        return set(self._points)


@pytest.fixture
def measured(_metric_reader: "InMemoryMetricReader") -> Measured:
    """The metrics the test records. Measurements from earlier tests are dropped first."""
    _metric_reader.get_metrics_data()
    return Measured(_metric_reader)


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
