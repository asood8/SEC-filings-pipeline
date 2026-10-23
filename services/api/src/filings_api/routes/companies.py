from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.engine import RowMapping

from filings_api.pagination import decode_cursor, encode_cursor
from filings_api.queries import (
    SessionDep,
    company_by_ticker,
    filing_select,
    reporting_owners_out,
    to_filing_out,
)
from filings_api.schemas import (
    CompanyDetail,
    CompanyOut,
    InsiderActivity,
    OwnerActivity,
    Page,
    TradeTotals,
)
from filings_common.db import Company, Filing, Form4Filing, Form4Transaction
from filings_common.names import company_name

router = APIRouter(tags=["companies"])

RECENT_FILINGS = 20
MAX_INSIDER_DAYS = 365
MAX_INSIDER_OWNERS = 50
# Form 4 codes that count as insider buying and selling: open-market or private purchases and
# sales. Grants (A), exercises (M), tax withholding (F), and gifts (G) say little about what an
# insider thinks of the stock.
INSIDER_TRADE_CODES = {"bought": "P", "sold": "S"}


def trade_lines() -> list[sa.ColumnElement[bool]]:
    """Open-market stock trades: exactly the condition of the partial index
    ix_form4_transactions_trades, `code IN ('P', 'S') AND is_derivative IS false`. The codes are
    sent as literals, not bound parameters, so the index stays usable in a prepared statement's
    generic plan too (psycopg prepares a query after five runs on a connection)."""
    codes: sa.BindParameter[list[str]] = sa.bindparam(
        "trade_codes", sorted(INSIDER_TRADE_CODES.values()), expanding=True, literal_execute=True
    )
    return [Form4Transaction.code.in_(codes), Form4Transaction.is_derivative.is_(False)]


def _company_out(company: Company) -> CompanyOut:
    return CompanyOut(
        cik=company.cik,
        ticker=company.ticker,
        name=company_name(company.name),
        exchange=company.exchange,
        industry=company.industry,
        on_watchlist=company.on_watchlist,
    )


@router.get("/companies", summary="Companies, by ticker")
def list_companies(
    session: SessionDep,
    watchlist: bool | None = None,
    q: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: str | None = None,
) -> Page[CompanyOut]:
    """`watchlist=true` limits to followed companies; `q` matches ticker or name."""
    query = sa.select(Company).order_by(Company.ticker, Company.cik)
    if watchlist is not None:
        query = query.where(Company.on_watchlist.is_(watchlist))
    if q is not None:
        pattern = f"%{q.replace('%', r'\%').replace('_', r'\_')}%"
        query = query.where(Company.ticker.ilike(pattern) | Company.name.ilike(pattern))
    if cursor is not None:
        ticker, cik = decode_cursor(cursor, 2)
        query = query.where(
            sa.tuple_(Company.ticker, Company.cik) > sa.tuple_(str(ticker), int(cik))
        )
    companies = session.scalars(query.limit(limit + 1)).all()
    items = [_company_out(c) for c in companies[:limit]]
    more = len(companies) > limit
    return Page(
        items=items, next_cursor=encode_cursor(items[-1].ticker, items[-1].cik) if more else None
    )


@router.get("/companies/{ticker}", summary="One company with its recent filings")
def get_company(session: SessionDep, ticker: str) -> CompanyDetail:
    company = company_by_ticker(session, ticker)
    if company is None:
        raise HTTPException(status_code=404, detail="company not found")
    rows = session.execute(
        filing_select()
        .where(Filing.cik == company.cik)
        .order_by(Filing.filed_at.desc(), Filing.accession_no.desc())
        .limit(RECENT_FILINGS)
    ).all()
    return CompanyDetail(
        **_company_out(company).model_dump(), recent_filings=[to_filing_out(*r) for r in rows]
    )


def _trade_sums(side: str, code: str) -> list[sa.Label[Any]]:
    """SQL sums of one code's lines: shares, value where priced, and shares with no price."""
    line = Form4Transaction
    of_code = line.code == code
    return [
        sa.func.coalesce(sa.func.sum(line.shares).filter(of_code), 0.0).label(f"{side}_shares"),
        sa.func.coalesce(
            sa.func.sum(line.shares * line.price_per_share).filter(of_code), 0.0
        ).label(f"{side}_value"),
        sa.func.coalesce(
            sa.func.sum(line.shares).filter(of_code, line.price_per_share.is_(None)), 0.0
        ).label(f"{side}_unpriced_shares"),
    ]


def _totals(rows: Sequence[RowMapping], side: str) -> TradeTotals:
    """One side's totals over `rows`. Rounded: float sums pick up noise like 0.30000000000000004,
    and nobody trades a ten-thousandth of a share."""
    return TradeTotals(
        shares=round(sum(row[f"{side}_shares"] for row in rows), 4),
        value=round(sum(row[f"{side}_value"] for row in rows), 2),
        unpriced_shares=round(sum(row[f"{side}_unpriced_shares"] for row in rows), 4),
    )


@router.get(
    "/companies/{ticker}/insider-activity",
    summary="Insiders' buying and selling of a company's stock",
)
def get_insider_activity(
    session: SessionDep,
    ticker: str,
    days: Annotated[int, Query(ge=1, le=MAX_INSIDER_DAYS)] = 90,
) -> InsiderActivity:
    """Open-market or private purchases (Form 4 code P) and sales (S) of the company's stock in
    the last `days` days, by transaction date, per reporting owner: filings by the same owners
    (by CIK) share a row, and joint filers share one. Grants, option exercises, tax withholding,
    gifts, and derivative trades are left out.

    Form 4s found under another company count too: what matters is whose stock was traded.
    """
    company = company_by_ticker(session, ticker)
    if company is None:
        raise HTTPException(status_code=404, detail="company not found")
    since = datetime.now(UTC).date() - timedelta(days=days)
    line, form = Form4Transaction, Form4Filing
    traded_on = sa.func.coalesce(line.transaction_date, form.period_of_report)
    # One row per filing; filings are then grouped by their owners. A filing's lines count once
    # however many owners filed it jointly.
    filings = (
        session.execute(
            sa.select(
                form.accession_no,
                form.reporting_owners,
                form.reporting_owner_ciks,
                form.reporting_owner_relationships,
                form.is_10b5_1,
                *(c for side, code in INSIDER_TRADE_CODES.items() for c in _trade_sums(side, code)),
                sa.func.max(traded_on).label("latest_date"),
            )
            .join(form, form.accession_no == line.accession_no)
            .join(Filing, Filing.accession_no == line.accession_no)
            .where(
                form.issuer_cik == company.cik,
                *trade_lines(),
                traded_on >= since,
                # Changes no result (a trade is filed after it happens), but bounds the work by
                # `days` instead of the issuer's whole history.
                Filing.filed_at >= since,
            )
            .group_by(form.accession_no)
        )
        .mappings()
        .all()
    )
    groups: dict[tuple[object, ...], list[RowMapping]] = {}
    for filing in filings:
        groups.setdefault(_owners_key(filing), []).append(filing)
    owners = sorted(
        (_owner_activity(group) for group in groups.values()),
        key=lambda o: (
            -(o.bought.value + o.sold.value),
            -o.latest_date.toordinal(),
            [owner.name for owner in o.reporting_owners],
        ),
    )
    planned = [filing for filing in filings if filing["is_10b5_1"]]
    return InsiderActivity(
        ticker=company.ticker,
        days=days,
        since=since,
        bought=_totals(filings, "bought"),
        sold=_totals(filings, "sold"),
        bought_planned=_totals(planned, "bought"),
        sold_planned=_totals(planned, "sold"),
        filings=len(filings),
        owners=owners[:MAX_INSIDER_OWNERS],
    )


def _owners_key(filing: RowMapping) -> tuple[object, ...]:
    """Who filed, whatever the order or spelling of their names: the set of owner CIKs. Rows
    saved before CIKs were stored fall back to the set of names."""
    ciks: list[int] | None = filing["reporting_owner_ciks"]
    if ciks is not None:
        return ("cik", *sorted(set(ciks)))
    return ("name", *sorted(set(filing["reporting_owners"])))


def _owner_activity(filings: Sequence[RowMapping]) -> OwnerActivity:
    """One set of owners' totals, named as on their latest filing."""
    latest = max(filings, key=lambda f: (f["latest_date"], f["accession_no"]))
    return OwnerActivity(
        reporting_owners=reporting_owners_out(
            latest["reporting_owners"],
            latest["reporting_owner_ciks"],
            latest["reporting_owner_relationships"],
        ),
        bought=_totals(filings, "bought"),
        sold=_totals(filings, "sold"),
        filings=len(filings),
        latest_date=latest["latest_date"],
    )
