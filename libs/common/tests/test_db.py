from datetime import date

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.engine import Engine

from filings_common.db import (
    Base,
    Company,
    Filing,
    FilingSearch,
    FilingStatus,
    OutboxEvent,
    create_engine,
    session_factory,
)

pytestmark = pytest.mark.integration


def test_migrations_match_the_models(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], "models changed without a migration: run alembic revision --autogenerate"


def test_migrations_match_the_search_vector_expression(db_engine: Engine) -> None:
    """Autogenerate doesn't compare generated-column expressions, so this compares Postgres's own
    rendering of the model's expression with the migrated column's."""
    computed = FilingSearch.__table__.c.tsv.computed
    assert computed is not None
    model_expression = str(computed.sqltext.compile(dialect=db_engine.dialect))
    expression_of = sa.text(
        "SELECT pg_get_expr(adbin, adrelid) FROM pg_attrdef WHERE adrelid = CAST(:t AS regclass)"
        " AND adnum = (SELECT attnum FROM pg_attribute WHERE attrelid = CAST(:t AS regclass)"
        " AND attname = 'tsv')"
    )
    with db_engine.connect() as conn:
        conn.exec_driver_sql(
            "CREATE TEMPORARY TABLE search_model (title text, company text, ticker text, body text,"
            f" keywords text, tsv tsvector GENERATED ALWAYS AS ({model_expression}) STORED)"
        )
        migrated = conn.scalar(expression_of, {"t": "filing_search"})
        modeled = conn.scalar(expression_of, {"t": "search_model"})
        conn.rollback()
    assert migrated is not None
    assert migrated == modeled


def test_filing_round_trip_with_default_status(clean_db: Engine) -> None:
    sessions = session_factory(clean_db)
    with sessions.begin() as session:
        session.add(Company(cik=320193, ticker="AAPL", name="Apple Inc.", exchange="Nasdaq"))
        session.flush()
        session.add(
            Filing(
                accession_no="0000320193-24-000123",
                cik=320193,
                form_type="8-K",
                filed_at=date(2024, 11, 1),
                primary_doc_url="https://www.sec.gov/Archives/edgar/data/320193/x/doc.htm",
                raw_object_key="filings/320193/0000320193-24-000123/doc.htm",
            )
        )
    with sessions() as session:
        filing = session.get_one(Filing, "0000320193-24-000123")
        assert filing.status is FilingStatus.DISCOVERED
        assert filing.created_at.tzinfo is not None


def test_database_rejects_unknown_status(clean_db: Engine) -> None:
    with clean_db.begin() as conn:
        conn.execute(sa.text("INSERT INTO companies (cik, ticker, name) VALUES (1, 'X', 'X')"))
    with pytest.raises(sa.exc.IntegrityError, match="ck_filings_status"), clean_db.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO filings (accession_no, cik, form_type, filed_at, primary_doc_url,"
                " raw_object_key, status) VALUES ('0000000001-24-000001', 1, '4', '2024-01-01',"
                " 'u', 'k', 'bogus')"
            )
        )


def test_engine_can_keep_parameters_out_of_errors(db_engine: Engine) -> None:
    url = db_engine.url.render_as_string(hide_password=False)
    failing = sa.text("SELECT CAST(:secret AS text), 1 / 0")  # the error itself echoes nothing
    for hide, shown in [(False, True), (True, False)]:
        engine = create_engine(url, hide_parameters=hide)
        try:
            with pytest.raises(sa.exc.DBAPIError) as excinfo, engine.connect() as conn:
                conn.execute(failing, {"secret": "webhook-token"})
        finally:
            engine.dispose()
        assert ("webhook-token" in str(excinfo.value)) is shown


def test_outbox_ids_increase_and_rows_start_unpublished(clean_db: Engine) -> None:
    sessions = session_factory(clean_db)
    with sessions.begin() as session:
        events = [OutboxEvent(topic="t", key=str(i), payload={"i": i}) for i in range(3)]
        session.add_all(events)
    ids = [e.id for e in events]
    assert ids == sorted(ids)
    assert all(e.published_at is None for e in events)
