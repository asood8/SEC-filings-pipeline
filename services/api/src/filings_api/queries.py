"""Shared query pieces: a filing joined with its company and current headline, a Form 4's
reporting owners, and a company looked up by ticker."""

from collections.abc import Iterator, Sequence
from typing import Annotated

import sqlalchemy as sa
from fastapi import Depends, Query, Request
from sqlalchemy.orm import Session, sessionmaker

from filings_api.schemas import FilingOut, ReportingOwnerOut
from filings_common.db import Company, Filing, FilingSearch
from filings_common.embeddings import Embedder
from filings_common.names import display_name


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
        company_name=name,
        form_type=filing.form_type,
        filed_at=filing.filed_at,
        accepted_at=filing.accepted_at,
        status=filing.status,
        headline=headline,
        sec_url=filing.primary_doc_url,
    )


def reporting_owners_out(
    names: Sequence[str], ciks: Sequence[int] | None
) -> list[ReportingOwnerOut]:
    """A Form 4's owners from its `form4_filings` row: names as filed, CIKs in the same order
    (NULL on rows saved before CIKs were stored)."""
    known: Sequence[int | None] = (
        ciks if ciks is not None and len(ciks) == len(names) else [None] * len(names)
    )
    return [
        ReportingOwnerOut(cik=cik, name=name, display_name=display_name(name))
        for name, cik in zip(names, known, strict=True)
    ]


def company_by_ticker(session: Session, ticker: str) -> Company | None:
    """Case-insensitive, with a dot or a dash as the class separator (BRK.B or BRK-B)."""
    normalized = ticker.strip().upper().replace(".", "-")  # BRK.B -> BRK-B, as SEC writes it
    return session.scalars(
        sa.select(Company).where(Company.ticker == normalized).order_by(Company.cik).limit(1)
    ).first()
