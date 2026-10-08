"""Add filing_search.ticker and filing_search.keywords

The ticker gets its own lexeme in capitals and is blanked out of the English text, so a ticker
that is also a word (COST) stops matching that word's forms (costs). Keywords hold other words
for what the text says (CFO for Chief Financial Officer), searchable but not embedded.

Existing rows keep their search behavior (no ticker, no keywords) until `filings-index` rebuilds
them. A rebuilt row's `company` holds the name only; the downgrade puts the ticker back after it.

Revision ID: 23e9e08283f1
Revises: 023766603346
Create Date: 2026-10-08 08:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "23e9e08283f1"
down_revision: str | Sequence[str] | None = "023766603346"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Run through sqlalchemy.text(), where "\:" is a literal colon (":1" would be a bind parameter).
_MASK = r"'\m' || regexp_replace(ticker, '\W', '\\\&', 'g') || '\M'"
NEW_TSV = (
    f"setweight(to_tsvector('english', coalesce(regexp_replace(title, {_MASK}, ' ', 'g'), title)),"
    " 'A') || "
    "setweight(to_tsvector('english', company), 'A') || "
    r"coalesce(setweight(('''' || replace(replace(upper(ticker), '\', '\\'), '''', '''''')"
    r" || '''\:1')::tsvector, 'A'), '') || "
    f"setweight(to_tsvector('english', coalesce(regexp_replace(body, {_MASK}, ' ', 'g'), body)),"
    " 'B') || "
    "setweight(to_tsvector('english', keywords), 'B')"
)
OLD_TSV = (
    "setweight(to_tsvector('english', title), 'A') || "
    "setweight(to_tsvector('english', company), 'A') || "
    "setweight(to_tsvector('english', body), 'B')"
)


def upgrade() -> None:
    op.add_column("filing_search", sa.Column("ticker", sa.Text(), nullable=True))
    op.add_column(
        "filing_search",
        sa.Column("keywords", sa.Text(), server_default="", nullable=False),
    )
    # Postgres 17+: recomputes every row and rebuilds the GIN index, under a brief exclusive lock.
    op.execute(f"ALTER TABLE filing_search ALTER COLUMN tsv SET EXPRESSION AS ({NEW_TSV})")


def downgrade() -> None:
    # The older code expects "name ticker" in company.
    op.execute("UPDATE filing_search SET company = company || ' ' || ticker WHERE ticker <> ''")
    op.execute(f"ALTER TABLE filing_search ALTER COLUMN tsv SET EXPRESSION AS ({OLD_TSV})")
    op.drop_column("filing_search", "keywords")
    op.drop_column("filing_search", "ticker")
