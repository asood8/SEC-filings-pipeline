"""Readiness (GET /readyz): whether this replica can serve, which takes Postgres.

/readyz is reachable from outside and never rate limited (the kubelet probes it often), so
nobody may turn it into a flood of database work:
- One check runs at a time. Requests arriving meanwhile wait for its answer, and an answer is
  reused for `reuse_for` seconds.
- A check that takes longer than `timeout` is given up on, and its worker thread abandoned (a
  hung connection can block for minutes). Until that thread returns, no new check reaches the
  database: they report it unresponsive at once. A hung database therefore ties up one thread,
  not one per probe.
"""

import logging
import threading
import time
from collections.abc import Callable

import anyio
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)


class _StillWaitingError(Exception):
    """An abandoned check is still waiting on the database."""


class DatabaseReadiness:
    def __init__(
        self,
        *,
        timeout: float = 2.0,
        reuse_for: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.timeout = timeout
        self._reuse_for = reuse_for
        self._clock = clock
        self._lock = anyio.Lock()
        # Held by the thread running the query, so it outlives an abandoned check.
        self._querying = threading.Lock()
        self._checked_at: float | None = None
        self._problem: str | None = None

    async def problem(self, sessions: sessionmaker[Session]) -> str | None:
        """Why the database isn't usable, or None if it is."""
        async with self._lock:
            if self._checked_at is None or self._clock() - self._checked_at >= self._reuse_for:
                self._record(await self._check(sessions))
                self._checked_at = self._clock()
            return self._problem

    async def _check(self, sessions: sessionmaker[Session]) -> str | None:
        try:
            with anyio.fail_after(self.timeout):
                await anyio.to_thread.run_sync(self._select_one, sessions, abandon_on_cancel=True)
        except TimeoutError:
            return f"no answer from the database within {self.timeout:g}s"
        except _StillWaitingError:
            return "an earlier check is still waiting for the database"
        except sa.exc.SQLAlchemyError as exc:
            return f"database error: {exc}"
        return None

    def _select_one(self, sessions: sessionmaker[Session]) -> None:
        if not self._querying.acquire(blocking=False):
            raise _StillWaitingError
        try:
            with sessions() as session:
                session.execute(sa.text("SELECT 1"))
        finally:
            self._querying.release()

    def _record(self, problem: str | None) -> None:
        """Log changes only: a probe every few seconds would otherwise repeat the same line."""
        if problem is not None and problem != self._problem:
            logger.warning("not ready: %s", problem)
        elif problem is None and self._problem is not None:
            logger.info("ready again: the database answers")
        self._problem = problem
