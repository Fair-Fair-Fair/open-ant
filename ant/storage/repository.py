"""History repository protocol + MySQL and JSONL implementations.

``HistoryRepository`` is the single persistence contract the rest of the
runtime depends on.  All methods are async.  ``MysqlHistoryRepository``
is the production backend (asyncmy); ``JsonlHistoryRepository`` adapts
the legacy JSONL implementation for dev / tests / fallback.

Semantics (kept identical across both backends):

* ``save_message`` raises ``ValueError`` when the session does not exist.
* ``get_messages`` returns ``[]`` for an unknown session.
* ``list_sessions`` returns most-recently-updated first.
* A session's title is auto-generated from its first user message.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ant.core.events import EventSource
from ant.core.history import HistoryStore
from ant.storage.db import create_engine, create_session_factory
from ant.storage.models import MessageRecord, SessionRecord
from ant.storage.schemas import HistoryMessage, HistorySession

logger = logging.getLogger(__name__)

__all__ = [
    "HistoryRepository",
    "MysqlHistoryRepository",
    "JsonlHistoryRepository",
]


def _now_iso() -> str:
    return datetime.now().isoformat()


def _derive_title(content: str, limit: int = 50) -> str:
    """Auto-title from a user message, mirroring the legacy JSONL logic."""
    title = content[:limit]
    if len(content) > limit:
        title += "..."
    return title


def _session_from_record(rec: SessionRecord) -> HistorySession:
    return HistorySession(
        id=rec.id,
        agent_id=rec.agent_id,
        source=rec.source,
        title=rec.title,
        message_count=rec.message_count,
        created_at=rec.created_at,
        updated_at=rec.updated_at,
    )


class HistoryRepository(Protocol):
    """Async persistence contract for conversation history."""

    async def create_session(
        self, agent_id: str, session_id: str, source: EventSource
    ) -> None:
        """Create a session row (idempotent: existing id is kept)."""
        ...

    async def save_message(self, session_id: str, message: HistoryMessage) -> None:
        """Persist one message; raise ValueError when the session is unknown."""
        ...

    async def list_sessions(self) -> list[HistorySession]:
        """All sessions, most recently updated first."""
        ...

    async def get_messages(self, session_id: str) -> list[HistoryMessage]:
        """Messages for a session in insertion order; [] when unknown."""
        ...

    async def get_session_info(self, session_id: str) -> HistorySession | None:
        """Session metadata without loading messages; None when unknown."""
        ...


# ── MySQL implementation (asyncmy) ─────────────────────────────────────


class MysqlHistoryRepository:
    """MySQL-backed history repository.

    Single-message INSERTs are constant-time regardless of history length;
    the session row is updated in the same transaction.  Use
    ``engine=`` only for tests (e.g. SQLite in-memory); production callers
    pass a MySQL DSN.
    """

    def __init__(self, dsn: str, engine: AsyncEngine | None = None) -> None:
        # Never log `dsn` — it embeds the MySQL password.
        self._dsn = dsn
        self._engine: AsyncEngine = engine or create_engine(dsn)
        self._session_factory: async_sessionmaker[AsyncSession] = (
            create_session_factory(self._engine)
        )

    async def create_session(
        self, agent_id: str, session_id: str, source: EventSource
    ) -> None:
        now = _now_iso()
        record = SessionRecord(
            id=session_id,
            agent_id=agent_id,
            source=str(source),
            title=None,
            message_count=0,
            created_at=now,
            updated_at=now,
        )
        async with self._session_factory() as session:
            session.add(record)
            try:
                await session.commit()
            except IntegrityError:
                # Concurrent create / resume race: id already exists — the
                # session row is fine as-is, so treat as success.
                await session.rollback()
                logger.debug("Session %s already exists; create skipped", session_id)

    async def save_message(self, session_id: str, message: HistoryMessage) -> None:
        async with self._session_factory() as session:
            session_row = await session.scalar(
                select(SessionRecord).where(SessionRecord.id == session_id)
            )
            if session_row is None:
                raise ValueError(f"Session not found: {session_id}")

            session.add(
                MessageRecord(
                    session_id=session_id,
                    timestamp=message.timestamp,
                    role=message.role,
                    content=message.content,
                    tool_calls=message.tool_calls,
                    tool_call_id=message.tool_call_id,
                )
            )

            new_title = session_row.title
            if new_title is None and message.role == "user":
                new_title = _derive_title(message.content)

            await session.execute(
                update(SessionRecord)
                .where(SessionRecord.id == session_id)
                .values(
                    title=new_title,
                    message_count=session_row.message_count + 1,
                    updated_at=_now_iso(),
                )
            )
            await session.commit()

    async def list_sessions(self) -> list[HistorySession]:
        async with self._session_factory() as session:
            result = await session.scalars(
                select(SessionRecord).order_by(SessionRecord.updated_at.desc())
            )
            return [_session_from_record(rec) for rec in result]

    async def get_messages(self, session_id: str) -> list[HistoryMessage]:
        async with self._session_factory() as session:
            result = await session.scalars(
                select(MessageRecord)
                .where(MessageRecord.session_id == session_id)
                .order_by(MessageRecord.id.asc())
            )
            return [
                HistoryMessage(
                    timestamp=rec.timestamp,
                    role=rec.role,
                    content=rec.content,
                    tool_calls=rec.tool_calls,
                    tool_call_id=rec.tool_call_id,
                )
                for rec in result
            ]

    async def get_session_info(self, session_id: str) -> HistorySession | None:
        async with self._session_factory() as session:
            rec = await session.scalar(
                select(SessionRecord).where(SessionRecord.id == session_id)
            )
            return _session_from_record(rec) if rec is not None else None

    async def close(self) -> None:
        """Dispose the engine (used by tests)."""
        await self._engine.dispose()


# ── JSONL implementation (legacy adapter) ──────────────────────────────


class JsonlHistoryRepository:
    """Async adapter over the legacy JSONL ``HistoryStore``.

    Kept for dev / unit tests and as the automatic fallback when MySQL
    credentials are unavailable.  The on-disk format is unchanged, so a
    pre-existing ``.history/`` directory keeps working as-is.
    """

    def __init__(self, base_path: Path) -> None:
        self._store = HistoryStore(base_path)

    async def create_session(
        self, agent_id: str, session_id: str, source: EventSource
    ) -> None:
        self._store.create_session(agent_id, session_id, source)

    async def save_message(self, session_id: str, message: HistoryMessage) -> None:
        self._store.save_message(session_id, message)

    async def list_sessions(self) -> list[HistorySession]:
        return self._store.list_sessions()

    async def get_messages(self, session_id: str) -> list[HistoryMessage]:
        return self._store.get_messages(session_id)

    async def get_session_info(self, session_id: str) -> HistorySession | None:
        return self._store.get_session_info(session_id)
