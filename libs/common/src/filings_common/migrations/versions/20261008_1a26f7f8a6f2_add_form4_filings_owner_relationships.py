"""Add form4_filings.reporting_owner_relationships

Each reporting owner's relationship to the issuer (director, officer and title, 10% owner, other),
as a JSON array in the order of `reporting_owners`, so the API can say who an insider is ("CEO",
"Director", "10% owner"). Existing rows get NULL; the parser fills the column for new Form 4s, and
`filings-rebuild-key-fields --form-type 4` for those parsed before, from their MongoDB payloads.

A check constraint keeps one relationship per owner. Adding it scans the table once (a few
hundred rows here), and NULLs pass it.

Revision ID: 1a26f7f8a6f2
Revises: bd7e46183a3c
Create Date: 2026-10-08 18:25:23.387372

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "1a26f7f8a6f2"
down_revision: str | Sequence[str] | None = "bd7e46183a3c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "form4_filings",
        sa.Column(
            "reporting_owner_relationships", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )
    op.create_check_constraint(
        op.f("ck_form4_filings_reporting_owner_relationships_length"),
        "form4_filings",
        "jsonb_array_length(reporting_owner_relationships) = cardinality(reporting_owners)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_form4_filings_reporting_owner_relationships_length"),
        "form4_filings",
        type_="check",
    )
    op.drop_column("form4_filings", "reporting_owner_relationships")
