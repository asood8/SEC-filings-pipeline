"""The discovery job: find new watchlist filings on EDGAR, store them, and announce them.

For each new filing, the raw document (and, for 8-Ks, its EX-99 exhibits) goes to the object
store first. Then the filing row, its exhibit rows, and its `filings.discovered` outbox event are
written in one transaction. Re-running is safe: known accession numbers are skipped, and the insert
ignores conflicts, so no filing or event is ever duplicated.

Tracing: a poll is one trace (a span per company). Each new filing starts a trace of its own,
linked to the company poll that found it, which then follows the filing through the pipeline.
Each recorded filing counts in `filings.discovered`, by form type.
"""

import logging
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date

import sqlalchemy as sa
from opentelemetry import trace
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from filings_common import metrics, telemetry
from filings_common.db import Company, Filing
from filings_common.edgar import EdgarClient, archive_url
from filings_common.events import FilingDiscovered, Topic
from filings_common.outbox import enqueue
from filings_common.storage import ObjectStore
from filings_discovery.exhibits import (
    DownloadedExhibit,
    download_exhibits,
    has_exhibit_rows,
    record_exhibits,
)
from filings_discovery.submissions import FilingRef, industry, raw_document_name, recent_filings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


@dataclass
class DiscoveryResult:
    companies: int = 0
    new_filings: int = 0
    failed_ciks: list[int] = field(default_factory=list)


def filing_prefix(cik: int, accession_no: str) -> str:
    return f"filings/{cik}/{accession_no}"


def object_key(cik: int, accession_no: str, document: str) -> str:
    return f"{filing_prefix(cik, accession_no)}/{document}"


def discover(
    sessions: sessionmaker[Session],
    edgar: EdgarClient,
    store: ObjectStore,
    *,
    form_types: Collection[str],
    since: date,
    exhibit_form_types: Collection[str] = (),
) -> DiscoveryResult:
    """Poll every watchlist company. One company's failure doesn't stop the others."""
    # A run that finds nothing still reports zero: "no new filings" is what the alert looks for.
    metrics.start_at_zero(metrics.FILINGS_DISCOVERED, map(metrics.form_type, form_types))
    with tracer.start_as_current_span("discovery poll") as poll:
        with sessions() as session:
            ciks = session.scalars(
                sa.select(Company.cik).where(Company.on_watchlist).order_by(Company.cik)
            ).all()
        result = DiscoveryResult(companies=len(ciks))
        for cik in ciks:
            with tracer.start_as_current_span(
                "poll company", attributes={telemetry.CIK: cik}
            ) as span:
                try:
                    new = discover_company(
                        sessions,
                        edgar,
                        store,
                        cik,
                        form_types=form_types,
                        since=since,
                        exhibit_form_types=exhibit_form_types,
                    )
                except Exception as exc:
                    logger.exception("discovery failed for CIK %d", cik)
                    result.failed_ciks.append(cik)
                    telemetry.record_error(span, exc)
                    continue
                span.set_attribute("filings.new_filings", new)
                result.new_filings += new
        poll.set_attributes(
            {
                "filings.companies": result.companies,
                "filings.new_filings": result.new_filings,
                "filings.failed_companies": len(result.failed_ciks),
            }
        )
    return result


def discover_company(
    sessions: sessionmaker[Session],
    edgar: EdgarClient,
    store: ObjectStore,
    cik: int,
    *,
    form_types: Collection[str],
    since: date,
    exhibit_form_types: Collection[str] = (),
) -> int:
    """Store and announce one company's new filings. Returns how many were new."""
    submissions = edgar.submissions(cik)
    _record_industry(sessions, cik, industry(submissions))
    candidates = [
        ref
        for ref in recent_filings(submissions)
        if ref.form_type in form_types and ref.filed_at >= since
    ]
    if not candidates:
        return 0
    with sessions() as session:
        known = set(
            session.scalars(
                sa.select(Filing.accession_no).where(
                    Filing.accession_no.in_([ref.accession_no for ref in candidates])
                )
            )
        )
    new = 0
    # Oldest first, so events go out roughly in filing order.
    for ref in sorted(candidates, key=lambda r: (r.filed_at, r.accession_no)):
        if ref.accession_no in known:
            continue
        with_exhibits = ref.form_type in exhibit_form_types
        if _store_and_record(sessions, edgar, store, cik, ref, with_exhibits=with_exhibits):
            new += 1
    logger.info("CIK %d: %d candidate filings, %d new", cik, len(candidates), new)
    return new


def _record_industry(sessions: sessionmaker[Session], cik: int, value: str | None) -> None:
    """Keep the company's industry as SEC describes it; a no-op when unchanged."""
    with sessions.begin() as session:
        session.execute(
            sa.update(Company)
            .where(Company.cik == cik, Company.industry.is_distinct_from(value))
            .values(industry=value)
        )


def _store_and_record(
    sessions: sessionmaker[Session],
    edgar: EdgarClient,
    store: ObjectStore,
    cik: int,
    ref: FilingRef,
    *,
    with_exhibits: bool,
) -> bool:
    with telemetry.filing_trace(
        tracer, "discover filing", ref.accession_no, form_type=ref.form_type, cik=cik
    ) as span:
        recorded = _fetch_and_record(sessions, edgar, store, cik, ref, with_exhibits=with_exhibits)
        span.set_attribute("filings.recorded", recorded)
    if recorded:
        metrics.FILINGS_DISCOVERED.add(1, metrics.form_type(ref.form_type))
    return recorded


def _fetch_and_record(
    sessions: sessionmaker[Session],
    edgar: EdgarClient,
    store: ObjectStore,
    cik: int,
    ref: FilingRef,
    *,
    with_exhibits: bool,
) -> bool:
    document = raw_document_name(ref.primary_document)
    url = archive_url(cik, ref.accession_no, document)
    response = edgar.get(url)
    key = object_key(cik, ref.accession_no, document)
    store.put(key, response.content, content_type=response.headers.get("Content-Type"))
    exhibits: list[DownloadedExhibit] = []
    if with_exhibits:
        prefix = filing_prefix(cik, ref.accession_no)
        exhibits = download_exhibits(edgar, store, cik, ref.accession_no, prefix)

    # The outbox row stores this span's context: the relay continues the trace from here.
    with tracer.start_as_current_span("record filing"), sessions.begin() as session:
        inserted = session.execute(
            pg_insert(Filing)
            .values(
                accession_no=ref.accession_no,
                cik=cik,
                form_type=ref.form_type,
                filed_at=ref.filed_at,
                accepted_at=ref.accepted_at,
                primary_doc_url=url,
                raw_object_key=key,
            )
            .on_conflict_do_nothing(index_elements=[Filing.accession_no])
            .returning(Filing.accession_no)
        ).scalar_one_or_none()
        if inserted is None:  # recorded concurrently, e.g. under another watchlist company
            return False
        record_exhibits(session, ref.accession_no, exhibits)
        enqueue(
            session,
            Topic.FILINGS_DISCOVERED,
            FilingDiscovered(
                accession_no=ref.accession_no,
                cik=cik,
                form_type=ref.form_type,
                filed_at=ref.filed_at,
                raw_object_key=key,
            ),
        )
    return True


def backfill_exhibits(
    sessions: sessionmaker[Session],
    edgar: EdgarClient,
    store: ObjectStore,
    *,
    form_types: Collection[str],
) -> tuple[int, int]:
    """Download EX-99 exhibits for known filings that have none recorded.

    For filings discovered before exhibits were collected. Filings that truly have no EX-99
    exhibits are checked again on each run (one small request each). Returns (filings checked,
    exhibits stored). Re-announce the filings afterwards (`filings-replay discovered`) so they
    are reparsed with their exhibits.
    """
    with sessions() as session:
        filings = [
            (f.cik, f.accession_no)
            for f in session.scalars(
                sa.select(Filing).where(Filing.form_type.in_(list(form_types)))
            )
            if not has_exhibit_rows(session, f.accession_no)
        ]
    stored = 0
    for cik, accession_no in filings:
        exhibits = download_exhibits(
            edgar, store, cik, accession_no, filing_prefix(cik, accession_no)
        )
        with sessions.begin() as session:
            record_exhibits(session, accession_no, exhibits)
        stored += len(exhibits)
    return len(filings), stored
