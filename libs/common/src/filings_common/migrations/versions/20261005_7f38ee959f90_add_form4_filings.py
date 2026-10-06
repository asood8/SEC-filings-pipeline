"""Add form4_filings

Revision ID: 7f38ee959f90
Revises: ae1c046dbcf6
Create Date: 2026-10-05 22:31:36.248550

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "7f38ee959f90"
down_revision: str | Sequence[str] | None = "ae1c046dbcf6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "form4_filings",
        sa.Column("accession_no", sa.Text(), nullable=False),
        sa.Column("period_of_report", sa.Date(), nullable=False),
        sa.Column("issuer_cik", sa.BigInteger(), nullable=False),
        sa.Column("issuer_name", sa.Text(), nullable=False),
        sa.Column("issuer_ticker", sa.Text(), nullable=True),
        sa.Column("reporting_owners", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("transaction_codes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("is_10b5_1", sa.Boolean(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["accession_no"],
            ["filings.accession_no"],
            name=op.f("fk_form4_filings_accession_no_filings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("accession_no", name=op.f("pk_form4_filings")),
    )
    op.create_index(
        "ix_form4_filings_issuer_cik_period",
        "form4_filings",
        ["issuer_cik", "period_of_report"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_form4_filings_issuer_cik_period", table_name="form4_filings")
    op.drop_table("form4_filings")
