"""SQLAlchemy 2.0 ORM models for the storage layer (Phase 1).

Tables:
  sessions           — conversation session metadata (one row per session)
  messages           — conversation messages (append-only, FK to sessions)
  outbox_events      — reliable-delivery outbox (idempotency via message_id)
  processed_messages — dedupe registry for inbound/outbound message ids
  audit_log          — append-only audit trail
  usage_records      — LLM token/cost accounting

``created_at`` / ``updated_at`` on ``sessions``/``messages`` use the same
ISO-string representation as the legacy JSONL format so both backends
behave identically.
"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# BIGINT AUTO_INCREMENT on MySQL; plain INTEGER on SQLite (SQLite only
# autoincrements an exact `INTEGER PRIMARY KEY`, so the unit tests — which
# run on SQLite+aiosqlite — need the variant).
BIGINT_AUTOINCREMENT = BigInteger().with_variant(Integer, "sqlite")

__all__ = [
    "Base",
    "SessionRecord",
    "MessageRecord",
    "OutboxEventRecord",
    "ProcessedMessageRecord",
    "AuditLogRecord",
    "UsageRecord",
]


class Base(DeclarativeBase):
    """Declarative base for all storage models."""


class SessionRecord(Base):
    """Conversation session metadata."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, index=True)


class MessageRecord(Base):
    """A single persisted conversation message."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BIGINT_AUTOINCREMENT, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    timestamp: Mapped[str] = mapped_column(String(40), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


class OutboxEventRecord(Base):
    """Reliable-delivery outbox row (one per outbound event)."""

    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(BIGINT_AUTOINCREMENT, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    message_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


class ProcessedMessageRecord(Base):
    """Dedupe registry: message ids already processed exactly once."""

    __tablename__ = "processed_messages"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class AuditLogRecord(Base):
    """Append-only audit trail of significant events."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BIGINT_AUTOINCREMENT, primary_key=True, autoincrement=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class UsageRecord(Base):
    """LLM usage accounting (tokens / cost per call)."""

    __tablename__ = "usage_records"

    id: Mapped[int] = mapped_column(BIGINT_AUTOINCREMENT, primary_key=True, autoincrement=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cost: Mapped[float | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
