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
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

# Deterministic constraint names, so migrations can refer to them.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


# Embedding size of the default model (nomic-embed-text v1.5). A model with another size needs a
# migration, because pgvector columns have a fixed dimension.
EMBEDDING_DIMENSIONS = 768

# Weighted full-text vector: the headline and company count more than the body.
_SEARCH_TSV = (
    "setweight(to_tsvector('english', title), 'A') || "
    "setweight(to_tsvector('english', company), 'A') || "
    "setweight(to_tsvector('english', body), 'B')"
)


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


class FilingExhibit(Base):
    """An exhibit downloaded with a filing (EX-99 press releases and investor materials only)."""

    __tablename__ = "filing_exhibits"

    accession_no: Mapped[str] = mapped_column(
        sa.ForeignKey("filings.accession_no", ondelete="CASCADE"), primary_key=True
    )
    filename: Mapped[str] = mapped_column(primary_key=True)
    exhibit_type: Mapped[str]  # e.g. "EX-99.1"
    sequence: Mapped[int]  # position in the filing
    description: Mapped[str | None]
    object_key: Mapped[str]


class Form4Filing(Base):
    """Queryable key fields of a parsed Form 4. The full parsed payload is in MongoDB.

    The issuer can differ from the watchlist company the filing was found under: a company's
    filing feed also lists Form 4s it files as an insider of *another* company.
    """

    __tablename__ = "form4_filings"
    __table_args__ = (
        sa.Index("ix_form4_filings_issuer_cik_period", "issuer_cik", "period_of_report"),
    )

    accession_no: Mapped[str] = mapped_column(
        sa.ForeignKey("filings.accession_no", ondelete="CASCADE"), primary_key=True
    )
    period_of_report: Mapped[date]
    # No foreign key: issuers are not always in SEC's ticker map (the companies table).
    issuer_cik: Mapped[int] = mapped_column(sa.BigInteger)
    issuer_name: Mapped[str]
    issuer_ticker: Mapped[str | None]
    reporting_owners: Mapped[list[str]] = mapped_column(ARRAY(sa.Text()))
    transaction_codes: Mapped[list[str]] = mapped_column(ARRAY(sa.Text()))
    is_10b5_1: Mapped[bool | None]
    updated_at: Mapped[datetime] = mapped_column(
        server_default=sa.func.now(), onupdate=sa.func.now()
    )


class Form8KFiling(Base):
    """Queryable key fields of a parsed 8-K: which items it reports. Full text is in MongoDB."""

    __tablename__ = "form8k_filings"

    accession_no: Mapped[str] = mapped_column(
        sa.ForeignKey("filings.accession_no", ondelete="CASCADE"), primary_key=True
    )
    # Item numbers in document order, e.g. {2.02, 9.01} for an earnings release.
    items: Mapped[list[str]] = mapped_column(ARRAY(sa.Text()))
    updated_at: Mapped[datetime] = mapped_column(
        server_default=sa.func.now(), onupdate=sa.func.now()
    )


class Summary(Base):
    """One summary of a filing. History is kept; the most recent row is the current summary.

    Re-summarizing with the same provider, model, and prompt version replaces that row, so replays
    don't pile up duplicates. A new prompt version or model adds a row.
    """

    __tablename__ = "summaries"
    __table_args__ = (
        sa.UniqueConstraint("accession_no", "provider", "model", "prompt_version"),
        sa.Index("ix_summaries_accession_no_created_at", "accession_no", "created_at"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(always=True), primary_key=True)
    accession_no: Mapped[str] = mapped_column(
        sa.ForeignKey("filings.accession_no", ondelete="CASCADE")
    )
    provider: Mapped[str]  # e.g. "template", "ollama", "gemini"
    model: Mapped[str]
    prompt_version: Mapped[int]
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB)  # a FilingSummary
    # Numbers in an LLM summary not found in the text the model was given (grounding check):
    # [] = all found; NULL = not checked (template summaries compute totals by design).
    ungrounded_numbers: Mapped[list[str] | None] = mapped_column(ARRAY(sa.Text()))
    created_at: Mapped[datetime] = mapped_column(server_default=sa.func.now())


class FilingSearch(Base):
    """One searchable document per filing, rebuilt from its current summary."""

    __tablename__ = "filing_search"
    __table_args__ = (sa.Index("ix_filing_search_tsv", "tsv", postgresql_using="gin"),)

    accession_no: Mapped[str] = mapped_column(
        sa.ForeignKey("filings.accession_no", ondelete="CASCADE"), primary_key=True
    )
    summary_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("summaries.id", ondelete="CASCADE")
    )
    title: Mapped[str]  # the summary's headline
    company: Mapped[str]  # company name and ticker
    body: Mapped[str]  # the summary and its key points
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, sa.Computed(_SEARCH_TSV, persisted=True), deferred=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=sa.func.now(), onupdate=sa.func.now()
    )


class FilingEmbedding(Base):
    """A filing's search document as a vector. Keyed by model: vectors from different models
    can't be compared, so a model change re-embeds alongside the old vectors."""

    __tablename__ = "filing_embeddings"
    __table_args__ = (
        sa.Index(
            "ix_filing_embeddings_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    accession_no: Mapped[str] = mapped_column(
        sa.ForeignKey("filings.accession_no", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(primary_key=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    updated_at: Mapped[datetime] = mapped_column(
        server_default=sa.func.now(), onupdate=sa.func.now()
    )


class Channel(StrEnum):
    DISCORD = "discord"
    SLACK = "slack"
    EMAIL = "email"


class NotificationStatus(StrEnum):
    PENDING = "pending"  # not delivered yet (new, or a transient failure that will be retried)
    SENT = "sent"
    FAILED = "failed"  # permanently: the target rejected it (e.g. a deleted webhook)


def _text_enum(enum: type[StrEnum], name: str) -> sa.Enum:
    return sa.Enum(
        enum,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=16,
        values_callable=lambda members: [m.value for m in members],
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(always=True), primary_key=True)
    email: Mapped[str]
    # Google's stable account ID (the ID token's `sub`); emails can change.
    google_sub: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=sa.func.now())


class UserSession(Base):
    """A signed-in browser. The raw session token exists only in the user's cookie; this stores
    its SHA-256 hash, so reading the table doesn't hand out working sessions."""

    __tablename__ = "sessions"
    __table_args__ = (sa.Index("ix_sessions_user_id", "user_id"),)

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE")
    )
    token_hash: Mapped[str] = mapped_column(unique=True)  # hex SHA-256 of the cookie's token
    created_at: Mapped[datetime] = mapped_column(server_default=sa.func.now())
    expires_at: Mapped[datetime]


class Subscription(Base):
    """Alert `target` (a webhook URL or an email address) about a company's new filings."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        # Subscribing twice to the same thing would send every alert twice.
        sa.UniqueConstraint("user_id", "cik", "channel", "target"),
        sa.Index("ix_subscriptions_cik", "cik"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE")
    )
    cik: Mapped[int] = mapped_column(sa.BigInteger, sa.ForeignKey("companies.cik"))
    # Empty means every form type.
    form_types: Mapped[list[str]] = mapped_column(
        ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]")
    )
    channel: Mapped[Channel] = mapped_column(_text_enum(Channel, "channel"))
    target: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=sa.func.now())


class Notification(Base):
    """One alert per (subscription, filing), enforced by the unique constraint: replays and
    redeliveries can never alert the same subscription about the same filing twice."""

    __tablename__ = "notifications"
    __table_args__ = (sa.UniqueConstraint("subscription_id", "accession_no"),)

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(always=True), primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("subscriptions.id", ondelete="CASCADE")
    )
    accession_no: Mapped[str] = mapped_column(
        sa.ForeignKey("filings.accession_no", ondelete="CASCADE")
    )
    status: Mapped[NotificationStatus] = mapped_column(
        _text_enum(NotificationStatus, "status"),
        default=NotificationStatus.PENDING,
        server_default=NotificationStatus.PENDING.value,
    )
    attempts: Mapped[int] = mapped_column(default=0, server_default=sa.text("0"))
    last_error: Mapped[str | None]
    sent_at: Mapped[datetime | None]
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
