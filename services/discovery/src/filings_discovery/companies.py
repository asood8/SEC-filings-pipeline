"""Load SEC's ticker ↔ CIK map into the companies table and flag the watchlist."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import batched
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from filings_common.db import Company
from filings_discovery.watchlist import normalize_ticker

_BATCH_SIZE = 1000


@dataclass(frozen=True)
class CompanyRecord:
    cik: int
    name: str
    ticker: str
    exchange: str | None


class UnknownTickersError(ValueError):
    def __init__(self, tickers: Sequence[str]) -> None:
        super().__init__(f"tickers not found in SEC's ticker map: {', '.join(tickers)}")
        self.tickers = list(tickers)


def parse_company_tickers_exchange(document: dict[str, Any]) -> list[CompanyRecord]:
    """Parse company_tickers_exchange.json: {"fields": [...], "data": [[cik, name, ...], ...]}."""
    column = {field: i for i, field in enumerate(document["fields"])}
    return [
        CompanyRecord(
            cik=int(row[column["cik"]]),
            name=row[column["name"]],
            ticker=normalize_ticker(row[column["ticker"]]),
            exchange=row[column["exchange"]] or None,
        )
        for row in document["data"]
    ]


def resolve_watchlist(records: Iterable[CompanyRecord], tickers: Sequence[str]) -> set[int]:
    """CIKs for the watchlist tickers. Raises UnknownTickersError listing any that don't exist."""
    by_ticker = {r.ticker: r.cik for r in records}
    unknown = [t for t in tickers if t not in by_ticker]
    if unknown:
        raise UnknownTickersError(unknown)
    return {by_ticker[t] for t in tickers}


def sync_companies(
    session: Session, records: Sequence[CompanyRecord], watchlist_ciks: set[int]
) -> int:
    """Upsert every company and set `on_watchlist`. Returns the number of companies written.

    Share classes (GOOGL/GOOG) share one CIK; the first one listed becomes the company's ticker.
    """
    unique: dict[int, CompanyRecord] = {}
    for record in records:
        unique.setdefault(record.cik, record)
    for batch in batched(unique.values(), _BATCH_SIZE):
        stmt = pg_insert(Company).values(
            [
                {
                    "cik": r.cik,
                    "ticker": r.ticker,
                    "name": r.name,
                    "exchange": r.exchange,
                    "on_watchlist": r.cik in watchlist_ciks,
                }
                for r in batch
            ]
        )
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=[Company.cik],
                set_={
                    "ticker": stmt.excluded.ticker,
                    "name": stmt.excluded.name,
                    "exchange": stmt.excluded.exchange,
                    "on_watchlist": stmt.excluded.on_watchlist,
                    "updated_at": sa.func.now(),
                },
            )
        )
    # Companies no longer in SEC's map keep their rows (filings reference them), but leave the
    # watchlist if they were removed from it.
    session.execute(
        sa.update(Company)
        .where(Company.on_watchlist, Company.cik.not_in(watchlist_ciks))
        .values(on_watchlist=False, updated_at=sa.func.now())
    )
    return len(unique)
