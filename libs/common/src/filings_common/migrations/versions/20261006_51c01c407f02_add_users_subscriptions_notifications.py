"""Add users, subscriptions, notifications

Revision ID: 51c01c407f02
Revises: ad262c5e530e
Create Date: 2026-10-06 20:52:56.343246

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "51c01c407f02"
down_revision: str | Sequence[str] | None = "ad262c5e530e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("google_sub", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("google_sub", name=op.f("uq_users_google_sub")),
    )
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("cik", sa.BigInteger(), nullable=False),
        sa.Column(
            "form_types",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "channel",
            sa.Enum(
                "discord",
                "slack",
                "email",
                name="channel",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "channel IN ('discord', 'slack', 'email')", name=op.f("ck_subscriptions_channel")
        ),
        sa.ForeignKeyConstraint(
            ["cik"], ["companies.cik"], name=op.f("fk_subscriptions_cik_companies")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_subscriptions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscriptions")),
    )
    op.create_index("ix_subscriptions_cik", "subscriptions", ["cik"], unique=False)
    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("subscription_id", sa.BigInteger(), nullable=False),
        sa.Column("accession_no", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "sent",
                "failed",
                name="status",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed')", name=op.f("ck_notifications_status")
        ),
        sa.ForeignKeyConstraint(
            ["accession_no"],
            ["filings.accession_no"],
            name=op.f("fk_notifications_accession_no_filings"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            name=op.f("fk_notifications_subscription_id_subscriptions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint(
            "subscription_id",
            "accession_no",
            name=op.f("uq_notifications_subscription_id_accession_no"),
        ),
    )


def downgrade() -> None:
    op.drop_table("notifications")
    op.drop_index("ix_subscriptions_cik", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_table("users")
