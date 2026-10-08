"""Add form4_transactions

Revision ID: 60526c3a24c7
Revises: 23e9e08283f1
Create Date: 2026-10-08 07:35:53.073900

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "60526c3a24c7"
down_revision: str | Sequence[str] | None = "23e9e08283f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Starts empty. The parser fills it for new Form 4s; `filings-rebuild-key-fields --form-type 4`
    # fills it for those parsed before, from their MongoDB payloads.
    op.create_table(
        "form4_transactions",
        sa.Column("accession_no", sa.Text(), nullable=False),
        sa.Column("line", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("is_derivative", sa.Boolean(), nullable=False),
        sa.Column("security_title", sa.Text(), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=True),
        sa.Column("code", sa.Text(), nullable=True),
        sa.Column("shares", sa.Double(), nullable=True),
        sa.Column("price_per_share", sa.Double(), nullable=True),
        sa.Column("acquired_disposed", sa.Text(), nullable=True),
        sa.Column("shares_owned_after", sa.Double(), nullable=True),
        sa.Column("direct_or_indirect", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "acquired_disposed IN ('A', 'D')", name=op.f("ck_form4_transactions_acquired_disposed")
        ),
        sa.CheckConstraint(
            "direct_or_indirect IN ('D', 'I')",
            name=op.f("ck_form4_transactions_direct_or_indirect"),
        ),
        sa.ForeignKeyConstraint(
            ["accession_no"],
            ["form4_filings.accession_no"],
            name=op.f("fk_form4_transactions_accession_no_form4_filings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("accession_no", "line", name=op.f("pk_form4_transactions")),
    )


def downgrade() -> None:
    op.drop_table("form4_transactions")
