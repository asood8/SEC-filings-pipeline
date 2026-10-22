"""Add summaries.ungrounded_names

The name check of LLM summaries (filings_summarizer.grounding.ungrounded_names): capitalized words
in the summary that the filing's text never uses. Existing rows get NULL, "not checked".

Revision ID: 0485ad731202
Revises: be5a237b919d
Create Date: 2026-10-22 03:19:14.623561

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0485ad731202"
down_revision: str | Sequence[str] | None = "be5a237b919d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "summaries", sa.Column("ungrounded_names", postgresql.ARRAY(sa.Text()), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("summaries", "ungrounded_names")
