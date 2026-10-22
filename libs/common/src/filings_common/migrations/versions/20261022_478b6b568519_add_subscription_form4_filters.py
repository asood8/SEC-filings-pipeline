"""Add subscriptions.form4_trades and form4_min_value

Form 4 alert filters (filings_common.alert_filters, ADR-0012): which trades a Form 4 must report
to be alerted (all, open_market, purchases) and a minimum dollar value for them. Existing
subscriptions get 'all' and no minimum, so they keep every Form 4 alert. The checks scan the
table once (a few rows) to validate.

Revision ID: 478b6b568519
Revises: a0ec88f3a86a
Create Date: 2026-10-22 02:52:52.794730

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "478b6b568519"
down_revision: str | Sequence[str] | None = "a0ec88f3a86a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscriptions", sa.Column("form4_trades", sa.Text(), server_default="all", nullable=False)
    )
    op.add_column("subscriptions", sa.Column("form4_min_value", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        op.f("ck_subscriptions_form4_trades"),
        "subscriptions",
        "form4_trades IN ('all', 'open_market', 'purchases')",
    )
    op.create_check_constraint(
        op.f("ck_subscriptions_form4_min_value_positive"), "subscriptions", "form4_min_value > 0"
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_subscriptions_form4_min_value_positive"), "subscriptions", type_="check"
    )
    op.drop_constraint(op.f("ck_subscriptions_form4_trades"), "subscriptions", type_="check")
    op.drop_column("subscriptions", "form4_min_value")
    op.drop_column("subscriptions", "form4_trades")
