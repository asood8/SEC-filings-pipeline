"""Add form8k_filings.investor_call

The upcoming call an 8-K announces (an earnings call, a Q&A session, an investor day): its kind,
day, start time when the filing gives one, and webcast link, as JSON
(filings_common.parsed.InvestorCall). Existing rows get NULL; the parser fills the column for new
8-Ks, and `filings-rebuild-key-fields --form-type 8-K` for those parsed before, from their MongoDB
payloads.

Revision ID: a0ec88f3a86a
Revises: 1a26f7f8a6f2
Create Date: 2026-10-21 02:23:59.137740

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a0ec88f3a86a"
down_revision: str | Sequence[str] | None = "1a26f7f8a6f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "form8k_filings",
        sa.Column("investor_call", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("form8k_filings", "investor_call")
