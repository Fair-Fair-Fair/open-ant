"""Outbox write path: enqueue events for reliable delivery.

``enqueue`` appends an ``OutboxEventRecord`` to the caller's active
SQLAlchemy session *without committing*.  The row lands in the
``outbox_events`` table in the same transaction as the caller's business
writes — all-or-nothing, so the outbox never diverges from app state.
The outbox drain (``ant.bus.outbox.OutboxPublisher``) publishes rows
after commit.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from ant.core.events import Event
from ant.storage.models import OutboxEventRecord

__all__ = ["enqueue"]


def enqueue(session: AsyncSession, event: Event, message_id: str) -> None:
    """Add an outbox row for *event* to *session* (no commit).

    The row is flushed and persisted only when the caller commits —
    ``await session.commit()`` — keeping it in the same transaction as
    the caller's business writes.  ``message_id`` is the dedupe key and
    the DB enforces uniqueness.

    Args:
        session: caller's active async session (transaction ownership
            stays with the caller).
        event: typed bus event; ``event_type`` is the class name and
            ``payload`` is ``event.to_dict()`` (which includes the
            ``"type"`` key ``deserialize_event`` needs to rebuild it).
        message_id: stable idempotency key, unique per outbox row.
    """
    session.add(
        OutboxEventRecord(
            event_type=event.__class__.__name__,
            payload=event.to_dict(),
            message_id=message_id,
        )
    )
