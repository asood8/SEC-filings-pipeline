"""Add form8k_filings

Revision ID: 9954cd456406
Revises: 7f38ee959f90
Create Date: 2026-10-05 22:44:01.222785

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "9954cd456406"
down_revision: str | Sequence[str] | None = "7f38ee959f90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "form8k_filings",
        sa.Column("accession_no", sa.Text(), nullable=False),
        sa.Column("items", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["accession_no"],
            ["filings.accession_no"],
            name=op.f("fk_form8k_filings_accession_no_filings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("accession_no", name=op.f("pk_form8k_filings")),
    )


def downgrade() -> None:
    op.drop_table("form8k_filings")
