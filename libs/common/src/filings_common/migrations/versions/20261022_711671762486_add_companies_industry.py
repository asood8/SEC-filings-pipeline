"""Add companies.industry: SEC's description of the company's SIC code

Discovery fills it from the submissions API it already reads, so existing companies get theirs
on the next poll; until then it is NULL.

Revision ID: 711671762486
Revises: a77c95f05ece
Create Date: 2026-10-22 21:02:56.286626

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "711671762486"
down_revision: str | Sequence[str] | None = "a77c95f05ece"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("industry", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("companies", "industry")
