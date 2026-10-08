"""Add form4_filings.reporting_owner_ciks

The reporting owners' CIKs, in the order of their names, so insider activity can group filings by
who filed them rather than by how their names are spelled. Existing rows get NULL; the parser
fills the column for new Form 4s, and `filings-rebuild-key-fields --form-type 4` for those parsed
before, from their MongoDB payloads.

Revision ID: 0046b4a4d18b
Revises: 9d53e036672c
Create Date: 2026-10-08 16:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0046b4a4d18b"
down_revision: str | Sequence[str] | None = "9d53e036672c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "form4_filings",
        sa.Column("reporting_owner_ciks", postgresql.ARRAY(sa.BigInteger()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("form4_filings", "reporting_owner_ciks")
