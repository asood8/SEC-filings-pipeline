"""Add filing_search and filing_embeddings

Revision ID: ad262c5e530e
Revises: 6605fba482c6
Create Date: 2026-10-06 20:38:26.341634

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "ad262c5e530e"
down_revision: str | Sequence[str] | None = "6605fba482c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pgvector ships in the Postgres image; this enables it in the database.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "filing_embeddings",
        sa.Column("accession_no", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(768), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["accession_no"],
            ["filings.accession_no"],
            name=op.f("fk_filing_embeddings_accession_no_filings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("accession_no", "model", name=op.f("pk_filing_embeddings")),
    )
    op.create_index(
        "ix_filing_embeddings_embedding_hnsw",
        "filing_embeddings",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_table(
        "filing_search",
        sa.Column("accession_no", sa.Text(), nullable=False),
        sa.Column("summary_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("company", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "setweight(to_tsvector('english', title), 'A') || "
                "setweight(to_tsvector('english', company), 'A') || "
                "setweight(to_tsvector('english', body), 'B')",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["accession_no"],
            ["filings.accession_no"],
            name=op.f("fk_filing_search_accession_no_filings"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["summary_id"],
            ["summaries.id"],
            name=op.f("fk_filing_search_summary_id_summaries"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("accession_no", name=op.f("pk_filing_search")),
    )
    op.create_index(
        "ix_filing_search_tsv", "filing_search", ["tsv"], unique=False, postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_filing_search_tsv", table_name="filing_search", postgresql_using="gin")
    op.drop_table("filing_search")
    op.drop_index(
        "ix_filing_embeddings_embedding_hnsw",
        table_name="filing_embeddings",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.drop_table("filing_embeddings")
