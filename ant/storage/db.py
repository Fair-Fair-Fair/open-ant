"""Async engine / session factories and database bootstrap helpers.

Bootstrap flow (used by integration tests now; server startup later):
    dsn = InfraSettings().mysql_dsn()          # None when credentials missing
    await create_database_if_missing(dsn)      # CREATE DATABASE ... utf8mb4
    await run_migrations(dsn)                  # alembic upgrade head

CREDENTIALS DISCIPLINE: never log ``dsn`` — it contains the password.
Use ``InfraSettings().masked_mysql_dsn()`` for any user-visible output.
"""

import asyncio
import logging
from pathlib import Path
from typing import AsyncIterator

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger(__name__)

__all__ = [
    "create_engine",
    "create_session_factory",
    "session_scope",
    "create_database_if_missing",
    "run_migrations",
    "alembic_config_for",
]

_ALEMBIC_DIR = Path(__file__).resolve().parent / "alembic"


def create_engine(dsn: str) -> AsyncEngine:
    """Create a lazy async engine for the given DSN.

    No connection is opened until the first use.  ``pool_pre_ping``
    discards stale pooled connections (MySQL wait_timeout).
    """
    return create_async_engine(dsn, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an async session factory bound to *engine*."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def session_scope(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Context-manager style async session factory.

    Usage::

        async with session_scope(engine) as session:
            await session.execute(...)
    """
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session


def _server_dsn(dsn: str) -> str:
    """Strip the database name from a DSN (server-level URL).

    Used for ``CREATE DATABASE IF NOT EXISTS`` — the database may not
    exist yet, so we must connect without it.

    NOTE: ``URL.set(database=None)`` cannot be used — SQLAlchemy 2.0
    silently drops a None/empty ``database`` kwarg, leaving the old name
    in place.  The URL is therefore rebuilt explicitly via ``URL.create``.
    """
    url = make_url(dsn)
    if not url.database:
        raise ValueError(f"DSN has no database name to strip: {url.render_as_string(hide_password=True)}")  # noqa: E501
    server_url = URL.create(
        drivername=url.drivername,
        username=url.username,
        password=url.password,
        host=url.host,
        port=url.port,
        database=None,
        query=url.query,
    )
    return server_url.render_as_string(hide_password=False)


def _database_name(dsn: str) -> str:
    url = make_url(dsn)
    if not url.database:
        raise ValueError("DSN has no database name")
    return url.database


async def create_database_if_missing(dsn: str) -> None:
    """Create the application database if it does not exist (utf8mb4).

    Connects to the server-level URL (no database) and runs
    ``CREATE DATABASE IF NOT EXISTS <db> DEFAULT CHARACTER SET utf8mb4``.
    The database name is read from *dsn*; it is quoted as a literal so it
    cannot be a SQL injection vector from config.
    """
    database = _database_name(dsn)
    if not database.replace("_", "").replace("-", "").isalnum():
        raise ValueError(f"Unsafe database name: {database!r}")

    server_url = _server_dsn(dsn)
    engine = create_async_engine(server_url, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    "CREATE DATABASE IF NOT EXISTS "
                    f"`{database}` DEFAULT CHARACTER SET utf8mb4"
                )
            )
            await conn.commit()
        logger.info("Ensured database exists: %s (utf8mb4)", database)
    finally:
        await engine.dispose()


# ── Alembic ────────────────────────────────────────────────────────────


def alembic_config_for(dsn: str) -> AlembicConfig:
    """Build an Alembic Config bound to the packaged migration scripts.

    ``sqlalchemy.url`` is set from *dsn* at runtime; the checked-in
    ``alembic.ini`` intentionally holds no credentials.
    """
    cfg = AlembicConfig(str(_ALEMBIC_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", dsn)
    # Never let alembic's fileConfig() reconfigure the app's root logger —
    # that would clobber logging (and pytest's caplog) for the whole
    # process.  The CLI (``alembic -c ...``) still configures logging.
    cfg.attributes["configure_logging"] = False
    return cfg


async def run_migrations(dsn: str) -> None:
    """Programmatically run ``alembic upgrade head`` against *dsn*.

    Runs in a worker thread (alembic is synchronous); ``env.py`` builds
    its own async engine from ``sqlalchemy.url`` and runs the migration
    inside a fresh event loop.
    """
    cfg = alembic_config_for(dsn)
    logger.info("Running alembic migrations (upgrade head)")
    await asyncio.to_thread(command.upgrade, cfg, "head")
    logger.info("Alembic migrations complete")
