from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def accessions(page: dict[str, Any]) -> list[str]:
    return [f["accession_no"] for f in page["items"]]


def test_lists_latest_first_with_headlines(client: TestClient, ids: Any) -> None:
    page = client.get("/filings").json()
    assert accessions(page) == [ids.form4, ids.msft_8k, ids.apple_8k]
    assert page["next_cursor"] is None
    first = page["items"][0]
    assert (first["ticker"], first["company_name"], first["form_type"]) == (
        "AAPL",
        "Apple Inc.",
        "4",
    )
    assert first["headline"] == "Tim Cook sold 50,000 AAPL shares"


def test_cursor_pagination_walks_every_filing_once(client: TestClient, ids: Any) -> None:
    seen, cursor = [], None
    while True:
        params: dict[str, Any] = {"limit": 1} | ({"cursor": cursor} if cursor else {})
        page = client.get("/filings", params=params).json()
        seen += accessions(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [ids.form4, ids.msft_8k, ids.apple_8k]


@pytest.mark.parametrize("cursor", ["not-a-cursor", "WzFd"])  # garbage; valid base64 of [1]
def test_bad_cursor_is_a_400(client: TestClient, ids: Any, cursor: str) -> None:
    assert client.get("/filings", params={"cursor": cursor}).status_code == 400


def test_filters(client: TestClient, ids: Any) -> None:
    assert accessions(client.get("/filings", params={"cik": 789019}).json()) == [ids.msft_8k]
    assert accessions(client.get("/filings", params={"form_type": "8-K"}).json()) == [
        ids.msft_8k,
        ids.apple_8k,
    ]


def test_form_4_detail(client: TestClient, ids: Any) -> None:
    detail = client.get(f"/filings/{ids.form4}").json()
    assert detail["summary"]["provider"] == "template"
    assert detail["summary"]["ungrounded_numbers"] is None  # templates aren't checked
    sale = {
        "is_derivative": False,
        "security_title": "Common Stock",
        "transaction_date": "2026-09-29",
        "code": "S",
        "acquired_disposed": "D",
    }
    assert detail["form4"] == {
        "period_of_report": "2026-09-29",
        "issuer_cik": 320193,
        "issuer_name": "Apple Inc.",
        "issuer_ticker": "AAPL",
        "reporting_owners": ["COOK TIMOTHY D"],
        "transaction_codes": ["S"],
        "is_10b5_1": True,
        "transactions": [  # by line
            sale
            | {
                "line": 1,
                "shares": 30000.0,
                "price_per_share": 254.5,
                "shares_owned_after": 3250000.0,
                "direct_or_indirect": "D",
            },
            sale
            | {
                "line": 2,
                "shares": 20000.0,
                "price_per_share": None,
                "shares_owned_after": 3230000.0,
                "direct_or_indirect": "I",
            },
        ],
    }
    assert (detail["items"], detail["exhibits"]) == (None, [])


def test_8k_detail_has_items_exhibits_and_grounding_flags(client: TestClient, ids: Any) -> None:
    detail = client.get(f"/filings/{ids.msft_8k}").json()
    assert detail["items"] == ["2.02", "9.01"]
    assert detail["exhibits"] == [
        {
            "type": "EX-99.1",
            "filename": "ex991.htm",
            "sec_url": "https://www.sec.gov/Archives/edgar/data/789019/000078901926000003/ex991.htm",
        }
    ]
    assert detail["summary"]["ungrounded_numbers"] == ["9.9 billion"]
    assert detail["form4"] is None


def test_unknown_and_malformed_accession_numbers(client: TestClient, ids: Any) -> None:
    assert client.get("/filings/0000000000-26-999999").status_code == 404
    assert client.get("/filings/nope").status_code == 422


def test_limit_is_bounded(client: TestClient, ids: Any) -> None:
    assert client.get("/filings", params={"limit": 0}).status_code == 422
    assert client.get("/filings", params={"limit": 101}).status_code == 422


def test_health_check(client: TestClient, ids: Any) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/filings", "/search?q=cfo"])
def test_out_of_range_ciks_are_refused_cleanly(client: TestClient, path: str) -> None:
    separator = "&" if "?" in path else "?"
    assert client.get(f"{path}{separator}cik=99999999999999999999").status_code == 422
