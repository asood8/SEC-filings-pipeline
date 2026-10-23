from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from filings_common.db import Filing, Form4Filing, Form4Transaction, session_factory

pytestmark = pytest.mark.integration

MSFT, AAPL = 789019, 320193
# Not in the companies table, and its Form 4s give two tickers: rows get no ticker.
LENNAR = 920760
ISSUERS = {
    MSFT: ("MICROSOFT CORP", "MSFT"),
    AAPL: ("Apple Inc.", "AAPL"),
    LENNAR: ("LENNAR CORP /NEW/", "LEN, LEN.B"),
}
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


def officer(title: str) -> dict[str, Any]:
    return NO_ROLE | {"is_officer": True, "officer_title": title}


# Berkshire Hathaway's and Warren Buffett's are real, from the joint Form 4 fixture
# 0001193125-26-397059, and so are Smith's and Hood's titles; the CIKs of the others, and
# Nadella's and Althoff's relationships, are made up.
NADELLA = Owner(1000001, "NADELLA SATYA", "Satya Nadella", officer("CEO"), "CEO")
SMITH = Owner(
    1000002,
    "SMITH BRADFORD L",
    "Bradford L Smith",
    officer("Vice Chair and President"),
    "Vice Chair and President",
)
HOOD = Owner(1000003, "HOOD AMY", "Amy Hood", officer("EVP, Chief Financial Officer"), "EVP, CFO")
ALTHOFF = Owner(
    1000004,
    "ALTHOFF JUDSON",
    "Judson Althoff",
    NO_ROLE | {"is_other": True, "other_text": "Former officer"},
    "Former officer",
)
TEN_PERCENT_OWNER = NO_ROLE | {"is_ten_percent_owner": True}
BERKSHIRE = Owner(
    1067983, "BERKSHIRE HATHAWAY INC", "Berkshire Hathaway Inc", TEN_PERCENT_OWNER, "10% owner"
)
BUFFETT = Owner(315090, "BUFFETT WARREN E", "Warren E Buffett", TEN_PERCENT_OWNER, "10% owner")

# The Form 4s added here (the seeded Apple sale is moved to 20 days ago).
SALES = "0000789019-26-000301"  # Nadella: sales, plus lines that don't count, and one purchase
JOINT = "0000320193-26-000302"  # Berkshire and Buffett buying Lennar
NEWER = "0000789019-26-000303"  # $20,000 of purchases, 1 day ago
OLDER = "0000789019-26-000305"  # $20,000 too, 4 days ago
UNPRICED = "0000789019-26-000304"  # a purchase priced only in a footnote
STRADDLES = "0000789019-26-000306"  # filed late: purchases 10 and 60 days ago, filed 9 days ago


def days_ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat()


def trade(
    line: int, code: str, shares: float, price: float | None, ago: int, **extra: Any
) -> dict[str, Any]:
    return {
        "line": line,
        "code": code,
        "shares": shares,
        "price_per_share": price,
        "transaction_date": TODAY - timedelta(days=ago),
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
    issuer: int,
    owners: list[Owner],
    trades: list[dict[str, Any]],
    filed_ago: int,
    is_10b5_1: bool | None = None,
    found_under: int = MSFT,
) -> None:
    session.add(
        Filing(
            accession_no=accession_no,
            cik=found_under,
            form_type="4",
            filed_at=TODAY - timedelta(days=filed_ago),
            primary_doc_url=f"https://www.sec.gov/Archives/{accession_no}.xml",
            raw_object_key=f"filings/{found_under}/{accession_no}/form4.xml",
        )
    )
    session.flush()
    name, ticker = ISSUERS[issuer]
    session.add(
        Form4Filing(
            accession_no=accession_no,
            period_of_report=min(t["transaction_date"] for t in trades),
            issuer_cik=issuer,
            issuer_name=name,
            issuer_ticker=ticker,
            reporting_owners=[o.name for o in owners],
            reporting_owner_ciks=[o.cik for o in owners],
            reporting_owner_relationships=[o.relationship for o in owners],
            transaction_codes=sorted({t["code"] for t in trades}),
            is_10b5_1=is_10b5_1,
        )
    )
    session.flush()
    session.add_all(Form4Transaction(accession_no=accession_no, **t) for t in trades)


@pytest.fixture
def insider_trades(seeded: Engine, ids: Any) -> None:
    with session_factory(seeded).begin() as session:
        # The seeded Apple sale (Tim Cook: 30,000 shares at $254.50 and 20,000 priced only in a
        # footnote, under a 10b5-1 plan), moved to dates the windows below always include.
        session.execute(
            sa.update(Form4Transaction)
            .where(Form4Transaction.accession_no == ids.form4)
            .values(transaction_date=TODAY - timedelta(days=20))
        )
        session.execute(
            sa.update(Filing)
            .where(Filing.accession_no == ids.form4)
            .values(filed_at=TODAY - timedelta(days=19))
        )
        add_form4(
            session,
            SALES,
            issuer=MSFT,
            owners=[NADELLA],
            trades=[
                trade(1, "S", 1000, 500.0, ago=3),
                trade(2, "S", 500.5, None, ago=2),  # price only in a footnote
                trade(3, "A", 9999, 0.0, ago=3),  # an award: not a trade
                trade(4, "F", 300, 505.0, ago=3),  # tax withholding: not a trade
                trade(5, "S", 77, 1.0, ago=3, is_derivative=True),  # derivatives left out
                trade(6, "P", 10, 490.0, ago=3),  # a purchase: on the other side
            ],
            filed_ago=1,
            is_10b5_1=True,
        )
        add_form4(
            session,
            JOINT,
            issuer=LENNAR,
            found_under=AAPL,  # any watchlist company's feed can list it
            owners=[BERKSHIRE, BUFFETT],
            trades=[trade(1, "P", 200, 480.25, ago=5), trade(2, "P", 100, 481.0, ago=5)],
            filed_ago=4,
            is_10b5_1=False,
        )
        add_form4(
            session,
            NEWER,
            issuer=MSFT,
            owners=[SMITH],
            trades=[trade(1, "P", 50, 400.0, ago=1, shares_owned_after=50)],  # a first purchase
            filed_ago=0,
        )
        add_form4(
            session,
            OLDER,
            issuer=MSFT,
            owners=[ALTHOFF],
            trades=[trade(1, "P", 40, 500.0, ago=4)],
            filed_ago=3,
        )
        add_form4(
            session,
            UNPRICED,
            issuer=MSFT,
            owners=[HOOD],
            trades=[trade(1, "P", 1000, None, ago=2)],
            filed_ago=1,
        )
        add_form4(
            session,
            STRADDLES,
            issuer=MSFT,
            owners=[SMITH],
            trades=[trade(1, "P", 10_000, 500.0, ago=60), trade(2, "P", 100, 500.0, ago=10)],
            filed_ago=9,
        )


def shown(*owners: Owner) -> list[dict[str, Any]]:
    """Owners as the API returns them."""
    return [
        {"cik": o.cik, "name": o.name, "display_name": o.display_name, "relationship": o.label}
        | o.relationship
        for o in owners
    ]


def trades(client: TestClient, **params: str | int) -> list[dict[str, Any]]:
    response = client.get("/insider-trades", params=params)
    assert response.status_code == 200
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def summary(client: TestClient, **params: str | int) -> list[tuple[str, float, float]]:
    return [(t["accession_no"], t["shares"], t["value"]) for t in trades(client, **params)]


@pytest.mark.usefixtures("insider_trades")
def test_largest_purchases_first_one_row_per_filing(client: TestClient) -> None:
    response = client.get("/insider-trades")  # purchases over 30 days by default
    assert response.status_code == 200
    body = response.json()
    assert (body["side"], body["days"], body["since"]) == ("buy", 30, days_ago(30))
    joint = body["items"][0]
    # Joint filers: one row, each line counted once (300 shares, not 600).
    assert joint == {
        "accession_no": JOINT,
        "filed_at": days_ago(4),
        "transaction_date": days_ago(5),
        "issuer": {"cik": LENNAR, "name": "Lennar Corp", "ticker": None},
        "reporting_owners": shown(BERKSHIRE, BUFFETT),
        "side": "buy",
        "shares": 300.0,
        "value": 144_150.0,
        "average_price": 480.5,
        "unpriced_shares": 0.0,
        "is_10b5_1": False,
        # 1,000,000 held directly after the last purchase, so 999,700 before the 300 bought.
        "direct_holdings_before": 999_700.0,
        "direct_holdings_after": 1_000_000.0,
    }
    # By value; equal values ($20,000), the latest trade first; unpriced filings last. Only the
    # purchase 10 days ago counts from the filing that straddles the window.
    assert [(t["accession_no"], t["shares"], t["value"]) for t in body["items"]] == [
        (JOINT, 300.0, 144_150.0),
        (STRADDLES, 100.0, 50_000.0),
        (NEWER, 50.0, 20_000.0),
        (OLDER, 40.0, 20_000.0),
        (SALES, 10.0, 4900.0),
        (UNPRICED, 1000.0, 0.0),
    ]
    # Who each insider is to the company whose stock they bought.
    assert [[o["relationship"] for o in t["reporting_owners"]] for t in body["items"]] == [
        ["10% owner", "10% owner"],
        ["Vice Chair and President"],
        ["Vice Chair and President"],
        ["Former officer"],
        ["CEO"],
        ["EVP, CFO"],
    ]
    unpriced = body["items"][-1]
    assert (unpriced["average_price"], unpriced["unpriced_shares"]) == (None, 1000.0)
    assert unpriced["issuer"] == {"cik": MSFT, "name": "Microsoft Corp", "ticker": "MSFT"}
    assert unpriced["reporting_owners"] == shown(HOOD)


@pytest.mark.usefixtures("insider_trades")
def test_largest_sales_count_stock_sales_only(client: TestClient, ids: Any) -> None:
    sales = trades(client, side="sell")
    assert [t["accession_no"] for t in sales] == [ids.form4, SALES]
    cook, nadella = sales
    assert (cook["value"], cook["average_price"], cook["unpriced_shares"]) == (
        7_635_000.0,
        254.5,
        20_000.0,
    )
    assert (cook["issuer"]["ticker"], cook["is_10b5_1"]) == ("AAPL", True)
    assert [o["relationship"] for o in cook["reporting_owners"]] == ["Executive Chair, Director"]
    # Only the two stock sales: not the award, the tax withholding, the derivative, or the
    # purchase. The latest of them is the row's date.
    assert {k: nadella[k] for k in ("shares", "value", "average_price", "unpriced_shares")} == {
        "shares": 1500.5,
        "value": 500_000.0,
        "average_price": 500.0,
        "unpriced_shares": 500.5,
    }
    assert (nadella["transaction_date"], nadella["side"], nadella["is_10b5_1"]) == (
        days_ago(2),
        "sell",
        True,
    )


@pytest.mark.usefixtures("insider_trades")
def test_direct_holdings_before_and_after(client: TestClient, ids: Any) -> None:
    def holdings(rows: list[dict[str, Any]], accession_no: str) -> tuple[Any, Any]:
        [row] = [r for r in rows if r["accession_no"] == accession_no]
        return row["direct_holdings_before"], row["direct_holdings_after"]

    sales, purchases = trades(client, side="sell"), trades(client)
    # Nadella's two stock sales, both direct: 1,500.5 sold, 1,000,000 left.
    assert holdings(sales, SALES) == (1_001_500.5, 1_000_000.0)
    assert holdings(purchases, NEWER) == (0.0, 50.0)  # a first purchase
    # Cook's second sale is from an indirect holding, which can't be told apart from others.
    assert holdings(sales, ids.form4) == (None, None)


@pytest.mark.usefixtures("insider_trades")
def test_planned_trades_can_be_left_out(client: TestClient) -> None:
    # Both sales say they were under a 10b5-1 plan, so does Nadella's filing with its small
    # purchase; the other purchases say they weren't or don't say, and those stay.
    response = client.get("/insider-trades", params={"side": "sell", "include_planned": False})
    assert (response.json()["items"], response.json()["include_planned"]) == ([], False)
    everything = summary(client, side="buy")
    assert summary(client, side="buy", include_planned="false") == [
        row for row in everything if row[0] != SALES
    ]
    assert len(everything) == 6


@pytest.mark.usefixtures("insider_trades")
def test_window(client: TestClient, ids: Any) -> None:
    # 90 days: all of the straddling filing's purchases, so it leads.
    assert summary(client, days=90)[:2] == [
        (STRADDLES, 10_100.0, 5_050_000.0),
        (JOINT, 300.0, 144_150.0),
    ]
    assert trades(client, days=90)[0]["transaction_date"] == days_ago(10)
    # 7 days: no straddling filing, and no seeded Apple sale.
    assert [t[0] for t in summary(client, days=7)] == [JOINT, NEWER, OLDER, SALES, UNPRICED]
    assert [t[0] for t in summary(client, side="sell", days=7)] == [SALES]
    assert summary(client, days=1) == [(NEWER, 50.0, 20_000.0)]


@pytest.mark.usefixtures("insider_trades")
def test_limit(client: TestClient) -> None:
    assert [t[0] for t in summary(client, limit=2)] == [JOINT, STRADDLES]


@pytest.mark.usefixtures("insider_trades")
def test_rows_add_up_to_each_companys_insider_activity(client: TestClient) -> None:
    for days in (7, 30, 90):
        for side, totals in (("buy", "bought"), ("sell", "sold")):
            rows = trades(client, side=side, days=days, limit=50)
            for ticker in ("MSFT", "AAPL"):
                activity = client.get(
                    f"/companies/{ticker}/insider-activity", params={"days": days}
                ).json()
                mine = [t for t in rows if t["issuer"]["ticker"] == ticker]
                assert {
                    "shares": sum(t["shares"] for t in mine),
                    "value": sum(t["value"] for t in mine),
                    "unpriced_shares": sum(t["unpriced_shares"] for t in mine),
                } == activity[totals], (days, side, ticker)


def test_no_trades(client: TestClient) -> None:
    # The seeded Apple sale is from 2026-09-29, before any 7-day window the tests run in.
    body = client.get("/insider-trades", params={"side": "sell", "days": 7}).json()
    assert (body["side"], body["days"], body["items"]) == ("sell", 7, [])


def test_bad_requests(client: TestClient) -> None:
    for params in (
        {"side": "hold"},
        {"side": "BUY"},
        {"days": 0},
        {"days": 366},
        {"limit": 0},
        {"limit": 51},
        {"days": "week"},
    ):
        response = client.get("/insider-trades", params=params)
        assert response.status_code == 422, params
