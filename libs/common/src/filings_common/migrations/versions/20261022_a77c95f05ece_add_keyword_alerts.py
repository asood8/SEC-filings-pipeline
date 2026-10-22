"""Add keyword alerts: subscriptions.query, and cik optional

A subscription can alert about any watchlist filing whose search document matches `query`
(cik NULL), or about a company's filings that match it. Every subscription has a company, a
query, or both (check "subject"). The unique constraint now includes the query and treats NULLs
as equal, so the same keyword alert can't be added twice. Existing rows keep their company and
get no query. Downgrading deletes keyword alerts without a company first, which the old schema
can't hold.

Revision ID: a77c95f05ece
Revises: 0485ad731202
Create Date: 2026-10-22 03:35:09.416254

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a77c95f05ece"
down_revision: str | Sequence[str] | None = "0485ad731202"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("subscriptions", sa.Column("query", sa.Text(), nullable=True))
    op.alter_column("subscriptions", "cik", existing_type=sa.BIGINT(), nullable=True)
    op.drop_constraint(
        op.f("uq_subscriptions_user_id_cik_channel_target"), "subscriptions", type_="unique"
    )
    op.create_unique_constraint(
        op.f("uq_subscriptions_user_id_cik_query_channel_target"),
        "subscriptions",
        ["user_id", "cik", "query", "channel", "target"],
        postgresql_nulls_not_distinct=True,
    )
    op.create_check_constraint(
        op.f("ck_subscriptions_subject"), "subscriptions", "cik IS NOT NULL OR query IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DELETE FROM subscriptions WHERE cik IS NULL")
    op.drop_constraint(op.f("ck_subscriptions_subject"), "subscriptions", type_="check")
    op.drop_constraint(
        op.f("uq_subscriptions_user_id_cik_query_channel_target"), "subscriptions", type_="unique"
    )
    op.create_unique_constraint(
        op.f("uq_subscriptions_user_id_cik_channel_target"),
        "subscriptions",
        ["user_id", "cik", "channel", "target"],
        postgresql_nulls_not_distinct=False,
    )
    op.alter_column("subscriptions", "cik", existing_type=sa.BIGINT(), nullable=False)
    op.drop_column("subscriptions", "query")
