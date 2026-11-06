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


def _without_ticker(column: str) -> str:
    """SQL for the column's text with the ticker blanked out where it stands alone, as written
    (in capitals): the English configuration would index "COST" as the word "cost", which "costs"
    matches. The ticker's characters are escaped for the regular expression."""
    pattern = r"'\m' || regexp_replace(ticker, '\W', '\\\&', 'g') || '\M'"
    return f"coalesce(regexp_replace({column}, {pattern}, ' ', 'g'), {column})"


# The ticker as one lexeme in capitals, written as tsvector input ('COST':1, quotes and backslashes
# escaped): normalized words are lowercase, so no word can match it, only a search for the ticker
# (filings_common.search). ("\:" is a literal colon in SQLAlchemy text, where ":1" would be a bind
# parameter.)
_TICKER_LEXEME = (
    r"setweight((''''"
    r" || replace(replace(upper(ticker), '\', '\\'), '''', '''''')"
    r" || '''\:1')::tsvector, 'A')"
)

# Weighted full-text vector: the headline, company, and ticker count more than the body and the
# keywords (other words for what the text says, which weigh what the words they stand for weigh).
# Migration 23e9e08283f1 has a copy, and test_db checks that the two agree.
_SEARCH_TSV = (
    f"setweight(to_tsvector('english', {_without_ticker('title')}), 'A') || "
    "setweight(to_tsvector('english', company), 'A') || "
    f"coalesce({_TICKER_LEXEME}, '') || "
    f"setweight(to_tsvector('english', {_without_ticker('body')}), 'B') || "
    "setweight(to_tsvector('english', keywords), 'B')"
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
    # SEC's description of the company's SIC code ("Electronic Computers"), from the submissions
    # API that discovery reads anyway; NULL until discovery has polled the company.
    industry: Mapped[str | None]
    on_watchlist: Mapped[bool] = mapped_column(default=False, server_default=sa.false())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=sa.func.now(), onupdate=sa.func.now()
    )


class Filing(Base):
    __tablename__ = "filings"
    __table_args__ = (
        # A company's filings, newest first (company pages, the feed filtered by company).
        sa.Index("ix_filings_cik_filed_at", "cik", "filed_at"),
        # The feed's order (GET /filings and its cursors): without it, each page sorts every filing.
        sa.Index("ix_filings_filed_at_accession_no", "filed_at", "accession_no"),
        # A filing's amendments (filings_parser.amendments).
        sa.Index("ix_filings_amends_accession_no", "amends_accession_no"),
    )

    # The natural key everywhere in the pipeline, e.g. 0000320193-24-000123.
    accession_no: Mapped[str] = mapped_column(primary_key=True)
    cik: Mapped[int] = mapped_column(sa.BigInteger, sa.ForeignKey("companies.cik"))
    # EDGAR form code: 4, 4/A (an amended Form 4), 8-K, 8-K/A.
    form_type: Mapped[str]
    filed_at: Mapped[date]  # EDGAR filingDate
    accepted_at: Mapped[datetime | None]  # EDGAR acceptanceDateTime
    # EDGAR reportDate: a Form 4's period of report, an 8-K's date of the earliest event it
    # reports. NULL when EDGAR gives none, and on rows discovered before it was stored until
    # discovery sees them again.
    report_date: Mapped[date | None]
    primary_doc_url: Mapped[str]
    raw_object_key: Mapped[str]
    # Amendments only (4/A, 8-K/A), written by the parser (filings_parser.amendments): the
    # filing date of the filing this amends, as the amendment states it, and that filing once
    # found. The link always points at the original Form 4 or 8-K, never at another amendment.
    # NULL when the amendment gives no date, or the original isn't collected (it may predate
    # the watchlist) or can't be told apart from another filing.
    amends_filed_at: Mapped[date | None]
    amends_accession_no: Mapped[str | None] = mapped_column(
        sa.ForeignKey("filings.accession_no", ondelete="SET NULL")
    )
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
    """Queryable key fields of a parsed Form 4, with its transaction lines in
    `form4_transactions`. The full parsed payload is in MongoDB.

    The issuer can differ from the watchlist company the filing was found under: a company's
    filing feed also lists Form 4s it files as an insider of *another* company.
    """

    __tablename__ = "form4_filings"
    __table_args__ = (
        sa.Index("ix_form4_filings_issuer_cik_period", "issuer_cik", "period_of_report"),
        # One relationship per owner. (jsonb_array_length also rejects a value that isn't an
        # array.)
        sa.CheckConstraint(
            "jsonb_array_length(reporting_owner_relationships) = cardinality(reporting_owners)",
            name="reporting_owner_relationships_length",
        ),
    )

    accession_no: Mapped[str] = mapped_column(
        sa.ForeignKey("filings.accession_no", ondelete="CASCADE"), primary_key=True
    )
    period_of_report: Mapped[date]
    # No foreign key: issuers are not always in SEC's ticker map (the companies table).
    issuer_cik: Mapped[int] = mapped_column(sa.BigInteger)
    issuer_name: Mapped[str]
    issuer_ticker: Mapped[str | None]
    reporting_owners: Mapped[list[str]] = mapped_column(ARRAY(sa.Text()))  # names, as filed
    # The owners' CIKs, in the same order as their names. NULL on rows saved before CIKs were
    # stored, until `filings-rebuild-key-fields` rewrites them.
    reporting_owner_ciks: Mapped[list[int] | None] = mapped_column(ARRAY(sa.BigInteger()))
    # Each owner's relationship to the issuer, in the same order: a JSON array of
    # filings_common.parsed.OwnerRelationship objects ({"is_director": true, "is_officer": false,
    # ..., "officer_title": null, "other_text": null}). NULL on rows saved before relationships
    # were stored, until `filings-rebuild-key-fields` rewrites them. (none_as_null: None is SQL
    # NULL, not the JSON value null, which the check constraint would reject.)
    reporting_owner_relationships: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    transaction_codes: Mapped[list[str]] = mapped_column(ARRAY(sa.Text()))
    is_10b5_1: Mapped[bool | None]
    updated_at: Mapped[datetime] = mapped_column(
        server_default=sa.func.now(), onupdate=sa.func.now()
    )


class Form4Transaction(Base):
    """One transaction line of a parsed Form 4, from either table: non-derivative (stock) or
    derivative (options, RSUs, convertibles). Holdings rows (positions reported without a
    transaction), footnotes, and derivative terms stay in the MongoDB payload.

    `line` numbers the payload's transactions from 1: the non-derivative table first, then the
    derivative table, each in document order. The parser rewrites a filing's lines together with
    its `form4_filings` row, so a replay leaves exactly the same rows.

    A Form 4/A's lines are saved like any Form 4's. Each one that restates a line of the
    amended filing marks that line `superseded_by` the amendment (filings_parser.amendments),
    and totals count only lines not superseded.
    """

    __tablename__ = "form4_transactions"
    __table_args__ = (
        sa.CheckConstraint("acquired_disposed IN ('A', 'D')", name="acquired_disposed"),
        sa.CheckConstraint("direct_or_indirect IN ('D', 'I')", name="direct_or_indirect"),
        # Insider buying and selling (GET /companies/{ticker}/insider-activity): open-market
        # stock purchases and sales only, the lines it totals, so the planner probes these per
        # filing instead of scanning every line. Its condition must match that query's.
        sa.Index(
            "ix_form4_transactions_trades",
            "accession_no",
            postgresql_where=sa.text(
                "code IN ('P', 'S') AND is_derivative IS false AND superseded_by IS NULL"
            ),
        ),
    )

    # Deleting a filing deletes its form4_filings row, and with it these lines.
    accession_no: Mapped[str] = mapped_column(
        sa.ForeignKey("form4_filings.accession_no", ondelete="CASCADE"), primary_key=True
    )
    line: Mapped[int] = mapped_column(sa.SmallInteger, primary_key=True, autoincrement=False)
    is_derivative: Mapped[bool]
    security_title: Mapped[str]
    transaction_date: Mapped[date | None]
    # SEC transaction code: P open-market purchase, S sale, A award, M exercise, F tax, G gift, ...
    code: Mapped[str | None]
    # Floats, like the payload (ADR-0006): reported figures, not accounting balances.
    shares: Mapped[float | None] = mapped_column(sa.Double)
    # NULL when the filing gives the price only in a footnote.
    price_per_share: Mapped[float | None] = mapped_column(sa.Double)
    acquired_disposed: Mapped[str | None]  # A or D
    shares_owned_after: Mapped[float | None] = mapped_column(sa.Double)
    direct_or_indirect: Mapped[str | None]  # D or I
    # How an indirect holding is held, as filed ("By Spouse", "By Trust"). NULL for direct
    # holdings, and on rows saved before it was stored until `filings-rebuild-key-fields`.
    nature_of_ownership: Mapped[str | None]
    # The Form 4/A whose line restates this one; NULL while this line is current.
    superseded_by: Mapped[str | None] = mapped_column(
        sa.ForeignKey("form4_filings.accession_no", ondelete="SET NULL")
    )


class Form8KFiling(Base):
    """Queryable key fields of a parsed 8-K: which items it reports, and the call it announces.
    Full text is in MongoDB."""

    __tablename__ = "form8k_filings"

    accession_no: Mapped[str] = mapped_column(
        sa.ForeignKey("filings.accession_no", ondelete="CASCADE"), primary_key=True
    )
    # Item numbers in document order, e.g. {2.02, 9.01} for an earnings release.
    items: Mapped[list[str]] = mapped_column(ARRAY(sa.Text()))
    # The upcoming call the 8-K announces, if any (filings_common.parsed.InvestorCall as JSON).
    investor_call: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=sa.func.now(), onupdate=sa.func.now()
    )


class CallTranscript(Base):
    """An earnings-call transcript a company published on its own site (ADR-0022). The text is in
    MongoDB (`call_transcripts`, keyed by the source URL) and is never served: only summaries of
    it and the link are."""

    __tablename__ = "call_transcripts"
    # One transcript per company and call day: a listing's links can move, the call doesn't.
    __table_args__ = (
        sa.Index("ix_call_transcripts_cik_call_date", "cik", "call_date", unique=True),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(always=True), primary_key=True)
    cik: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("companies.cik", ondelete="CASCADE")
    )
    call_date: Mapped[date]
    # Where the company publishes it: the natural key, so a re-run fetches nothing twice.
    source_url: Mapped[str] = mapped_column(unique=True)
    text_sha256: Mapped[str]
    words: Mapped[int]
    exchanges: Mapped[int]  # Q&A exchanges found when it was stored
    fetched_at: Mapped[datetime] = mapped_column(server_default=sa.func.now())


class CallSummary(Base):
    """One summary of a call's Q&A (ADR-0022): each exchange, then the takeaways. History is kept
    like a filing's: the newest row is current, and the same provider, model and prompt version
    replace their row."""

    __tablename__ = "call_summaries"
    __table_args__ = (
        sa.UniqueConstraint("transcript_id", "provider", "model", "prompt_version"),
        sa.Index("ix_call_summaries_transcript_id_created_at", "transcript_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(always=True), primary_key=True)
    transcript_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("call_transcripts.id", ondelete="CASCADE")
    )
    # The provider and model that wrote the takeaways; each exchange records its own, since a
    # failover can switch providers partway through a call.
    provider: Mapped[str]
    model: Mapped[str]
    prompt_version: Mapped[int]
    takeaways: Mapped[dict[str, Any]] = mapped_column(JSONB)  # a FilingSummary
    # [{analyst, firm, question, answer, provider, model, ungrounded_numbers, ungrounded_names}];
    # an exchange no provider could summarize is {analyst, firm, failed: true}.
    exchanges: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    # The takeaways' grounding checks, against the whole transcript.
    ungrounded_numbers: Mapped[list[str]] = mapped_column(ARRAY(sa.Text()))
    ungrounded_names: Mapped[list[str]] = mapped_column(ARRAY(sa.Text()))
    created_at: Mapped[datetime] = mapped_column(server_default=sa.func.now())


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
    # Capitalized words in an LLM summary the text never uses (an invented name, say); NULL =
    # not checked (template summaries, and those written before the check, 2026-10-22).
    ungrounded_names: Mapped[list[str] | None] = mapped_column(ARRAY(sa.Text()))
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
    # The company's name. Rows written before the ticker column existed hold "name ticker".
    company: Mapped[str]
    ticker: Mapped[str | None]
    body: Mapped[str]  # the summary and its key points
    # Searchable but not embedded or shown: other words for what the text says ("CFO" for "Chief
    # Financial Officer", "purchase" for "bought").
    keywords: Mapped[str] = mapped_column(server_default="")
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
    # Hex SHA-256 of the text that was embedded (SearchDocument.text()), so `filings-index`
    # re-embeds a document whose text has changed since. NULL for vectors stored before it was
    # recorded, which are re-embedded once.
    text_sha256: Mapped[str | None]
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
    # Reviews company suggestions (filings-admin grant <email>); never set through the API.
    is_admin: Mapped[bool] = mapped_column(default=False, server_default=sa.false())
    created_at: Mapped[datetime] = mapped_column(server_default=sa.func.now())


class RequestStatus(StrEnum):
    OPEN = "open"
    ADDED = "added"  # the company is on the watchlist now
    DECLINED = "declined"


class CompanyRequest(Base):
    """A signed-in user's suggestion to add a company to the watchlist. One per user and company;
    the admin decides per company (every request for it gets the decision)."""

    __tablename__ = "company_requests"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "cik"),
        sa.Index("ix_company_requests_cik", "cik"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE")
    )
    cik: Mapped[int] = mapped_column(sa.BigInteger, sa.ForeignKey("companies.cik"))
    note: Mapped[str | None]  # why, in the user's words (shown only to the admin)
    status: Mapped[RequestStatus] = mapped_column(
        _text_enum(RequestStatus, "status"),
        default=RequestStatus.OPEN,
        server_default=RequestStatus.OPEN.value,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=sa.func.now())
    decided_at: Mapped[datetime | None]


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
    """Alert `target` (a webhook URL or an email address) about a company's new filings, about
    any watchlist filing matching `query` (a keyword alert), or about a company's filings
    matching `query`."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        # Subscribing twice to the same thing would send every alert twice. NULLS NOT DISTINCT:
        # two keyword alerts on every company (cik NULL) with the same query are duplicates too.
        sa.UniqueConstraint(
            "user_id", "cik", "query", "channel", "target", postgresql_nulls_not_distinct=True
        ),
        sa.Index("ix_subscriptions_cik", "cik"),
        sa.CheckConstraint("cik IS NOT NULL OR query IS NOT NULL", name="subject"),
        sa.CheckConstraint(
            "form4_trades IN ('all', 'open_market', 'purchases')", name="form4_trades"
        ),
        sa.CheckConstraint("form4_min_value > 0", name="form4_min_value_positive"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE")
    )
    # NULL for a keyword alert on every watchlist company.
    cik: Mapped[int | None] = mapped_column(sa.BigInteger, sa.ForeignKey("companies.cik"))
    # Keyword alerts: only filings whose search document (filing_search) matches this
    # websearch_to_tsquery query ("buyback", "resign OR retire", '"credit facility"').
    query: Mapped[str | None]
    # Empty means every form type.
    form_types: Mapped[list[str]] = mapped_column(
        ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]")
    )
    channel: Mapped[Channel] = mapped_column(_text_enum(Channel, "channel"))
    target: Mapped[str]
    # Form 4s only (filings_common.alert_filters): which trades a Form 4 must report to be
    # alerted (all, open_market, purchases), and a minimum dollar value for them. Other forms
    # ignore both.
    form4_trades: Mapped[str] = mapped_column(server_default="all")
    form4_min_value: Mapped[int | None] = mapped_column(sa.BigInteger)
    # 8-Ks only: alert only when the 8-K reports one of these items (2.02, 5.02, ...); empty
    # means every 8-K.
    form8k_items: Mapped[list[str]] = mapped_column(
        ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=sa.func.now())


class Notification(Base):
    """One alert per (subscription, filing), or per (subscription, earnings call) for a call's
    Q&A summary (ADR-0022), enforced by the unique constraints: replays and redeliveries can
    never alert the same subscription about the same thing twice."""

    __tablename__ = "notifications"
    __table_args__ = (
        sa.UniqueConstraint("subscription_id", "accession_no"),
        sa.UniqueConstraint("subscription_id", "transcript_id"),
        sa.CheckConstraint("(accession_no IS NULL) <> (transcript_id IS NULL)", name="subject"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(always=True), primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("subscriptions.id", ondelete="CASCADE")
    )
    # What the alert is about: a filing, or an earnings call's Q&A. Exactly one is set.
    accession_no: Mapped[str | None] = mapped_column(
        sa.ForeignKey("filings.accession_no", ondelete="CASCADE")
    )
    transcript_id: Mapped[int | None] = mapped_column(
        sa.BigInteger, sa.ForeignKey("call_transcripts.id", ondelete="CASCADE")
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
    # W3C trace context ({"traceparent": ..., "tracestate": ...}) of the span that wrote the
    # event, so the relay continues that trace. NULL when no trace was active.
    trace_context: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=sa.func.now())
    published_at: Mapped[datetime | None]


def create_engine(url: str, *, hide_parameters: bool = False) -> sa.Engine:
    """`hide_parameters` keeps bound values out of SQLAlchemy's error messages and logs, for
    services whose queries carry user secrets (the API stores webhook URLs). Postgres's own error
    details can still quote a row, e.g. the key in a unique violation."""
    return sa.create_engine(url, pool_pre_ping=True, hide_parameters=hide_parameters)


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
