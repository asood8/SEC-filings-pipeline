from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def tickers(client: TestClient, **params: str | int) -> list[str]:
    return [c["ticker"] for c in client.get("/companies", params=params).json()["items"]]


def test_lists_companies_by_ticker_with_filters(client: TestClient) -> None:
    assert tickers(client) == ["AAPL", "MSFT", "ZZZZ"]
    assert tickers(client, watchlist="true") == ["AAPL", "MSFT"]
    assert tickers(client, watchlist="false") == ["ZZZZ"]
    assert tickers(client, q="micro") == ["MSFT"]  # name match, case-insensitive
    assert tickers(client, q="aap") == ["AAPL"]


def test_company_pagination(client: TestClient) -> None:
    first = client.get("/companies", params={"limit": 2}).json()
    second = client.get("/companies", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert [c["ticker"] for c in first["items"] + second["items"]] == ["AAPL", "MSFT", "ZZZZ"]
    assert second["next_cursor"] is None


def test_company_detail_with_recent_filings(client: TestClient, ids: Any) -> None:
    detail = client.get("/companies/aapl").json()  # case-insensitive
    assert (detail["cik"], detail["name"], detail["on_watchlist"]) == (320193, "Apple Inc.", True)
    assert [f["accession_no"] for f in detail["recent_filings"]] == [ids.form4, ids.apple_8k]


def test_unknown_company(client: TestClient) -> None:
    assert client.get("/companies/NOPE").status_code == 404
