"""Add filing_exhibits

Revision ID: daa30efcc621
Revises: 13c2122d7aaa
Create Date: 2026-10-05 23:24:09.276188

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "daa30efcc621"
down_revision: str | Sequence[str] | None = "13c2122d7aaa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "filing_exhibits",
        sa.Column("accession_no", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("exhibit_type", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["accession_no"],
            ["filings.accession_no"],
            name=op.f("fk_filing_exhibits_accession_no_filings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("accession_no", "filename", name=op.f("pk_filing_exhibits")),
    )


def downgrade() -> None:
    op.drop_table("filing_exhibits")
