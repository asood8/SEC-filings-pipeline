"""The discovery job: find new watchlist filings on EDGAR, store them, and announce them.

For each new filing, the raw document goes to the object store first, then the filing row and its
`filings.discovered` outbox event are written in one transaction. Re-running is safe: known
accession numbers are skipped, and the insert ignores conflicts, so no filing or event is ever
duplicated.
"""

import logging
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from filings_common.db import Company, Filing
from filings_common.edgar import EdgarClient, archive_url
from filings_common.events import FilingDiscovered, Topic
from filings_common.outbox import enqueue
from filings_common.storage import ObjectStore
from filings_discovery.submissions import FilingRef, raw_document_name, recent_filings

logger = logging.getLogger(__name__)


@dataclass
class DiscoveryResult:
    companies: int = 0
    new_filings: int = 0
    failed_ciks: list[int] = field(default_factory=list)


def object_key(cik: int, accession_no: str, document: str) -> str:
    return f"filings/{cik}/{accession_no}/{document}"


def discover(
    sessions: sessionmaker[Session],
    edgar: EdgarClient,
    store: ObjectStore,
    *,
    form_types: Collection[str],
    since: date,
) -> DiscoveryResult:
    """Poll every watchlist company. One company's failure doesn't stop the others."""
    with sessions() as session:
        ciks = session.scalars(
            sa.select(Company.cik).where(Company.on_watchlist).order_by(Company.cik)
        ).all()
    result = DiscoveryResult(companies=len(ciks))
    for cik in ciks:
        try:
            result.new_filings += discover_company(
                sessions, edgar, store, cik, form_types=form_types, since=since
            )
        except Exception:
            logger.exception("discovery failed for CIK %d", cik)
            result.failed_ciks.append(cik)
    return result


def discover_company(
    sessions: sessionmaker[Session],
    edgar: EdgarClient,
    store: ObjectStore,
    cik: int,
    *,
    form_types: Collection[str],
    since: date,
) -> int:
    """Store and announce one company's new filings. Returns how many were new."""
    candidates = [
        ref
        for ref in recent_filings(edgar.submissions(cik))
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
        if ref.accession_no not in known and _store_and_record(sessions, edgar, store, cik, ref):
            new += 1
    logger.info("CIK %d: %d candidate filings, %d new", cik, len(candidates), new)
    return new


def _store_and_record(
    sessions: sessionmaker[Session],
    edgar: EdgarClient,
    store: ObjectStore,
    cik: int,
    ref: FilingRef,
) -> bool:
    document = raw_document_name(ref.primary_document)
    url = archive_url(cik, ref.accession_no, document)
    response = edgar.get(url)
    key = object_key(cik, ref.accession_no, document)
    store.put(key, response.content, content_type=response.headers.get("Content-Type"))

    with sessions.begin() as session:
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
