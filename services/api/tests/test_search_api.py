from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

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


def test_query_is_validated(client: TestClient) -> None:
    assert client.get("/search", params={"q": ""}).status_code == 422
    assert client.get("/search", params={"q": "x", "mode": "fuzzy"}).status_code == 422
