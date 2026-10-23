"""Upcoming investor calls announced in the watchlist's 8-Ks (docs/adr/0007, "Investor calls"):
as a list for the website, and as an iCalendar feed to subscribe to in a calendar app. Both can
be narrowed to one company with `ticker`."""

import re
from datetime import date, datetime, timedelta
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from filings_api import ical
from filings_api.conditional import conditional_response
from filings_api.queries import SessionDep, company_by_ticker
from filings_api.schemas import UpcomingCall, UpcomingCalls
from filings_common.db import Company, Filing, Form8KFiling
from filings_common.names import company_name
from filings_common.parsed import EASTERN, InvestorCall

router = APIRouter(tags=["calls"])

MAX_DAYS_AHEAD = 120  # the parser ignores announced dates further out than this
FEED_DAYS_BEHIND = 30  # the feed keeps a month of past calls, so calendars don't lose them
_WRITTEN_AT = re.compile(rb"^DTSTAMP:[^\r\n]*\r\n", re.MULTILINE)


def _today() -> date:
    return datetime.now(EASTERN).date()


TickerFilter = Annotated[
    str | None, Query(min_length=1, max_length=20, description="Only this company's calls.")
]


def _company(session: Session, ticker: str | None) -> Company | None:
    if ticker is None:
        return None
    company = company_by_ticker(session, ticker)
    if company is None:
        raise HTTPException(status_code=404, detail="company not found")
    return company


def calls_between(
    session: Session, since: date, until: date, cik: int | None = None
) -> list[UpcomingCall]:
    """One call per company and day, in order (UpcomingCalls.items); one company's with `cik`."""
    day = Form8KFiling.investor_call["day"].astext.cast(sa.Date)
    query = (
        sa.select(
            Form8KFiling.accession_no,
            Form8KFiling.investor_call,
            Filing.accepted_at,
            Filing.filed_at,
            Company.ticker,
            Company.name,
        )
        .join(Filing, Filing.accession_no == Form8KFiling.accession_no)
        .join(Company, Company.cik == Filing.cik)
        .where(Form8KFiling.investor_call.is_not(None), day >= since, day <= until)
    )
    if cik is not None:
        query = query.where(Filing.cik == cik)
    rows = session.execute(query).all()

    def detail(row: Any) -> tuple[bool, datetime | date]:
        # A start time beats none; then the later filing.
        return (row.investor_call.get("starts_at") is not None, row.accepted_at or row.filed_at)

    best: dict[tuple[str, str], Any] = {}
    for row in rows:
        key = (row.ticker, row.investor_call["day"])
        if key not in best or detail(row) > detail(best[key]):
            best[key] = row
    calls = [
        UpcomingCall(
            **InvestorCall.model_validate(row.investor_call).model_dump(),
            ticker=row.ticker,
            company_name=company_name(row.name),
            accession_no=row.accession_no,
        )
        for row in best.values()
    ]
    return sorted(
        calls,
        key=lambda c: (c.day, c.starts_at is None, c.starts_at or datetime.min, c.ticker),
    )


@router.get("/calls", summary="Upcoming investor calls")
def list_calls(
    session: SessionDep,
    days: Annotated[
        int, Query(ge=1, le=MAX_DAYS_AHEAD, description="How many days ahead, from today.")
    ] = 60,
    ticker: TickerFilter = None,
) -> UpcomingCalls:
    """Earnings calls, Q&A sessions, investor days, and conference webcasts that the watchlist's
    8-Ks announce, from today (US Eastern) to `days` ahead. Read from the filings' own words;
    the calls themselves are on the companies' sites."""
    company = _company(session, ticker)
    cik = company.cik if company is not None else None
    since = _today()
    until = since + timedelta(days=days)
    return UpcomingCalls(since=since, until=until, items=calls_between(session, since, until, cik))


@router.get(
    "/calls.ics",
    summary="Investor calls as a calendar feed",
    response_class=Response,
    responses={
        200: {"content": {"text/calendar": {}}, "description": "An iCalendar feed."},
        404: {"description": "No company with that ticker."},
    },
)
def calls_calendar(session: SessionDep, request: Request, ticker: TickerFilter = None) -> Response:
    """The same calls as an iCalendar feed (RFC 5545), for "subscribe by URL" in Google
    Calendar, Apple Calendar, or Outlook: the next 120 days and the last 30. A call with a time
    is an hour long; one announced without a time is an all-day event."""
    company = _company(session, ticker)
    today = _today()
    calls = calls_between(
        session,
        today - timedelta(days=FEED_DAYS_BEHIND),
        today + timedelta(days=MAX_DAYS_AHEAD),
        company.cik if company is not None else None,
    )
    urls = dict(
        session.execute(
            sa.select(Filing.accession_no, Filing.primary_doc_url).where(
                Filing.accession_no.in_([c.accession_no for c in calls])
            )
        ).all()
    )
    events = [
        ical.Event(
            uid=f"{call.accession_no}-{call.day:%Y%m%d}@sec-filings",
            summary=f"{call.ticker} {call.kind.lower()}",
            description="\n".join(
                [
                    f"{call.company_name}: {call.kind.lower()}, as announced in its 8-K.",
                    *([f"Webcast: {call.webcast_url}"] if call.webcast_url else []),
                    f"Filing: {urls.get(call.accession_no, call.accession_no)}",
                ]
            ),
            day=call.day,
            starts_at=call.starts_at,
            url=call.webcast_url,
        )
        for call in calls
    ]
    name = "SEC filings: investor calls"
    if company is not None:
        name = f"SEC filings: {company.ticker} investor calls"
    body = ical.calendar(events, name=name, now=datetime.now(EASTERN)).encode()
    return conditional_response(
        request,
        body,
        media_type="text/calendar; charset=utf-8",
        # DTSTAMP is when the feed was written, so it changes on every request: not a new version.
        version=_WRITTEN_AT.sub(b"", body),
        headers={"Content-Disposition": 'inline; filename="investor-calls.ics"'},
    )
