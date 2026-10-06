"""Add summaries.ungrounded_numbers

Revision ID: 6605fba482c6
Revises: daa30efcc621
Create Date: 2026-10-05 23:47:33.647725

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "6605fba482c6"
down_revision: str | Sequence[str] | None = "daa30efcc621"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "summaries", sa.Column("ungrounded_numbers", postgresql.ARRAY(sa.Text()), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("summaries", "ungrounded_numbers")
