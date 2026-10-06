"""Create companies, filings, and outbox tables

Revision ID: ae1c046dbcf6
Revises:
Create Date: 2026-10-05 20:40:32.277292

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "ae1c046dbcf6"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("cik", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("exchange", sa.Text(), nullable=True),
        sa.Column("on_watchlist", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("cik", name=op.f("pk_companies")),
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
    )
    op.create_index(
        "ix_outbox_unpublished",
        "outbox",
        ["id"],
        unique=False,
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_table(
        "filings",
        sa.Column("accession_no", sa.Text(), nullable=False),
        sa.Column("cik", sa.BigInteger(), nullable=False),
        sa.Column("form_type", sa.Text(), nullable=False),
        sa.Column("filed_at", sa.Date(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("primary_doc_url", sa.Text(), nullable=False),
        sa.Column("raw_object_key", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "discovered",
                "parsed",
                "ready",
                "notified",
                "failed",
                name="status",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            server_default="discovered",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('discovered', 'parsed', 'ready', 'notified', 'failed')",
            name=op.f("ck_filings_status"),
        ),
        sa.ForeignKeyConstraint(["cik"], ["companies.cik"], name=op.f("fk_filings_cik_companies")),
        sa.PrimaryKeyConstraint("accession_no", name=op.f("pk_filings")),
    )
    op.create_index("ix_filings_cik_filed_at", "filings", ["cik", "filed_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_filings_cik_filed_at", table_name="filings")
    op.drop_table("filings")
    op.drop_index(
        "ix_outbox_unpublished",
        table_name="outbox",
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.drop_table("outbox")
    op.drop_table("companies")
