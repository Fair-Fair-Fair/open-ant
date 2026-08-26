"""Message idempotency helpers for worker consumption (Phase 1).

RabbitMQ gives at-least-once delivery: a crash (or nack) between consume
and ack redelivers the same message with the same ``message_id``.
``processed_messages`` (message_id primary key, see
``ant.storage.models.ProcessedMessageRecord``) makes processing
exactly-once per id.

Only meaningful for the MySQL backend — JSONL mode has no shared registry
and simply never dedupes (``is_processed`` returns False, ``mark_processed``
is a no-op).  The caller decides whether to consult these helpers; workers
only do so in rabbitmq mode.
"""

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ant.storage.models import ProcessedMessageRecord

logger = logging.getLogger(__name__)

__all__ = ["is_processed", "mark_processed"]

SessionFactory = async_sessionmaker[AsyncSession]


async def is_processed(session_factory: SessionFactory | None, message_id: str) -> bool:
    """True when *message_id* was already marked as processed.

    ``session_factory is None`` (jsonl backend) or an empty id → False.
    """
    if session_factory is None or not message_id:
        return False
    async with session_factory() as session:
        row = await session.scalar(
            select(ProcessedMessageRecord.id).where(
                ProcessedMessageRecord.id == message_id
            )
        )
        return row is not None


async def mark_processed(session_factory: SessionFactory | None, message_id: str) -> None:
    """Record *message_id* as processed (INSERT, duplicates ignored).

    ``session_factory is None`` (jsonl backend) or an empty id → no-op.
    """
    if session_factory is None or not message_id:
        return
    async with session_factory() as session:
        session.add(ProcessedMessageRecord(id=message_id))
        try:
            await session.commit()
        except IntegrityError:
            # Concurrent duplicate: another consumer already recorded it.
            await session.rollback()
            logger.debug("message_id %s already processed", message_id)
