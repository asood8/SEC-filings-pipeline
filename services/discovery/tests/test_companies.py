from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from filings_common.db import Company, session_factory
from filings_discovery.companies import (
    CompanyRecord,
    UnknownTickersError,
    parse_company_tickers_exchange,
    resolve_watchlist,
    sync_companies,
)


def test_parses_by_field_name(company_tickers: dict[str, Any]) -> None:
    company_tickers["fields"] = ["ticker", "exchange", "cik", "name"]
    company_tickers["data"] = [["AAPL", "Nasdaq", 320193, "Apple Inc."]]
    assert parse_company_tickers_exchange(company_tickers) == [
        CompanyRecord(cik=320193, name="Apple Inc.", ticker="AAPL", exchange="Nasdaq")
    ]


def test_resolves_any_share_class_to_its_cik(company_tickers: dict[str, Any]) -> None:
    records = parse_company_tickers_exchange(company_tickers)
    assert resolve_watchlist(records, ["GOOG", "AAPL"]) == {1652044, 320193}


def test_unknown_tickers_are_all_reported(company_tickers: dict[str, Any]) -> None:
    records = parse_company_tickers_exchange(company_tickers)
    with pytest.raises(UnknownTickersError) as excinfo:
        resolve_watchlist(records, ["AAPL", "NOPE", "FAKE"])
    assert excinfo.value.tickers == ["NOPE", "FAKE"]


@pytest.mark.integration
def test_sync_upserts_one_row_per_cik_and_moves_the_watchlist(
    clean_db: Engine, company_tickers: dict[str, Any]
) -> None:
    records = parse_company_tickers_exchange(company_tickers)
    sessions = session_factory(clean_db)
    with sessions.begin() as session:
        assert sync_companies(session, records, {320193, 1652044}) == 5
    with sessions.begin() as session:
        sync_companies(session, records, {789019})

    with sessions() as session:
        rows = {c.cik: c for c in session.scalars(sa.select(Company))}
    assert len(rows) == 5
    assert rows[1652044].ticker == "GOOGL"  # first-listed share class wins
    assert rows[1234567].exchange is None
    assert {cik for cik, c in rows.items() if c.on_watchlist} == {789019}
