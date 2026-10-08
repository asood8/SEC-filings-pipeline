from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from filings_common.db import Filing, Form4Filing, Form4Transaction, session_factory

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


# --- Insider activity ----------------------------------------------------------------------------

MSFT, AAPL = 789019, 320193
TODAY = datetime.now(UTC).date()


def trade(
    line: int, code: str, shares: float, price: float | None, days_ago: int, **extra: Any
) -> dict[str, Any]:
    return {
        "line": line,
        "code": code,
        "shares": shares,
        "price_per_share": price,
        "transaction_date": TODAY - timedelta(days=days_ago),
        "is_derivative": False,
        "security_title": "Common Stock",
        "acquired_disposed": "A" if code in {"P", "A", "M"} else "D",
        "shares_owned_after": 1_000_000,
        "direct_or_indirect": "D",
    } | extra


def add_form4(
    session: Session,
    accession_no: str,
    *,
    found_under: int,
    issuer: int,
    owners: list[str],
    trades: list[dict[str, Any]],
) -> None:
    session.add(
        Filing(
            accession_no=accession_no,
            cik=found_under,
            form_type="4",
            filed_at=TODAY,
            primary_doc_url=f"https://www.sec.gov/Archives/{accession_no}.xml",
            raw_object_key=f"filings/{found_under}/{accession_no}/form4.xml",
        )
    )
    session.flush()
    session.add(
        Form4Filing(
            accession_no=accession_no,
            period_of_report=min(t["transaction_date"] for t in trades),
            issuer_cik=issuer,
            issuer_name="MICROSOFT CORP" if issuer == MSFT else "Apple Inc.",
            issuer_ticker="MSFT" if issuer == MSFT else "AAPL",
            reporting_owners=owners,
            transaction_codes=sorted({t["code"] for t in trades}),
            is_10b5_1=None,
        )
    )
    session.flush()
    session.add_all(Form4Transaction(accession_no=accession_no, **t) for t in trades)


@pytest.fixture
def insider_trades(seeded: Engine) -> None:
    with session_factory(seeded).begin() as session:
        add_form4(
            session,
            "0000789019-26-000101",
            found_under=MSFT,
            issuer=MSFT,
            owners=["NADELLA SATYA"],
            trades=[
                trade(1, "S", 1000, 500.0, days_ago=10),
                trade(2, "S", 500.5, None, days_ago=9),  # price only in a footnote
                trade(3, "A", 9999, 0.0, days_ago=10),  # an award: not a trade
                trade(4, "S", 77, 1.0, days_ago=10, is_derivative=True),  # derivatives left out
            ],
        )
        add_form4(
            session,
            "0000789019-26-000102",
            found_under=MSFT,
            issuer=MSFT,
            owners=["NADELLA SATYA"],
            trades=[trade(1, "S", 100, 510.0, days_ago=3), trade(2, "P", 10, 490.0, days_ago=200)],
        )
        # Found under Apple, but Microsoft stock: counts for Microsoft. Joint filers, one row.
        add_form4(
            session,
            "0000320193-26-000103",
            found_under=AAPL,
            issuer=MSFT,
            owners=["BERKSHIRE HATHAWAY INC", "BUFFETT WARREN E"],
            trades=[trade(1, "P", 200, 480.25, days_ago=5), trade(2, "P", 100, 481.0, days_ago=5)],
        )
        # Found under Microsoft, but Apple stock: doesn't count for Microsoft.
        add_form4(
            session,
            "0000789019-26-000104",
            found_under=MSFT,
            issuer=AAPL,
            owners=["NADELLA SATYA"],
            trades=[trade(1, "P", 5, 250.0, days_ago=4)],
        )


@pytest.mark.usefixtures("insider_trades")
def test_insider_activity_totals_purchases_and_sales_per_owner(client: TestClient) -> None:
    response = client.get("/companies/msft/insider-activity")
    assert response.status_code == 200
    activity = response.json()
    assert (activity["ticker"], activity["days"]) == ("MSFT", 90)
    assert activity["since"] == (datetime.now(UTC).date() - timedelta(days=90)).isoformat()
    nothing = {"shares": 0.0, "value": 0.0, "unpriced_shares": 0.0}
    nadella = {
        "reporting_owners": ["NADELLA SATYA"],
        "bought": nothing,  # the purchase 200 days ago is outside the window
        "sold": {"shares": 1600.5, "value": 551_000.0, "unpriced_shares": 500.5},
        "filings": 2,
        "latest_date": (TODAY - timedelta(days=3)).isoformat(),
    }
    berkshire = {
        "reporting_owners": ["BERKSHIRE HATHAWAY INC", "BUFFETT WARREN E"],
        "bought": {"shares": 300.0, "value": 144_150.0, "unpriced_shares": 0.0},
        "sold": nothing,
        "filings": 1,
        "latest_date": (TODAY - timedelta(days=5)).isoformat(),
    }
    assert activity["owners"] == [nadella, berkshire]  # by value, largest first
    assert (activity["bought"], activity["sold"], activity["filings"]) == (
        berkshire["bought"],
        nadella["sold"],
        3,
    )


@pytest.mark.usefixtures("insider_trades")
def test_insider_activity_window(client: TestClient) -> None:
    year = client.get("/companies/MSFT/insider-activity", params={"days": 365}).json()
    assert year["bought"] == {"shares": 310.0, "value": 149_050.0, "unpriced_shares": 0.0}
    week = client.get("/companies/MSFT/insider-activity", params={"days": 7}).json()
    # Only Nadella's latest sale is this recent, so Berkshire's purchases now lead.
    assert [o["reporting_owners"] for o in week["owners"]] == [
        ["BERKSHIRE HATHAWAY INC", "BUFFETT WARREN E"],
        ["NADELLA SATYA"],
    ]
    assert week["sold"] == {"shares": 100.0, "value": 51_000.0, "unpriced_shares": 0.0}


@pytest.mark.usefixtures("insider_trades")
def test_insider_activity_counts_the_issuers_stock_only(client: TestClient) -> None:
    # The seeded Apple Form 4 is from 2026-09-29, before any 7-day window the tests run in.
    apple = client.get("/companies/AAPL/insider-activity", params={"days": 7}).json()
    [nadella] = apple["owners"]  # Apple stock, though the Form 4 was found under Microsoft
    assert nadella["reporting_owners"] == ["NADELLA SATYA"]
    assert nadella["bought"] == {"shares": 5.0, "value": 1250.0, "unpriced_shares": 0.0}


def test_insider_activity_with_no_trades_and_bad_requests(client: TestClient) -> None:
    empty = client.get("/companies/ZZZZ/insider-activity").json()
    nothing = {"shares": 0.0, "value": 0.0, "unpriced_shares": 0.0}
    assert (empty["owners"], empty["bought"], empty["sold"], empty["filings"]) == (
        [],
        nothing,
        nothing,
        0,
    )
    assert client.get("/companies/NOPE/insider-activity").status_code == 404
    for days in (0, 366):
        response = client.get("/companies/MSFT/insider-activity", params={"days": days})
        assert response.status_code == 422
