"""Add outbox.trace_context

Revision ID: 023766603346
Revises: 3f26366cf8f4
Create Date: 2026-10-07 00:40:24.089387

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "023766603346"
down_revision: str | Sequence[str] | None = "3f26366cf8f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable with no default: adding it rewrites nothing, and existing rows have no trace.
    op.add_column(
        "outbox", sa.Column("trace_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("outbox", "trace_context")
