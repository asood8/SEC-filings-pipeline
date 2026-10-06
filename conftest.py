"""Shared pytest fixtures.

Integration tests (marked `integration`) use the local Compose stack from `make up`. When a service
is unreachable they are skipped, unless REQUIRE_SERVICES=1 (set in CI), which makes them fail.
"""

import os
import uuid
from collections.abc import Iterator
from typing import NoReturn

import pytest
import redis
import sqlalchemy as sa
from sqlalchemy.engine import Engine, make_url

# Defaults match .env.example. Tests use their own Redis DB and a throwaway Postgres database.
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
TEST_POSTGRES_URL = os.environ.get(
    "TEST_POSTGRES_URL", "postgresql+psycopg://filings:filings-local-dev@localhost:5432/filings"
)


def _unavailable(service: str, exc: Exception) -> NoReturn:
    message = f"{service} is not reachable ({exc.__class__.__name__}); start it with `make up`"
    if os.environ.get("REQUIRE_SERVICES") == "1":
        pytest.fail(message)
    pytest.skip(message)


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
