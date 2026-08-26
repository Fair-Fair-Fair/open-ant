"""Real-MySQL integration tests (Phase 1 foundation).

These tests require a reachable MySQL server with credentials provided in
``.env`` (``MYSQL_USERNAME`` / ``MYSQL_PASSWORD``; host/port/database
default to 127.0.0.1:3306/open_ant).  They are *skipped* (never failed)
when:

  * credentials are absent or incomplete in ``.env``, or
  * the server cannot be reached.

When run, they create the ``open_ant`` database if missing (utf8mb4),
run the alembic migrations, and exercise ``MysqlHistoryRepository`` and
the outbox JSON column end-to-end.
"""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ant.core.events import CliEventSource
from ant.storage.db import create_database_if_missing, run_migrations
from ant.storage.models import OutboxEventRecord
from ant.storage.repository import MysqlHistoryRepository
from ant.storage.schemas import HistoryMessage
from ant.utils.settings import InfraSettings


@pytest.fixture(scope="module")
async def mysql_dsn():
    """DSN from .env; skip the whole module when unusable.

    Credentials discipline: the skip reason only names the failure class
    (e.g. OperationalError / TimeoutError) — never the DSN or password.
    """
    infra = InfraSettings()
    dsn = infra.mysql_dsn()
    if dsn is None:
        pytest.skip(
            "MySQL credentials not configured in .env "
            "(MYSQL_USERNAME / MYSQL_PASSWORD missing)"
        )
    # Probe the SERVER-level DSN (no database): the application database
    # may legitimately not exist yet — that is what the bootstrap creates.
    try:
        engine = create_async_engine(infra.mysql_server_dsn(), pool_pre_ping=True)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"MySQL unreachable ({type(exc).__name__}); integration tests skipped")

    # Bootstrap: create the database if missing, then migrate to head.
    # Failures here are real integration failures — do NOT skip.
    await create_database_if_missing(dsn)
    await run_migrations(dsn)
    return dsn


async def test_create_database_and_migrate_creates_all_tables(mysql_dsn):
    """Bootstrap leaves the full schema (all six tables + alembic_version)."""
    engine = create_async_engine(mysql_dsn, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            tables = set(
                (
                    await conn.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = DATABASE()"
                        )
                    )
                ).scalars()
            )
        assert {"sessions", "messages", "outbox_events"} <= tables
        assert {"processed_messages", "audit_log", "usage_records"} <= tables
        assert "alembic_version" in tables

        async with engine.connect() as conn:
            rev = (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar()
        assert rev == "0001"
    finally:
        await engine.dispose()


async def test_mysql_repository_full_round_trip(mysql_dsn):
    repo = MysqlHistoryRepository(mysql_dsn)
    try:
        sid = f"it-{uuid.uuid4().hex[:12]}"
        await repo.create_session("it-agent", sid, CliEventSource())
        await repo.save_message(sid, HistoryMessage(role="user", content="hello from integration"))
        await repo.save_message(sid, HistoryMessage(role="assistant", content="hi back"))

        info = await repo.get_session_info(sid)
        assert info is not None
        assert info.agent_id == "it-agent"
        assert info.source == "platform-cli:cli-user"
        assert info.message_count == 2

        msgs = await repo.get_messages(sid)
        assert [m.content for m in msgs] == ["hello from integration", "hi back"]

        all_ids = [s.id for s in await repo.list_sessions()]
        assert sid in all_ids

        with pytest.raises(ValueError):
            await repo.save_message("does-not-exist", HistoryMessage(role="user", content="x"))
    finally:
        await repo.close()


async def test_outbox_table_json_column_on_mysql(mysql_dsn):
    """The outbox_events JSON payload column round-trips on real MySQL."""
    engine = create_async_engine(mysql_dsn, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    message_id = f"it-msg-{uuid.uuid4().hex[:12]}"
    try:
        async with factory() as session:
            session.add(
                OutboxEventRecord(
                    event_type="OutboundEvent",
                    payload={"kind": "test", "n": 42, "nested": {"ok": True}},
                    message_id=message_id,
                )
            )
            await session.commit()

        async with factory() as session:
            row = (
                await session.scalars(
                    select(OutboxEventRecord).where(
                        OutboxEventRecord.message_id == message_id
                    )
                )
            ).first()
        assert row is not None
        assert row.payload == {"kind": "test", "n": 42, "nested": {"ok": True}}
        assert row.attempts == 0
        assert row.published_at is None
    finally:
        # clean up the probe row so later phases start with a clean outbox
        async with factory() as session:
            await session.execute(
                OutboxEventRecord.__table__.delete().where(
                    OutboxEventRecord.message_id == message_id
                )
            )
            await session.commit()
        await engine.dispose()
