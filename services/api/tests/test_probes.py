"""The Kubernetes probes: /healthz (liveness) and /readyz (readiness: Postgres answers)."""

import asyncio
import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

import anyio
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from filings_api.readiness import DatabaseReadiness
from filings_common.db import session_factory

pytestmark = pytest.mark.integration

ClientFactory = Callable[..., TestClient]


@pytest.fixture
def unreachable() -> Iterator[Engine]:
    engine = sa.create_engine("postgresql+psycopg://u:p@127.0.0.1:1/db")  # nothing listens
    yield engine
    engine.dispose()


def test_ready_when_the_database_answers(client: TestClient) -> None:
    response = client.get("/readyz")
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def test_not_ready_without_the_database_but_still_live(
    make_client: ClientFactory, unreachable: Engine
) -> None:
    client = make_client(sessions=session_factory(unreachable))
    response = client.get("/readyz")
    # Nothing about the error leaks: /readyz is reachable from outside.
    assert (response.status_code, response.json()) == (503, {"status": "unavailable"})
    assert client.get("/healthz").status_code == 200  # never restarted for a database outage


def test_a_hung_database_is_given_up_on_quickly(make_client: ClientFactory, seeded: Engine) -> None:
    hung = sa.create_engine(seeded.url)
    sa.event.listen(hung, "do_connect", lambda *_: time.sleep(2))  # e.g. packets dropped
    client = make_client(sessions=session_factory(hung))
    assert isinstance(client.app, FastAPI)
    client.app.state.readiness = DatabaseReadiness(timeout=0.2)
    start = time.monotonic()
    assert client.get("/readyz").status_code == 503
    assert time.monotonic() - start < 1.5
    hung.dispose()


def test_a_result_is_reused_for_a_moment(seeded: Engine) -> None:
    queries: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        queries.append(statement)

    sa.event.listen(seeded, "before_cursor_execute", record)
    now = [0.0]
    readiness = DatabaseReadiness(reuse_for=1.0, clock=lambda: now[0])
    sessions = session_factory(seeded)

    def problems() -> list[str | None]:
        return [asyncio.run(readiness.problem(sessions)) for _ in range(3)]

    try:
        assert problems() == [None, None, None]
        assert queries.count("SELECT 1") == 1  # a flood of requests is one query a second
        now[0] = 1.0
        problems()
        assert queries.count("SELECT 1") == 2
    finally:
        sa.event.remove(seeded, "before_cursor_execute", record)


def test_concurrent_requests_share_one_check(seeded: Engine) -> None:
    queries: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        queries.append(statement)

    sa.event.listen(seeded, "before_cursor_execute", record)
    readiness = DatabaseReadiness(reuse_for=1.0)
    sessions = session_factory(seeded)

    async def burst() -> list[str | None]:
        return await asyncio.gather(*(readiness.problem(sessions) for _ in range(5)))

    try:
        assert asyncio.run(burst()) == [None] * 5
        assert queries.count("SELECT 1") == 1
    finally:
        sa.event.remove(seeded, "before_cursor_execute", record)


def test_a_hung_database_ties_up_one_thread_not_one_per_check(seeded: Engine) -> None:
    hung = sa.create_engine(seeded.url)
    connects: list[float] = []

    def slow_connect(*_: Any) -> None:
        connects.append(time.monotonic())
        time.sleep(1.0)

    sa.event.listen(hung, "do_connect", slow_connect)
    readiness = DatabaseReadiness(timeout=0.1, reuse_for=0.0)
    sessions = session_factory(hung)

    async def checks() -> None:
        first = await readiness.problem(sessions)  # abandoned, its thread still connecting
        assert first == "no answer from the database within 0.1s"
        second = await readiness.problem(sessions)
        assert second == "an earlier check is still waiting for the database"
        assert len(connects) == 1  # the second check never reached the database
        await anyio.sleep(1.2)  # the abandoned thread got its connection by now
        assert await readiness.problem(sessions) is None

    try:
        asyncio.run(checks())
    finally:
        hung.dispose()


def test_only_changes_of_readiness_are_logged(
    seeded: Engine, unreachable: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    readiness = DatabaseReadiness(reuse_for=0.0)

    async def checks() -> None:
        for _ in range(3):
            assert await readiness.problem(session_factory(unreachable)) is not None
        for _ in range(2):
            assert await readiness.problem(session_factory(seeded)) is None

    with caplog.at_level(logging.INFO, logger="filings_api.readiness"):
        asyncio.run(checks())
    assert [(r.levelname, r.getMessage()[:20]) for r in caplog.records] == [
        ("WARNING", "not ready: database "),
        ("INFO", "ready again: the dat"),
    ]


def test_probes_are_never_rate_limited_or_cached(make_client: ClientFactory) -> None:
    client = make_client(rate_limit_per_minute=60, rate_limit_burst=1, cache_ttl_seconds=60)
    assert client.get("/companies").status_code == 200
    assert client.get("/companies").status_code == 429
    for path in ("/healthz", "/readyz"):
        responses = [client.get(path) for _ in range(3)]
        assert [r.status_code for r in responses] == [200] * 3
        assert all("X-Cache" not in r.headers for r in responses)


def test_a_cached_ready_answer_never_hides_an_outage(
    make_client: ClientFactory, unreachable: Engine
) -> None:
    ready = make_client(cache_ttl_seconds=60)
    assert ready.get("/readyz").status_code == 200
    # Another replica sharing the cache, but without its database.
    broken = make_client(cache_ttl_seconds=60, sessions=session_factory(unreachable))
    assert broken.get("/readyz").status_code == 503
