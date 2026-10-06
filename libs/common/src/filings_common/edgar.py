"""The one HTTP client for SEC EDGAR.

Every SEC request goes through `EdgarClient`, which sends a declared User-Agent, takes a token from
the shared rate limiter before each attempt, and backs off on 429/5xx and network errors.
See https://www.sec.gov/os/accessing-edgar-data.
"""

import logging
import random
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any, Self

import httpx
import redis

from filings_common.config import EdgarSettings
from filings_common.ratelimit import RateLimiter, RedisTokenBucket

logger = logging.getLogger(__name__)

SEC_WWW = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"
# One bucket for every process that talks to SEC.
RATE_LIMIT_KEY = "ratelimit:sec"

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0


def archive_url(cik: int, accession_no: str, filename: str) -> str:
    """URL of a document inside a filing's folder on EDGAR."""
    return f"{SEC_WWW}/Archives/edgar/data/{cik}/{accession_no.replace('-', '')}/{filename}"


class EdgarClient:
    def __init__(
        self,
        settings: EdgarSettings,
        limiter: RateLimiter,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._limiter = limiter
        self._max_attempts = settings.max_attempts
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._http = httpx.Client(
            headers={"User-Agent": settings.user_agent},
            timeout=settings.timeout_seconds,
            transport=transport,
            follow_redirects=True,
        )

    @classmethod
    def from_settings(cls, settings: EdgarSettings, redis_client: redis.Redis) -> Self:
        """The production wiring: rate-limited by the bucket shared through Redis."""
        limiter = RedisTokenBucket(
            redis_client,
            RATE_LIMIT_KEY,
            settings.rate_limit_per_second,
            settings.rate_limit_burst,
        )
        return cls(settings, limiter)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def get(self, url: str) -> httpx.Response:
        """GET a URL, retrying transient failures. Raises httpx.HTTPStatusError otherwise."""
        for attempt in range(1, self._max_attempts + 1):
            self._limiter.acquire()
            last_attempt = attempt == self._max_attempts
            try:
                response = self._http.get(url)
            except httpx.TransportError as exc:
                if last_attempt:
                    raise
                delay = self._backoff(attempt, retry_after=None)
                logger.warning("SEC request failed (%s); retry %d in %.1fs", exc, attempt, delay)
                self._sleep(delay)
                continue
            if response.status_code in RETRY_STATUSES and not last_attempt:
                delay = self._backoff(attempt, retry_after=response.headers.get("Retry-After"))
                logger.warning(
                    "SEC returned %d for %s; retry %d in %.1fs",
                    response.status_code,
                    url,
                    attempt,
                    delay,
                )
                self._sleep(delay)
                continue
            response.raise_for_status()
            return response
        raise AssertionError("unreachable")  # the loop always returns or raises

    def get_json(self, url: str) -> Any:
        return self.get(url).json()

    def submissions(self, cik: int) -> dict[str, Any]:
        """A company's metadata and its most recent filings (up to 1,000, newest first)."""
        result: dict[str, Any] = self.get_json(f"{SEC_DATA}/submissions/CIK{cik:010d}.json")
        return result

    def company_tickers_exchange(self) -> dict[str, Any]:
        """SEC's ticker ↔ CIK ↔ exchange map for all listed companies."""
        result: dict[str, Any] = self.get_json(f"{SEC_WWW}/files/company_tickers_exchange.json")
        return result

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        # Exponential backoff with jitter so concurrent workers don't retry in lockstep.
        ceiling = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 2.0 ** (attempt - 1))
        delay = ceiling / 2 + self._rng.uniform(0, ceiling / 2)
        if retry_after is not None and retry_after.isdigit():
            delay = max(delay, float(retry_after))
        return delay
