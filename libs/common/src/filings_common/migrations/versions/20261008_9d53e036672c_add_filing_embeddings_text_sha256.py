"""Add filing_embeddings.text_sha256

A hash of the text each vector was made from, so `filings-index` re-embeds exactly the documents
whose text changed since they were embedded. Existing vectors get NULL: nothing says which text
made them, so the next `filings-index` run re-embeds them once.

Revision ID: 9d53e036672c
Revises: 60526c3a24c7
Create Date: 2026-10-08 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9d53e036672c"
down_revision: str | Sequence[str] | None = "60526c3a24c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("filing_embeddings", sa.Column("text_sha256", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("filing_embeddings", "text_sha256")
