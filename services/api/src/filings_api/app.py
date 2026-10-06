"""The public read API."""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import Depends, FastAPI, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from filings_common.config import DatabaseSettings
from filings_common.db import Company, Filing, FilingStatus, create_engine, session_factory


class FilingOut(BaseModel):
    accession_no: str
    cik: int
    ticker: str
    company_name: str
    form_type: str
    filed_at: date
    accepted_at: datetime | None
    status: FilingStatus
    sec_url: str


def get_session(request: Request) -> Iterator[Session]:
    sessions: sessionmaker[Session] = request.app.state.sessions
    with sessions() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def create_app(sessions: sessionmaker[Session] | None = None) -> FastAPI:
    """Build the app. Without `sessions`, it connects to DATABASE_URL on startup."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if sessions is not None:
            app.state.sessions = sessions
            yield
            return
        engine = create_engine(DatabaseSettings().database_url.get_secret_value())
        app.state.sessions = session_factory(engine)
        yield
        engine.dispose()

    app = FastAPI(title="SEC Filings API", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/filings")
    def list_filings(
        session: SessionDep,
        cik: int | None = None,
        form_type: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[FilingOut]:
        """Latest filings first, optionally for one company and/or form type."""
        query = (
            sa.select(Filing, Company.ticker, Company.name)
            .join(Company, Company.cik == Filing.cik)
            .order_by(Filing.filed_at.desc(), Filing.accession_no.desc())
            .limit(limit)
        )
        if cik is not None:
            query = query.where(Filing.cik == cik)
        if form_type is not None:
            query = query.where(Filing.form_type == form_type)
        return [
            FilingOut(
                accession_no=filing.accession_no,
                cik=filing.cik,
                ticker=ticker,
                company_name=name,
                form_type=filing.form_type,
                filed_at=filing.filed_at,
                accepted_at=filing.accepted_at,
                status=filing.status,
                sec_url=filing.primary_doc_url,
            )
            for filing, ticker, name in session.execute(query)
        ]

    return app
