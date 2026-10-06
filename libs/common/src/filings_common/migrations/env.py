"""Alembic environment: runs migrations against DATABASE_URL, or a URL passed programmatically."""

from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context

from filings_common.config import DatabaseSettings
from filings_common.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    url: str | None = config.attributes.get("url")
    return url if url is not None else DatabaseSettings().database_url.get_secret_value()


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of connecting (`alembic upgrade head --sql`)."""
    context.configure(
        url=_database_url(),
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = sa.create_engine(_database_url(), poolclass=sa.pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
