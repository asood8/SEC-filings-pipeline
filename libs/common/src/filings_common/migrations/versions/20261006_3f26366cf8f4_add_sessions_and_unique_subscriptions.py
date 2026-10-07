"""Add sessions and unique subscriptions

Revision ID: 3f26366cf8f4
Revises: 51c01c407f02
Create Date: 2026-10-06 23:19:58.940020

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3f26366cf8f4"
down_revision: str | Sequence[str] | None = "51c01c407f02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_sessions_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sessions_token_hash")),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False)
    op.create_unique_constraint(
        op.f("uq_subscriptions_user_id_cik_channel_target"),
        "subscriptions",
        ["user_id", "cik", "channel", "target"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("uq_subscriptions_user_id_cik_channel_target"), "subscriptions", type_="unique"
    )
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
