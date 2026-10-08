"""The largest recent insider purchases or sales, across every company's stock."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Query

from filings_api.queries import SessionDep, reporting_owners_out
from filings_api.routes.companies import INSIDER_TRADE_CODES, MAX_INSIDER_DAYS, trade_lines
from filings_api.schemas import InsiderTrade, InsiderTrades, IssuerOut, TradeSide
from filings_common.db import Company, Filing, Form4Filing, Form4Transaction
from filings_common.names import company_name

router = APIRouter(tags=["insider trades"])

MAX_TRADES = 50
# The same codes as a company's insider activity, so the two always agree.
SIDE_CODES: dict[TradeSide, str] = {
    "buy": INSIDER_TRADE_CODES["bought"],
    "sell": INSIDER_TRADE_CODES["sold"],
}


@router.get("/insider-trades", summary="The largest recent insider purchases or sales")
def list_insider_trades(
    session: SessionDep,
    side: Annotated[TradeSide, Query(description="Purchases or sales.")] = "buy",
    days: Annotated[
        int, Query(ge=1, le=MAX_INSIDER_DAYS, description="How far back, by transaction date.")
    ] = 30,
    limit: Annotated[int, Query(ge=1, le=MAX_TRADES, description="How many filings.")] = 25,
) -> InsiderTrades:
    """Open-market or private purchases (`side=buy`, Form 4 code P) or sales (`side=sell`, code
    S) of any company's stock in the last `days` days, by transaction date, one row per Form 4.
    Grants, option exercises, tax withholding, gifts, and derivative trades are left out.

    The rules are those of `/companies/{ticker}/insider-activity`: one company's rows add up to
    its totals there (when they all fit in `limit`).
    """
    since = datetime.now(UTC).date() - timedelta(days=days)
    line, form = Form4Transaction, Form4Filing
    traded_on = sa.func.coalesce(line.transaction_date, form.period_of_report)
    value = sa.func.coalesce(sa.func.sum(line.shares * line.price_per_share), 0.0)
    # NULL when no line has a price: those filings sort last.
    priced_shares = sa.func.sum(line.shares).filter(line.price_per_share.is_not(None))
    latest = sa.func.max(traded_on)
    # One row per filing, however many owners filed it jointly.
    rows = session.execute(
        sa.select(
            form.accession_no,
            Filing.filed_at,
            form.issuer_cik,
            form.issuer_name,
            Company.ticker,
            form.reporting_owners,
            form.reporting_owner_ciks,
            form.is_10b5_1,
            sa.func.coalesce(sa.func.sum(line.shares), 0.0).label("shares"),
            value.label("value"),
            priced_shares.label("priced_shares"),
            sa.func.coalesce(
                sa.func.sum(line.shares).filter(line.price_per_share.is_(None)), 0.0
            ).label("unpriced_shares"),
            latest.label("latest_date"),
        )
        .select_from(line)
        .join(form, form.accession_no == line.accession_no)
        .join(Filing, Filing.accession_no == form.accession_no)
        # The ticker the company pages use. A Form 4's own can be "BRK.A" or "LEN, LEN.B".
        .outerjoin(Company, Company.cik == form.issuer_cik)
        .where(
            *trade_lines(),
            line.code == SIDE_CODES[side],
            traded_on >= since,
            # A trade is reported after it happens, so only filings filed in the window can
            # have trades in it. The feed's index on `filed_at` finds those, which keeps the
            # work in proportion to `days` rather than to the whole history. (Not
            # `period_of_report`: that is the earliest trade, and a late Form 4 can report
            # year-old lines next to last week's.)
            Filing.filed_at >= since,
        )
        .group_by(form.accession_no, Filing.accession_no, Company.cik)
        .order_by(
            priced_shares.is_(None),
            value.desc(),
            latest.desc(),
            form.accession_no.desc(),
        )
        .limit(limit)
    ).all()
    return InsiderTrades(
        side=side,
        days=days,
        since=since,
        items=[
            InsiderTrade(
                accession_no=row.accession_no,
                filed_at=row.filed_at,
                transaction_date=row.latest_date,
                issuer=IssuerOut(
                    cik=row.issuer_cik, name=company_name(row.issuer_name), ticker=row.ticker
                ),
                reporting_owners=reporting_owners_out(
                    row.reporting_owners, row.reporting_owner_ciks
                ),
                side=side,
                # Rounded like the activity totals: float sums pick up noise.
                shares=round(row.shares, 4),
                value=round(row.value, 2),
                average_price=(
                    round(row.value / row.priced_shares, 4) if row.priced_shares else None
                ),
                unpriced_shares=round(row.unpriced_shares, 4),
                is_10b5_1=row.is_10b5_1,
            )
            for row in rows
        ],
    )
