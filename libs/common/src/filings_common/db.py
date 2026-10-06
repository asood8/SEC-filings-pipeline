"""Postgres schema (SQLAlchemy models) and database helpers.

Schema changes go through Alembic migrations in `filings_common/migrations/`: edit the models, then
run `uv run alembic revision --autogenerate -m "..."` and review the generated file.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

# Deterministic constraint names, so migrations can refer to them.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 (SQLAlchemy reads this class attribute)
        str: sa.Text(),
        datetime: sa.DateTime(timezone=True),
    }


class FilingStatus(StrEnum):
    DISCOVERED = "discovered"
    PARSED = "parsed"
    READY = "ready"
    NOTIFIED = "notified"
    FAILED = "failed"


class Company(Base):
    __tablename__ = "companies"

    cik: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=False)
    ticker: Mapped[str]
    name: Mapped[str]
    exchange: Mapped[str | None]
    on_watchlist: Mapped[bool] = mapped_column(default=False, server_default=sa.false())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=sa.func.now(), onupdate=sa.func.now()
    )


class Filing(Base):
    __tablename__ = "filings"
    __table_args__ = (sa.Index("ix_filings_cik_filed_at", "cik", "filed_at"),)

    # The natural key everywhere in the pipeline, e.g. 0000320193-24-000123.
    accession_no: Mapped[str] = mapped_column(primary_key=True)
    cik: Mapped[int] = mapped_column(sa.BigInteger, sa.ForeignKey("companies.cik"))
    form_type: Mapped[str]
    filed_at: Mapped[date]  # EDGAR filingDate
    accepted_at: Mapped[datetime | None]  # EDGAR acceptanceDateTime
    primary_doc_url: Mapped[str]
    raw_object_key: Mapped[str]
    status: Mapped[FilingStatus] = mapped_column(
        sa.Enum(
            FilingStatus,
            name="status",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=lambda statuses: [s.value for s in statuses],
        ),
        default=FilingStatus.DISCOVERED,
        server_default=FilingStatus.DISCOVERED.value,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=sa.func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=sa.func.now(), onupdate=sa.func.now()
    )


class OutboxEvent(Base):
    """An event written in the same transaction as the change it announces (transactional outbox).

    The outbox relay publishes unpublished rows in id order and then sets `published_at`.
    """

    __tablename__ = "outbox"
    __table_args__ = (
        sa.Index("ix_outbox_unpublished", "id", postgresql_where=sa.text("published_at IS NULL")),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(always=True), primary_key=True)
    topic: Mapped[str]
    key: Mapped[str]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=sa.func.now())
    published_at: Mapped[datetime | None]


def create_engine(url: str) -> sa.Engine:
    return sa.create_engine(url, pool_pre_ping=True)


def session_factory(engine: sa.Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def alembic_config(url: str | None = None) -> Config:
    """Alembic config for programmatic use. Without a URL, migrations read DATABASE_URL."""
    config = Config()
    config.set_main_option("script_location", "filings_common:migrations")
    if url is not None:
        config.attributes["url"] = url
    return config


def run_migrations(url: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)
