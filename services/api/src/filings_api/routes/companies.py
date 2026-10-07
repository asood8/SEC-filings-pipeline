from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query

from filings_api.pagination import decode_cursor, encode_cursor
from filings_api.queries import SessionDep, company_by_ticker, filing_select, to_filing_out
from filings_api.schemas import CompanyDetail, CompanyOut, Page
from filings_common.db import Company, Filing

router = APIRouter(tags=["companies"])

RECENT_FILINGS = 20


def _company_out(company: Company) -> CompanyOut:
    return CompanyOut(
        cik=company.cik,
        ticker=company.ticker,
        name=company.name,
        exchange=company.exchange,
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
