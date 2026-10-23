from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from filings_common.db import Company, Filing, Form4Filing, Form4Transaction, session_factory

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


def test_companies_carry_their_industry(client: TestClient, seeded: Engine) -> None:
    with session_factory(seeded).begin() as session:
        session.get_one(Company, 320193).industry = "Electronic Computers"
    industries = {c["ticker"]: c["industry"] for c in client.get("/companies").json()["items"]}
    assert industries == {"AAPL": "Electronic Computers", "MSFT": None, "ZZZZ": None}
    assert client.get("/companies/AAPL").json()["industry"] == "Electronic Computers"


def test_unknown_company(client: TestClient) -> None:
    assert client.get("/companies/NOPE").status_code == 404


# --- Insider activity ----------------------------------------------------------------------------

MSFT, AAPL = 789019, 320193
TODAY = datetime.now(UTC).date()

NO_ROLE = {
    "is_director": False,
    "is_officer": False,
    "is_ten_percent_owner": False,
    "is_other": False,
    "officer_title": None,
    "other_text": None,
}


class Owner(NamedTuple):
    cik: int
    name: str  # as filed
    display_name: str
    relationship: dict[str, Any]  # as stored in form4_filings.reporting_owner_relationships
    label: str  # the API's `relationship`


# Berkshire Hathaway's and Warren Buffett's are real, from the joint Form 4 fixture
# 0001193125-26-397059; Nadella's CIK and title are made up.
NADELLA = Owner(
    1000001,
    "NADELLA SATYA",
    "Satya Nadella",
    NO_ROLE | {"is_director": True, "is_officer": True, "officer_title": "Chief Executive Officer"},
    "CEO, Director",
)
TEN_PERCENT_OWNER = NO_ROLE | {"is_ten_percent_owner": True}
BERKSHIRE = Owner(
    1067983, "BERKSHIRE HATHAWAY INC", "Berkshire Hathaway Inc", TEN_PERCENT_OWNER, "10% owner"
)
BUFFETT = Owner(315090, "BUFFETT WARREN E", "Warren E Buffett", TEN_PERCENT_OWNER, "10% owner")


def shown(*owners: Owner, ciks: bool = True, roles: bool = True) -> list[dict[str, Any]]:
    """Owners as the API returns them. `ciks` or `roles` False: from a Form 4 saved before they
    were stored."""
    return [
        {
            "cik": o.cik if ciks else None,
            "name": o.name,
            "display_name": o.display_name,
            "relationship": o.label if roles else None,
        }
        | (o.relationship if roles else dict.fromkeys(o.relationship))
        for o in owners
    ]


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
    owners: list[Owner],
    trades: list[dict[str, Any]],
    ciks: bool = True,  # False: saved before owner CIKs were stored
    roles: bool = True,  # False: saved before owner relationships were stored
    is_10b5_1: bool | None = None,
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
            reporting_owners=[o.name for o in owners],
            reporting_owner_ciks=[o.cik for o in owners] if ciks else None,
            reporting_owner_relationships=[o.relationship for o in owners] if roles else None,
            transaction_codes=sorted({t["code"] for t in trades}),
            is_10b5_1=is_10b5_1,
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
            owners=[NADELLA],
            trades=[
                trade(1, "S", 1000, 500.0, days_ago=10),
                trade(2, "S", 500.5, None, days_ago=9),  # price only in a footnote
                trade(3, "A", 9999, 0.0, days_ago=10),  # an award: not a trade
                trade(4, "S", 77, 1.0, days_ago=10, is_derivative=True),  # derivatives left out
            ],
            is_10b5_1=True,
        )
        add_form4(
            session,
            "0000789019-26-000102",
            found_under=MSFT,
            issuer=MSFT,
            owners=[NADELLA],
            trades=[trade(1, "S", 100, 510.0, days_ago=3), trade(2, "P", 10, 490.0, days_ago=200)],
        )
        # Found under Apple, but Microsoft stock: counts for Microsoft. Joint filers, one row.
        add_form4(
            session,
            "0000320193-26-000103",
            found_under=AAPL,
            issuer=MSFT,
            owners=[BERKSHIRE, BUFFETT],
            trades=[trade(1, "P", 200, 480.25, days_ago=5), trade(2, "P", 100, 481.0, days_ago=5)],
        )
        # Found under Microsoft, but Apple stock: doesn't count for Microsoft.
        add_form4(
            session,
            "0000789019-26-000104",
            found_under=MSFT,
            issuer=AAPL,
            owners=[NADELLA],
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
        "reporting_owners": shown(NADELLA),
        "bought": nothing,  # the purchase 200 days ago is outside the window
        "sold": {"shares": 1600.5, "value": 551_000.0, "unpriced_shares": 500.5},
        "filings": 2,
        "latest_date": (TODAY - timedelta(days=3)).isoformat(),
    }
    berkshire = {
        "reporting_owners": shown(BERKSHIRE, BUFFETT),
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
    # Nadella's first filing says its sales were under a 10b5-1 plan; the second doesn't say.
    assert activity["sold_planned"] == {
        "shares": 1500.5,
        "value": 500_000.0,
        "unpriced_shares": 500.5,
    }
    assert activity["bought_planned"] == nothing


@pytest.mark.usefixtures("insider_trades")
def test_insider_activity_window(client: TestClient) -> None:
    year = client.get("/companies/MSFT/insider-activity", params={"days": 365}).json()
    assert year["bought"] == {"shares": 310.0, "value": 149_050.0, "unpriced_shares": 0.0}
    week = client.get("/companies/MSFT/insider-activity", params={"days": 7}).json()
    # Only Nadella's latest sale is this recent, so Berkshire's purchases now lead.
    assert [o["reporting_owners"] for o in week["owners"]] == [
        shown(BERKSHIRE, BUFFETT),
        shown(NADELLA),
    ]
    assert week["sold"] == {"shares": 100.0, "value": 51_000.0, "unpriced_shares": 0.0}


@pytest.mark.usefixtures("insider_trades")
def test_insider_activity_counts_the_issuers_stock_only(client: TestClient) -> None:
    # The seeded Apple Form 4 is from 2026-09-29, before any 7-day window the tests run in.
    apple = client.get("/companies/AAPL/insider-activity", params={"days": 7}).json()
    [nadella] = apple["owners"]  # Apple stock, though the Form 4 was found under Microsoft
    assert nadella["reporting_owners"] == shown(NADELLA)
    assert nadella["bought"] == {"shares": 5.0, "value": 1250.0, "unpriced_shares": 0.0}


def test_insider_activity_groups_filings_by_who_filed_them(
    seeded: Engine, client: TestClient
) -> None:
    buffett_alone = BUFFETT._replace(name="Buffett Warren E")
    with session_factory(seeded).begin() as session:

        def add(number: int, owners: list[Owner], line: dict[str, Any], ciks: bool = True) -> None:
            add_form4(
                session,
                f"0000789019-26-000{number}",
                found_under=MSFT,
                issuer=MSFT,
                owners=owners,
                trades=[line],
                ciks=ciks,
                roles=ciks,  # saved before CIKs, so before relationships too
            )

        add(201, [BERKSHIRE, BUFFETT], trade(1, "P", 100, 480.0, days_ago=6))
        # The same owners listed the other way round: one row, named as on the latest filing.
        add(202, [BUFFETT, BERKSHIRE], trade(1, "P", 50, 482.0, days_ago=2))
        # Buffett alone, his name typed another way: a row of his own, with the same CIK.
        add(203, [buffett_alone], trade(1, "P", 20, 485.0, days_ago=4))
        # Saved before owner CIKs were stored: grouped by name.
        add(204, [NADELLA], trade(1, "S", 5, 500.0, days_ago=3), ciks=False)
        add(205, [NADELLA], trade(1, "S", 5, 500.0, days_ago=1), ciks=False)

    activity = client.get("/companies/MSFT/insider-activity").json()
    rows = [
        (o["reporting_owners"], o["filings"], o["bought"]["value"], o["sold"]["value"])
        for o in activity["owners"]
    ]
    assert rows == [
        (shown(BUFFETT, BERKSHIRE), 2, 72_100.0, 0.0),
        (shown(buffett_alone), 1, 9700.0, 0.0),
        (shown(NADELLA, ciks=False, roles=False), 2, 0.0, 5000.0),
    ]
    # Joint filings count once, in the totals as in their row.
    assert (activity["bought"]["shares"], activity["filings"]) == (170.0, 5)


def test_insider_activity_shows_relationships_as_on_the_latest_filing(
    seeded: Engine, client: TestClient
) -> None:
    # Bradford L Smith's CIK and titles are made up.
    director = Owner(
        1000002, "SMITH BRADFORD L", "Bradford L Smith", NO_ROLE | {"is_director": True}, "Director"
    )
    promoted = director._replace(
        relationship=NO_ROLE | {"is_officer": True, "officer_title": "Vice Chair and President"},
        label="Vice Chair and President",
    )
    with session_factory(seeded).begin() as session:

        def add(number: int, owner: Owner, days_ago: int, roles: bool = True) -> None:
            add_form4(
                session,
                f"0000789019-26-000{number}",
                found_under=MSFT,
                issuer=MSFT,
                owners=[owner],
                trades=[trade(1, "S", 10, 500.0, days_ago=days_ago)],
                roles=roles,
            )

        add(301, director, days_ago=6)
        add(302, promoted, days_ago=2)
        add(303, NADELLA, days_ago=5)
        # Nadella's latest was saved before relationships were stored (until the rebuild).
        add(304, NADELLA, days_ago=1, roles=False)

    activity = client.get("/companies/MSFT/insider-activity").json()
    assert [(o["reporting_owners"], o["filings"]) for o in activity["owners"]] == [
        (shown(NADELLA, roles=False), 2),  # the latest trade first, on equal values
        (shown(promoted), 2),
    ]


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
