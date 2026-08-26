"""Outbox relay: drain ``outbox_events`` and publish via the bus.

``OutboxPublisher`` is a long-lived background task.  Each poll selects
up to 100 un-published rows (``published_at IS NULL`` and
``attempts < max_attempts``), deserializes the stored payload, publishes
via the bus, and records the outcome: ``published_at`` on success,
``attempts += 1`` on failure.  Rows that exhaust ``max_attempts`` are
skipped by the poll filter (ERROR logged) and left in place for manual
handling — the outbox never loses rows.

Credentials discipline: this module never touches credentials; it needs
only a session factory and a minimal bus protocol object.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ant.core.events import deserialize_event
from ant.storage.models import OutboxEventRecord

logger = logging.getLogger(__name__)

__all__ = ["OutboxPublisher"]

_BATCH_SIZE = 100


class OutboxPublisher:
    """Poll the outbox table and publish pending events via *bus*.

    *bus* only needs to satisfy the minimal protocol
    ``async def publish(event) -> None``.
    """

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        bus: Any,
        poll_interval: float = 1.0,
        max_attempts: int = 10,
    ) -> None:
        self._session_factory = session_factory
        self._bus = bus
        self.poll_interval = poll_interval
        self.max_attempts = max_attempts
        self._stop = asyncio.Event()

    async def stop(self) -> None:
        """Set the stop flag so ``run`` exits after the current batch (idempotent)."""
        self._stop.set()

    async def run(self) -> None:
        """Publish pending rows in a loop until stopped or cancelled.

        Cancellation (``CancelledError``) propagates immediately — the
        open batch session rolls back, leaving no partial state.
        """
        while not self._stop.is_set():
            try:
                await self._publish_batch()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("outbox poll batch failed")
            await asyncio.sleep(self.poll_interval)

    async def _publish_batch(self) -> None:
        """Publish up to ``_BATCH_SIZE`` pending rows, then commit the outcome."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(OutboxEventRecord)
                .where(
                    OutboxEventRecord.published_at.is_(None),
                    OutboxEventRecord.attempts < self.max_attempts,
                )
                .order_by(OutboxEventRecord.id)
                .limit(_BATCH_SIZE)
            )
            rows = result.scalars().all()
            if not rows:
                return
            for row in rows:
                await self._publish_row(row)
            await session.commit()

    async def _publish_row(self, row: OutboxEventRecord) -> None:
        """Publish one row: set ``published_at`` on success, bump attempts on failure."""
        try:
            event = deserialize_event(row.payload)
        except Exception:
            logger.exception("outbox row %s: cannot deserialize payload", row.id)
            self._bump_attempts(row)
            return
        try:
            await self._bus.publish(event)
        except Exception:
            logger.exception("outbox row %s: publish failed", row.id)
            self._bump_attempts(row)
            return
        row.published_at = datetime.now()

    def _bump_attempts(self, row: OutboxEventRecord) -> None:
        row.attempts += 1
        if row.attempts >= self.max_attempts:
            logger.error(
                "outbox row %s (message_id=%s): exhausted %s attempts, "
                "left in place for manual inspection",
                row.id,
                row.message_id,
                self.max_attempts,
            )
