"""Upcoming investor calls announced in the watchlist's 8-Ks (docs/adr/0007, "Investor calls"):
as a list for the website, and as an iCalendar feed to subscribe to in a calendar app. Both can
be narrowed to one company with `ticker`. And summaries of past earnings calls' Q&A, from the
companies' own transcripts (ADR-0022): only the summaries and a link, never the transcript."""

import re
from datetime import date, datetime, timedelta
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session, aliased

from filings_api import ical
from filings_api.conditional import conditional_response
from filings_api.queries import SessionDep, company_by_ticker
from filings_api.schemas import (
    CallExchangeOut,
    CallSummaries,
    CallSummaryOut,
    UpcomingCall,
    UpcomingCalls,
)
from filings_common.db import CallSummary, CallTranscript, Company, Filing, Form8KFiling
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


def call_summaries(
    session: Session,
    *,
    cik: int | None = None,
    transcript_id: int | None = None,
    limit: int = 20,
) -> list[CallSummaryOut]:
    """The current (newest) summary of each summarized call, newest call first."""
    current = (
        sa.select(CallSummary)
        .ext(distinct_on(CallSummary.transcript_id))
        .order_by(CallSummary.transcript_id, CallSummary.created_at.desc(), CallSummary.id.desc())
        .subquery()
    )
    summary = aliased(CallSummary, current)
    query = (
        sa.select(summary, CallTranscript, Company.ticker, Company.name)
        .join(CallTranscript, CallTranscript.id == summary.transcript_id)
        .join(Company, Company.cik == CallTranscript.cik)
        .order_by(CallTranscript.call_date.desc(), CallTranscript.id.desc())
        .limit(limit)
    )
    if cik is not None:
        query = query.where(CallTranscript.cik == cik)
    if transcript_id is not None:
        query = query.where(CallTranscript.id == transcript_id)
    return [
        CallSummaryOut(
            id=transcript.id,
            ticker=ticker,
            company_name=company_name(name),
            call_date=transcript.call_date,
            source_url=transcript.source_url,
            headline=row.takeaways["headline"],
            summary=row.takeaways["summary"],
            key_points=row.takeaways.get("key_points", []),
            exchanges=[
                CallExchangeOut(
                    analyst=x["analyst"],
                    firm=x["firm"],
                    question=x.get("question"),
                    answer=x.get("answer"),
                    failed=bool(x.get("failed")),
                    ungrounded_numbers=x.get("ungrounded_numbers", []),
                )
                for x in row.exchanges
            ],
            provider=row.provider,
            model=row.model,
            prompt_version=row.prompt_version,
            created_at=row.created_at,
            ungrounded_numbers=row.ungrounded_numbers,
            ungrounded_names=row.ungrounded_names,
        )
        for row, transcript, ticker, name in session.execute(query).all()
    ]


@router.get("/call-summaries", summary="Earnings-call Q&A summaries")
def list_call_summaries(
    session: SessionDep,
    ticker: Annotated[
        str | None, Query(min_length=1, max_length=20, description="Only this company's calls.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> CallSummaries:
    """Automated summaries of past earnings calls' Q&A, newest call first: the takeaways, then
    each analyst's question and management's answer. Written from the transcripts that
    Microsoft, Alphabet, JPMorgan, Bank of America, J&J, PepsiCo and Nike publish on their own
    sites (ADR-0022); `source_url` links each transcript."""
    company = _company(session, ticker)
    return CallSummaries(
        items=call_summaries(session, cik=company.cik if company is not None else None, limit=limit)
    )


@router.get(
    "/call-summaries/{transcript_id}",
    summary="One earnings call's Q&A summary",
    responses={404: {"description": "No summary of that call."}},
)
def get_call_summary(session: SessionDep, transcript_id: int) -> CallSummaryOut:
    found = call_summaries(session, transcript_id=transcript_id, limit=1)
    if not found:
        raise HTTPException(status_code=404, detail="call summary not found")
    return found[0]
