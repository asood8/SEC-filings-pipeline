"""Shared query pieces: a filing joined with its company and current headline, a Form 4's
reporting owners, and a company looked up by ticker."""

from collections.abc import Iterator, Mapping, Sequence
from datetime import date
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session, sessionmaker

from filings_api.schemas import FilingOut, ReportingOwnerOut
from filings_common.db import Company, Filing, FilingSearch, Form8KFiling
from filings_common.embeddings import Embedder
from filings_common.names import company_name, display_name, relationship_label
from filings_common.parsed import OwnerRelationship


def get_session(request: Request) -> Iterator[Session]:
    sessions: sessionmaker[Session] = request.app.state.sessions
    with sessions() as session:
        yield session


def get_embedder(request: Request) -> Embedder | None:
    embedder: Embedder | None = request.app.state.embedder
    return embedder


SessionDep = Annotated[Session, Depends(get_session)]
EmbedderDep = Annotated[Embedder | None, Depends(get_embedder)]
# CIKs have at most 10 digits; a bigger number would overflow the bigint column (a 500).
CikFilter = Annotated[int | None, Query(ge=1, le=9_999_999_999)]
ItemFilter = Annotated[
    str | None,
    Query(
        pattern=r"^\d\.\d{2}$",
        description="Only 8-Ks reporting this item, e.g. 2.02 (results) or 5.02 (officer changes).",
    ),
]
Since = Annotated[date | None, Query(description="Only filings filed on or after this date.")]
Until = Annotated[date | None, Query(description="Only filings filed on or before this date.")]


def reports_item(item: str) -> sa.ColumnElement[bool]:
    """The filing is an 8-K that reports `item`."""
    return sa.exists().where(
        Form8KFiling.accession_no == Filing.accession_no, Form8KFiling.items.contains([item])
    )


def check_dates(since: date | None, until: date | None) -> None:
    if since is not None and until is not None and since > until:
        raise HTTPException(status_code=422, detail="since is after until")


def filing_select() -> sa.Select[Filing, str, str, str | None]:
    return (
        sa.select(Filing, Company.ticker, Company.name, FilingSearch.title)
        .join(Company, Company.cik == Filing.cik)
        .outerjoin(FilingSearch, FilingSearch.accession_no == Filing.accession_no)
    )


def to_filing_out(filing: Filing, ticker: str, name: str, headline: str | None) -> FilingOut:
    return FilingOut(
        accession_no=filing.accession_no,
        cik=filing.cik,
        ticker=ticker,
        company_name=company_name(name),
        form_type=filing.form_type,
        filed_at=filing.filed_at,
        accepted_at=filing.accepted_at,
        status=filing.status,
        headline=headline,
        sec_url=filing.primary_doc_url,
    )


def reporting_owners_out(
    names: Sequence[str],
    ciks: Sequence[int] | None,
    relationships: Sequence[Mapping[str, Any]] | None,
) -> list[ReportingOwnerOut]:
    """A Form 4's owners from its `form4_filings` row: names as filed, then CIKs and
    relationships in the same order (each NULL on rows saved before it was stored)."""
    return [
        ReportingOwnerOut(
            cik=cik, name=name, display_name=display_name(name), **_relationship_fields(role)
        )
        for name, cik, role in zip(
            names, _per_owner(ciks, names), _per_owner(relationships, names), strict=True
        )
    ]


def _per_owner[T](values: Sequence[T] | None, names: Sequence[str]) -> Sequence[T | None]:
    return values if values is not None and len(values) == len(names) else [None] * len(names)


def _relationship_fields(stored: Mapping[str, Any] | None) -> dict[str, Any]:
    if stored is None:
        return dict.fromkeys(["relationship", *OwnerRelationship.model_fields])
    relationship = OwnerRelationship.model_validate(stored)
    return {"relationship": relationship_label(relationship), **relationship.model_dump()}


def company_by_ticker(session: Session, ticker: str) -> Company | None:
    """Case-insensitive, with a dot or a dash as the class separator (BRK.B or BRK-B)."""
    normalized = ticker.strip().upper().replace(".", "-")  # BRK.B -> BRK-B, as SEC writes it
    return session.scalars(
        sa.select(Company).where(Company.ticker == normalized).order_by(Company.cik).limit(1)
    ).first()
