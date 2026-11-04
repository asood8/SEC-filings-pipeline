"""Add amended filings: report dates, amendment links, and superseded Form 4 lines

- filings.report_date: EDGAR's reportDate. Discovery fills it in for known filings it sees again
  (those in its lookback window); older rows stay NULL.
- filings.amends_filed_at, filings.amends_accession_no: an amendment's stated original date and
  the original it was linked to (filings_parser.amendments).
- form4_transactions.nature_of_ownership: part of what matches an amendment's line to the
  original's. NULL on existing rows until `filings-rebuild-key-fields --form-type 4`.
- form4_transactions.superseded_by: the Form 4/A whose line restates this one. The trades index
  now leaves superseded lines out, like the queries that use it.

Revision ID: 777e3b230648
Revises: 711671762486
Create Date: 2026-11-04 01:18:26.886368

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "777e3b230648"
down_revision: str | Sequence[str] | None = "711671762486"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRADES = "code IN ('P', 'S') AND is_derivative IS false"
CURRENT_TRADES = f"{TRADES} AND superseded_by IS NULL"


def _trades_index(condition: str) -> None:
    op.drop_index("ix_form4_transactions_trades", table_name="form4_transactions")
    op.create_index(
        "ix_form4_transactions_trades",
        "form4_transactions",
        ["accession_no"],
        postgresql_where=sa.text(condition),
    )


def upgrade() -> None:
    op.add_column("filings", sa.Column("report_date", sa.Date(), nullable=True))
    op.add_column("filings", sa.Column("amends_filed_at", sa.Date(), nullable=True))
    op.add_column("filings", sa.Column("amends_accession_no", sa.Text(), nullable=True))
    op.create_index("ix_filings_amends_accession_no", "filings", ["amends_accession_no"])
    op.create_foreign_key(
        op.f("fk_filings_amends_accession_no_filings"),
        "filings",
        "filings",
        ["amends_accession_no"],
        ["accession_no"],
        ondelete="SET NULL",
    )
    op.add_column("form4_transactions", sa.Column("nature_of_ownership", sa.Text(), nullable=True))
    op.add_column("form4_transactions", sa.Column("superseded_by", sa.Text(), nullable=True))
    op.create_foreign_key(
        op.f("fk_form4_transactions_superseded_by_form4_filings"),
        "form4_transactions",
        "form4_filings",
        ["superseded_by"],
        ["accession_no"],
        ondelete="SET NULL",
    )
    _trades_index(CURRENT_TRADES)


def downgrade() -> None:
    _trades_index(TRADES)
    op.drop_constraint(
        op.f("fk_form4_transactions_superseded_by_form4_filings"),
        "form4_transactions",
        type_="foreignkey",
    )
    op.drop_column("form4_transactions", "superseded_by")
    op.drop_column("form4_transactions", "nature_of_ownership")
    op.drop_constraint(
        op.f("fk_filings_amends_accession_no_filings"), "filings", type_="foreignkey"
    )
    op.drop_index("ix_filings_amends_accession_no", table_name="filings")
    op.drop_column("filings", "amends_accession_no")
    op.drop_column("filings", "amends_filed_at")
    op.drop_column("filings", "report_date")
