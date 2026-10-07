"""Shared query pieces: a filing joined with its company and current headline, and a company
looked up by ticker."""

from collections.abc import Iterator
from typing import Annotated

import sqlalchemy as sa
from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from filings_api.schemas import FilingOut
from filings_common.db import Company, Filing, FilingSearch
from filings_common.embeddings import Embedder


def get_session(request: Request) -> Iterator[Session]:
    sessions: sessionmaker[Session] = request.app.state.sessions
    with sessions() as session:
        yield session


def get_embedder(request: Request) -> Embedder | None:
    embedder: Embedder | None = request.app.state.embedder
    return embedder


SessionDep = Annotated[Session, Depends(get_session)]
EmbedderDep = Annotated[Embedder | None, Depends(get_embedder)]


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


def company_by_ticker(session: Session, ticker: str) -> Company | None:
    """Case-insensitive, with a dot or a dash as the class separator (BRK.B or BRK-B)."""
    normalized = ticker.strip().upper().replace(".", "-")  # BRK.B -> BRK-B, as SEC writes it
    return session.scalars(
        sa.select(Company).where(Company.ticker == normalized).order_by(Company.cik).limit(1)
    ).first()
