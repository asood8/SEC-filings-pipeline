"""The latest summarized filings as an Atom feed, for feed readers and chat apps that follow
feeds (Slack's RSS app, for one): all watchlist filings, or one company's, or one form type's."""

import uuid
from datetime import UTC, datetime, time
from html import escape
from typing import Annotated, Literal

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request, Response

from filings_api import atom
from filings_api.conditional import conditional_response
from filings_api.queries import CikFilter, ItemFilter, SessionDep, filing_select, reports_item
from filings_common.db import Company, Filing, Summary
from filings_common.names import company_name
from filings_common.parsed import EASTERN
from filings_common.summaries import DISCLAIMER, FilingSummary

router = APIRouter(tags=["feeds"])

FEED_SIZE = 50


def _form_label(form_type: str) -> str:
    return "Form 4" if form_type == "4" else form_type


def _html(summary: FilingSummary, sec_url: str) -> str:
    """The summary's paragraphs and key points, then the filing's link and the disclaimer. Every
    piece is escaped: summaries are model output."""
    parts = [f"<p>{escape(p.strip())}</p>" for p in summary.summary.split("\n\n") if p.strip()]
    if summary.key_points:
        parts.append(
            "<ul>" + "".join(f"<li>{escape(k)}</li>" for k in summary.key_points) + "</ul>"
        )
    parts.append(f'<p><a href="{escape(sec_url)}">The filing on sec.gov</a></p>')
    parts.append(f"<p><small>{escape(DISCLAIMER)}</small></p>")
    return "".join(parts)


@router.get(
    "/feeds/filings.atom",
    summary="Latest filings as an Atom feed",
    response_class=Response,
    responses={
        200: {"content": {"application/atom+xml": {}}, "description": "An Atom feed."},
        404: {"description": "No company with that CIK."},
    },
)
def filings_feed(
    session: SessionDep,
    request: Request,
    cik: CikFilter = None,
    form_type: Annotated[Literal["4", "8-K"] | None, Query()] = None,
    item: ItemFilter = None,
) -> Response:
    """The 50 latest filings that have a summary, newest first, each with its summary, key
    points, and a link to the filing on sec.gov. Paste the URL into a feed reader; `cik`,
    `form_type`, and `item` narrow it to one company, one form, or 8-Ks reporting one item."""
    parts = []
    if cik is not None:
        ticker = session.scalar(sa.select(Company.ticker).where(Company.cik == cik))
        if ticker is None:
            raise HTTPException(status_code=404, detail="company not found")
        parts.append(ticker)
    if item is not None:  # only 8-Ks report items
        parts.append(f"8-K item {item}")
    elif form_type is not None:
        parts.append(f"{_form_label(form_type)}s")
    title = "SEC filings" + (f": {', '.join(parts)}" if parts else "")

    current = (
        sa.select(Summary.summary, Summary.created_at)
        .where(Summary.accession_no == Filing.accession_no)
        .order_by(Summary.created_at.desc(), Summary.id.desc())
        .limit(1)
        .lateral("current_summary")
    )
    query = (
        filing_select()
        .add_columns(current.c.summary, current.c.created_at)
        .join(current, sa.true())  # filings without a summary yet are left out
        .order_by(Filing.filed_at.desc(), Filing.accession_no.desc())
        .limit(FEED_SIZE)
    )
    if cik is not None:
        query = query.where(Filing.cik == cik)
    if form_type is not None:
        query = query.where(Filing.form_type == form_type)
    if item is not None:
        query = query.where(reports_item(item))

    entries = []
    for filing, ticker, name, _, summary_json, summarized_at in session.execute(query).all():
        summary = FilingSummary.model_validate(summary_json)
        published = filing.accepted_at or datetime.combine(filing.filed_at, time(), EASTERN)
        entries.append(
            atom.Entry(
                id=filing.primary_doc_url,  # permanent and unique: EDGAR never moves a filing
                title=f"{ticker} {_form_label(filing.form_type)}: {summary.headline}",
                published=published,
                updated=max(published, summarized_at),
                link=filing.primary_doc_url,
                author=company_name(name),
                category=filing.form_type,
                html=_html(summary, filing.primary_doc_url),
            )
        )
    # Stable whatever host or scheme the request came through, unlike the URL itself.
    view = f"filings?cik={cik or ''}&form_type={form_type or ''}"
    if item is not None:  # appended only when set, so the existing feeds keep their ids
        view += f"&item={item}"
    body = atom.feed(
        entries,
        id=f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'sec-filings:{view}')}",
        title=title,
        self_url=str(request.url),
        updated=max((e.updated for e in entries), default=datetime.now(UTC)),
    )
    return conditional_response(request, body, media_type="application/atom+xml; charset=utf-8")
