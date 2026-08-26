"""Alembic migration environment (async engine).

Two entry points:

1. Programmatic: ``ant.storage.db.run_migrations(dsn)`` builds an
   AlembicConfig with ``sqlalchemy.url`` set and runs in a worker
   thread; this module creates its own async engine from that URL.
2. CLI: ``alembic -c ant/storage/alembic/alembic.ini upgrade head``
   (credentials must be provided via ``-x`` or environment; the ini
   holds no credentials by design).
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from ant.storage.models import Base

# this is the Alembic Config object, which provides access to the
# values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging — UNLESS invoked
# programmatically (ant.storage.db.run_migrations), which sets
# ``configure_logging=False`` so alembic never hijacks the app's root
# logger (this would break pytest caplog for later tests).
if (
    config.config_file_name is not None
    and config.attributes.get("configure_logging", True)
):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def do_run_migrations(connection) -> None:
    """Configure alembic on the (sync proxy of an) async connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine from sqlalchemy.url and run migrations."""
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "alembic: no sqlalchemy.url configured — call "
            "ant.storage.db.run_migrations(dsn) so the DSN is injected "
            "from .env instead of committing credentials."
        )

    connectable = create_async_engine(url, pool_pre_ping=True)
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against the async engine."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
