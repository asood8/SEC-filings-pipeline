from collections.abc import Callable
from typing import Any

import pytest
import redis
from fastapi.testclient import TestClient

from filings_api.app import api_redis_client
from filings_api.embedding_cache import CACHE_PREFIX as EMBEDDING_CACHE_PREFIX
from filings_api.embedding_cache import TTL_SECONDS, QueryEmbeddingCache, cache_key
from filings_api.middleware import RedisBackoff
from filings_common.db import EMBEDDING_DIMENSIONS

pytestmark = pytest.mark.integration


def search(client: TestClient, **params: str | int) -> dict[str, object]:
    response = client.get("/search", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def hits(body: dict[str, object]) -> list[str]:
    items = body["items"]
    assert isinstance(items, list)
    return [item["accession_no"] for item in items]


def test_keyword_search(client: TestClient, ids: Any) -> None:
    body = search(client, q="chief financial officer", mode="keyword")
    assert (body["mode"], body["degraded"]) == ("keyword", False)
    assert hits(body) == [ids.apple_8k]
    # The matching words of the summary come marked, as plain text.
    items = body["items"]
    assert isinstance(items, list)
    snippet = items[0]["snippet"]
    assert "".join(part["text"] for part in snippet).startswith("Apple appointed a new CFO")
    assert all(set(part) == {"text", "match"} for part in snippet)


def test_semantic_search(client: TestClient, ids: Any) -> None:
    body = search(client, q="record revenue guidance", mode="semantic")
    assert body["mode"] == "semantic"
    assert hits(body)[0] == ids.msft_8k


def test_hybrid_is_the_default(client: TestClient, ids: Any) -> None:
    body = search(client, q="insider sale")
    assert (body["mode"], body["degraded"]) == ("hybrid", False)
    assert hits(body)[0] == ids.form4
    first = body["items"][0]  # type: ignore[index]
    assert first["headline"] == "Tim Cook sold 50,000 AAPL shares"
    assert first["score"] > 0


def test_filters_apply_to_search(client: TestClient, ids: Any) -> None:
    assert hits(search(client, q="Apple", mode="keyword", form_type="8-K")) == [ids.apple_8k]


@pytest.mark.parametrize("mode", ["keyword", "semantic", "hybrid"])
def test_filing_dates_apply_in_every_mode(client: TestClient, ids: Any, mode: str) -> None:
    # Filed 2026-09-28 (Apple's 8-K), 09-29 (Microsoft's), and 09-30 (the Form 4).
    later = hits(search(client, q="Apple", mode=mode, since="2026-09-29"))
    assert later
    assert set(later) <= {ids.form4, ids.msft_8k}
    assert hits(search(client, q="Apple", mode=mode, until="2026-09-28")) == [ids.apple_8k]


def test_dates_must_be_in_order(client: TestClient) -> None:
    params = {"q": "Apple", "since": "2026-09-30", "until": "2026-09-01"}
    response = client.get("/search", params=params)
    assert (response.status_code, response.json()) == (422, {"detail": "since is after until"})


@pytest.mark.parametrize("embedder_state", ["unconfigured", "failing"])
def test_falls_back_to_keywords_without_a_working_embedder(
    make_client: Callable[..., TestClient],
    make_embedder: Callable[..., Any],
    ids: Any,
    embedder_state: str,
) -> None:
    embedder = None if embedder_state == "unconfigured" else make_embedder(fail=True)
    body = search(make_client(embedder=embedder), q="chief financial officer")
    assert (body["mode"], body["degraded"]) == ("keyword", True)
    assert hits(body) == [ids.apple_8k]


def test_a_fallback_from_a_failing_model_is_not_cached(
    make_client: Callable[..., TestClient], make_embedder: Callable[..., Any]
) -> None:
    # The model is likely just busy: the next request should try it again.
    client = make_client(embedder=make_embedder(fail=True), cache_ttl_seconds=60)
    responses = [client.get("/search", params={"q": "cfo"}) for _ in range(2)]
    assert [r.json()["degraded"] for r in responses] == [True, True]
    assert all(r.headers["Cache-Control"] == "no-store" for r in responses)
    assert all("X-Cache" not in r.headers for r in responses)
    # Without any model, keyword results are all there is, and they are cached as usual.
    unconfigured = make_client(embedder=None, cache_ttl_seconds=60)
    responses = [unconfigured.get("/search", params={"q": "cfo"}) for _ in range(2)]
    assert [r.headers["X-Cache"] for r in responses] == ["MISS", "HIT"]


def test_query_embeddings_are_cached_across_modes_and_filters(
    make_client: Callable[..., TestClient], make_embedder: Callable[..., Any], ids: Any
) -> None:
    embedder = make_embedder()
    client = make_client(embedder=embedder, cache_ttl_seconds=60)
    assert hits(search(client, q="record revenue guidance", mode="semantic"))[0] == ids.msft_8k
    search(client, q="record revenue guidance")  # hybrid: another URL, the same query
    search(client, q="record revenue guidance", form_type="8-K")
    assert embedder.queries == ["record revenue guidance"]

    # A busy model doesn't matter for a query embedded recently.
    busy = make_client(embedder=make_embedder(fail=True), cache_ttl_seconds=60)
    body = search(busy, q="record revenue guidance", mode="semantic", limit=1)
    assert (body["mode"], body["degraded"]) == ("semantic", False)
    assert hits(body) == [ids.msft_8k]


def test_query_embeddings_are_cached_only_with_caching_on(
    make_client: Callable[..., TestClient], make_embedder: Callable[..., Any]
) -> None:
    embedder = make_embedder()
    client = make_client(embedder=embedder, cache_ttl_seconds=0)
    search(client, q="cfo", mode="semantic")
    search(client, q="cfo", mode="semantic")
    assert embedder.queries == ["cfo", "cfo"]


def test_query_embedding_cache_fails_open(
    make_client: Callable[..., TestClient], make_embedder: Callable[..., Any]
) -> None:
    dead = api_redis_client("redis://localhost:1/0")  # nothing listens on port 1
    client = make_client(redis_client=dead, cache_ttl_seconds=60)
    body = search(client, q="record revenue guidance", mode="semantic")
    assert (body["mode"], body["degraded"]) == ("semantic", False)


def test_cached_query_vectors_are_compact_and_expire(redis_client: redis.Redis) -> None:
    # Only this cache's keys: the test database is shared with other tests and runs.
    for key in redis_client.scan_iter(match=f"{EMBEDDING_CACHE_PREFIX}*"):
        redis_client.delete(key)
    cache = QueryEmbeddingCache(redis_client, RedisBackoff())
    vector = [0.1] * EMBEDDING_DIMENSIONS
    cache.put("model-a", "cfo", vector)
    assert cache.get("model-a", "cfo") == pytest.approx(vector)
    assert cache.get("model-b", "cfo") is None  # vectors of different models never mix
    key = cache_key("model-a", "cfo")
    assert len(redis_client.get(key) or b"") == 4 * EMBEDDING_DIMENSIONS
    assert 0 < redis_client.ttl(key) <= TTL_SECONDS
    redis_client.set(key, b"garbage")
    assert cache.get("model-a", "cfo") is None


def test_query_is_validated(client: TestClient) -> None:
    assert client.get("/search", params={"q": ""}).status_code == 422
    assert client.get("/search", params={"q": "x", "mode": "fuzzy"}).status_code == 422
