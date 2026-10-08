"""Add the feed and insider-trade indexes

- filings (filed_at, accession_no): the feed's order. Without it every page of GET /filings
  sorted all filings (278 ms at 450,000 filings; under 1 ms with it).
- form4_transactions (accession_no) for open-market purchases and sales only: what
  GET /companies/{ticker}/insider-activity totals. The planner then probes the issuer's filings'
  lines instead of scanning all of them (89 ms to 35 ms for a company with 8,400 Form 4s).

Measured with scripts/synthetic_data.py and scripts/measure_queries.py (docs/runbook.md,
"Capacity planning"). Plain CREATE INDEX: at today's volume each takes milliseconds (under a
second at 450,000 filings), so the brief write lock doesn't matter.

Revision ID: bd7e46183a3c
Revises: 0046b4a4d18b
Create Date: 2026-10-08 13:31:35.286655

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bd7e46183a3c"
down_revision: str | Sequence[str] | None = "0046b4a4d18b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRADES = "code IN ('P', 'S') AND is_derivative IS false"


def upgrade() -> None:
    op.create_index("ix_filings_filed_at_accession_no", "filings", ["filed_at", "accession_no"])
    op.create_index(
        "ix_form4_transactions_trades",
        "form4_transactions",
        ["accession_no"],
        postgresql_where=sa.text(TRADES),
    )


def downgrade() -> None:
    op.drop_index("ix_form4_transactions_trades", table_name="form4_transactions")
    op.drop_index("ix_filings_filed_at_accession_no", table_name="filings")
