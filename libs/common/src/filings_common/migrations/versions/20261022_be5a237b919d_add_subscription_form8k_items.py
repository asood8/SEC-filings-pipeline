"""Add subscriptions.form8k_items

8-K alert filters (ADR-0012): alert only about 8-Ks that report one of these items (2.02
results, 5.02 officer changes, ...). Existing subscriptions get the empty list, which means every
8-K, so they keep every 8-K alert.

Revision ID: be5a237b919d
Revises: 478b6b568519
Create Date: 2026-10-22 03:04:22.791472

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "be5a237b919d"
down_revision: str | Sequence[str] | None = "478b6b568519"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column(
            "form8k_items",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "form8k_items")
