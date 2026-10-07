import time
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from filings_api.app import api_redis_client
from filings_api.middleware import RedisBackoff

ClientFactory = Callable[..., TestClient]

pytestmark = pytest.mark.integration


def test_rate_limit_rejects_after_the_burst_with_retry_after(make_client: ClientFactory) -> None:
    client = make_client(rate_limit_per_minute=60, rate_limit_burst=2)
    assert [client.get("/companies").status_code for _ in range(2)] == [200, 200]
    rejected = client.get("/companies")
    assert rejected.status_code == 429
    assert rejected.headers["Retry-After"] == "1"
    assert client.get("/healthz").status_code == 200  # probes are never limited


def test_trusted_proxy_header_identifies_the_client(make_client: ClientFactory) -> None:
    client = make_client(rate_limit_per_minute=60, rate_limit_burst=1, trust_proxy_headers=True)
    assert client.get("/companies", headers={"CF-Connecting-IP": "1.1.1.1"}).status_code == 200
    assert client.get("/companies", headers={"CF-Connecting-IP": "1.1.1.1"}).status_code == 429
    assert client.get("/companies", headers={"CF-Connecting-IP": "2.2.2.2"}).status_code == 200


def test_untrusted_proxy_header_is_ignored(make_client: ClientFactory) -> None:
    client = make_client(rate_limit_per_minute=60, rate_limit_burst=1)
    assert client.get("/companies", headers={"CF-Connecting-IP": "1.1.1.1"}).status_code == 200
    # A spoofed header doesn't buy a fresh bucket.
    assert client.get("/companies", headers={"CF-Connecting-IP": "2.2.2.2"}).status_code == 429


def test_responses_are_cached_per_url(make_client: ClientFactory) -> None:
    client = make_client(cache_ttl_seconds=60)
    first = client.get("/companies", params={"watchlist": "true"})
    second = client.get("/companies", params={"watchlist": "true"})
    other = client.get("/companies", params={"watchlist": "false"})
    assert (first.headers["X-Cache"], second.headers["X-Cache"]) == ("MISS", "HIT")
    assert second.json() == first.json()
    assert other.headers["X-Cache"] == "MISS"
    assert "X-Cache" not in client.get("/companies/NOPE").headers  # errors aren't cached


def test_redis_outage_fails_open_and_fast(make_client: ClientFactory) -> None:
    dead = api_redis_client("redis://localhost:1/0")  # nothing listens on port 1
    client = make_client(redis_client=dead, rate_limit_burst=1, cache_ttl_seconds=60)
    start = time.monotonic()
    assert [client.get("/companies").status_code for _ in range(5)] == [200] * 5
    # One failed connection, then Redis is skipped: not a timeout per request.
    assert time.monotonic() - start < 2


def test_backoff_skips_redis_for_a_while_after_an_error() -> None:
    now = [0.0]
    backoff = RedisBackoff(seconds=10, clock=lambda: now[0])
    assert backoff.available()
    backoff.failed()
    now[0] = 9.9
    assert not backoff.available()
    now[0] = 10.0
    assert backoff.available()
