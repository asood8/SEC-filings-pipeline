"""add filings view_url: the primary document as SEC shows it to people

A Form 4's primary_doc_url is its raw XML, which discovery parses; SEC's list of filings names
the XSL-rendered page (xslF345X06/form4.xml), which is what a reader should be sent to. NULL on
existing rows until discovery sees them again (within the 30-day lookback).

Revision ID: 8e719488e543
Revises: 979e944a9c6e
Create Date: 2026-11-13 23:57:42.953682

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8e719488e543"
down_revision: str | Sequence[str] | None = "979e944a9c6e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("filings", sa.Column("view_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("filings", "view_url")
