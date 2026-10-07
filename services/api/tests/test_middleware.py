import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from filings_api.app import api_redis_client
from filings_api.middleware import RedisBackoff, is_private_path

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


# --- Per-user responses never go through the shared cache ----------------------------------


def add_route(client: TestClient, path: str, headers: dict[str, str] | None = None) -> None:
    """A JSON endpoint that counts its calls, for checking what the cache serves."""
    assert isinstance(client.app, FastAPI)
    calls = {"n": 0}

    def endpoint() -> JSONResponse:
        calls["n"] += 1
        return JSONResponse({"calls": calls["n"]}, headers=headers)

    client.app.add_api_route(path, endpoint, methods=["GET"])


@pytest.mark.parametrize("credentials", [{"Cookie": "session=abc"}, {"Authorization": "Bearer x"}])
def test_requests_with_credentials_bypass_the_cache(
    make_client: ClientFactory, credentials: dict[str, str]
) -> None:
    client = make_client(cache_ttl_seconds=60)
    add_route(client, "/per-user")
    assert client.get("/per-user", headers=credentials).json() == {"calls": 1}
    assert client.get("/per-user").json() == {"calls": 2}  # nothing was stored for it...
    assert client.get("/per-user").headers["X-Cache"] == "HIT"
    response = client.get("/per-user", headers=credentials)  # ...and it isn't served from cache
    assert response.json() == {"calls": 3}
    assert "X-Cache" not in response.headers


@pytest.mark.parametrize(
    "cache_control", ["private", "no-store", "private, max-age=60", "max-age=0, No-Store"]
)
def test_responses_marked_private_or_no_store_are_not_cached(
    make_client: ClientFactory, cache_control: str
) -> None:
    client = make_client(cache_ttl_seconds=60)
    add_route(client, "/private", headers={"Cache-Control": cache_control})
    assert [client.get("/private").json()["calls"] for _ in range(2)] == [1, 2]


def test_responses_that_set_cookies_are_not_cached(make_client: ClientFactory) -> None:
    client = make_client(cache_ttl_seconds=60)
    add_route(client, "/cookie", headers={"Set-Cookie": "a=b"})
    assert [client.get("/cookie").json()["calls"] for _ in range(2)] == [1, 2]


@pytest.mark.parametrize("path", ["/subscriptions", "/subscriptions/1", "/me/x", "/auth/x"])
def test_private_paths_are_never_cached_and_say_no_store(
    make_client: ClientFactory, path: str
) -> None:
    client = make_client(cache_ttl_seconds=60)
    add_route(client, path)
    responses = [client.get(path) for _ in range(2)]
    assert [r.json()["calls"] for r in responses] == [1, 2]
    assert all(r.headers["Cache-Control"] == "no-store" for r in responses)


def test_private_paths_say_no_store_with_caching_off(client: TestClient) -> None:
    assert client.get("/me").headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    ("path", "private"),
    [
        ("/me", True),
        ("/me/x", True),
        ("/auth/google", True),
        ("/subscriptions", True),
        ("/subscriptions/7", True),
        ("/meetings", False),
        ("/authors", False),
        ("/companies", False),
        ("/", False),
    ],
)
def test_private_path_prefixes_match_whole_segments(path: str, private: bool) -> None:
    assert is_private_path(path) is private


# --- CSRF: other sites can't make state-changing requests ----------------------------------


@pytest.mark.parametrize(
    ("origin", "status"),
    [
        (None, 204),  # not a browser
        ("http://localhost:3000", 204),  # the website
        ("https://testserver", 204),  # the API's own /docs page
        ("https://evil.example", 403),
        ("http://localhost:3000.evil.example", 403),
        ("null", 403),  # sandboxed frames and some redirects
    ],
)
def test_state_changing_requests_from_other_origins_are_refused(
    client: TestClient, origin: str | None, status: int
) -> None:
    headers = {"Origin": origin} if origin else {}
    response = client.post("/auth/logout", headers=headers)
    assert response.status_code == status
    if status == 403:
        assert response.json() == {"detail": "origin not allowed"}


def test_other_origins_cannot_delete_the_account(client: TestClient, google: Any) -> None:
    client.post("/auth/google", json={"credential": google.token()})
    assert client.delete("/me", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/me").status_code == 200


def test_reads_from_other_origins_are_not_refused(client: TestClient) -> None:
    assert client.get("/companies", headers={"Origin": "https://evil.example"}).status_code == 200


@pytest.mark.parametrize(
    ("content_type", "status"),
    [
        ("application/x-www-form-urlencoded", 415),
        ("multipart/form-data; boundary=x", 415),
        ("text/plain", 415),
        (None, 415),
        ("application/json", 401),  # past the check; the made-up credential is rejected
        ("application/json; charset=utf-8", 401),
    ],
)
def test_request_bodies_must_be_json(
    client: TestClient, content_type: str | None, status: int
) -> None:
    headers = {"Content-Type": content_type} if content_type else {}
    response = client.post("/auth/google", content=b'{"credential": "x"}', headers=headers)
    assert response.status_code == status


def test_bodyless_requests_need_no_content_type(client: TestClient) -> None:
    assert client.post("/auth/logout").status_code == 204
    assert client.delete("/me").status_code == 401  # past the check; not signed in


# --- CORS -----------------------------------------------------------------------------------


def preflight(client: TestClient, origin: str) -> Any:
    return client.options(
        "/auth/google",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )


def test_cors_lets_the_website_send_credentials(client: TestClient) -> None:
    response = preflight(client, "http://localhost:3000")
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
    assert response.headers["Access-Control-Allow-Credentials"] == "true"
    simple = client.get("/companies", headers={"Origin": "http://localhost:3000"})
    assert simple.headers["Access-Control-Allow-Origin"] == "http://localhost:3000"


def test_cors_ignores_other_origins(client: TestClient) -> None:
    assert preflight(client, "https://evil.example").status_code == 400
    simple = client.get("/companies", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in simple.headers


def test_cors_headers_reach_rate_limited_responses(make_client: ClientFactory) -> None:
    client = make_client(rate_limit_per_minute=60, rate_limit_burst=1)
    origin = "http://localhost:3000"
    assert [preflight(client, origin).status_code for _ in range(3)] == [200] * 3
    client.get("/companies", headers={"Origin": origin})
    limited = client.get("/companies", headers={"Origin": origin})
    assert limited.status_code == 429  # the preflights didn't spend the budget; this did
    assert limited.headers["Access-Control-Allow-Origin"] == origin


def test_cached_responses_get_each_origins_own_cors_headers(make_client: ClientFactory) -> None:
    origins = ["http://localhost:3000", "https://app.example.com"]
    client = make_client(cache_ttl_seconds=60, cors_origins=origins)
    first = client.get("/companies", headers={"Origin": origins[0]})
    second = client.get("/companies", headers={"Origin": origins[1]})
    assert second.headers["X-Cache"] == "HIT"
    assert first.headers["Access-Control-Allow-Origin"] == origins[0]
    assert second.headers["Access-Control-Allow-Origin"] == origins[1]
