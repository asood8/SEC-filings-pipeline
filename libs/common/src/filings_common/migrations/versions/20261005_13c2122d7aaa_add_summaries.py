"""Add summaries

Revision ID: 13c2122d7aaa
Revises: 9954cd456406
Create Date: 2026-10-05 22:50:23.885888

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "13c2122d7aaa"
down_revision: str | Sequence[str] | None = "9954cd456406"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "summaries",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("accession_no", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Integer(), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["accession_no"],
            ["filings.accession_no"],
            name=op.f("fk_summaries_accession_no_filings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_summaries")),
        sa.UniqueConstraint(
            "accession_no",
            "provider",
            "model",
            "prompt_version",
            name=op.f("uq_summaries_accession_no_provider_model_prompt_version"),
        ),
    )
    op.create_index(
        "ix_summaries_accession_no_created_at",
        "summaries",
        ["accession_no", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_summaries_accession_no_created_at", table_name="summaries")
    op.drop_table("summaries")
