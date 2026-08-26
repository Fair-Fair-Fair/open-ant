"""LLM usage accounting (tokens / cost) persisted to ``usage_records``.

Best-effort by design: recording must never break a chat turn. A missing
session factory (jsonl backend), a closed database or any write failure
only produces a warning — the LLM call itself is unaffected.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from ant.storage.models import UsageRecord

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]


class UsageRecorder:
    """Records LLM usage rows into the ``usage_records`` table.

    ``session_factory`` is the shared MySQL async session factory
    (``SharedContext._session_factory``); ``None`` (jsonl backend) turns
    the recorder into a no-op.
    """

    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        self._session_factory = session_factory

    async def record_usage(
        self,
        session_id: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost: float,
    ) -> None:
        """Insert one usage row. Failures are logged, never raised."""
        if self._session_factory is None:
            logger.warning(
                "usage recording skipped: no session factory (storage backend "
                "is not MySQL-backed)"
            )
            return
        try:
            async with self._session_factory() as session:
                session.add(
                    UsageRecord(
                        session_id=session_id,
                        model=model,
                        input_tokens=int(prompt_tokens or 0),
                        output_tokens=int(completion_tokens or 0),
                        cost=float(cost or 0.0),
                    )
                )
                await session.commit()
        except Exception:
            # Accounting is best-effort: never let a failed write kill the turn.
            logger.warning(
                "usage recording failed (session=%s model=%s); LLM call is "
                "unaffected",
                session_id,
                model,
                exc_info=True,
            )


# Callable[[dict], Awaitable[None]] — the shape the harness pipeline expects
# on ``PipelineContext.usage_recorder`` (see agent.harness_stream_chat).
UsageCallback = Callable[[dict[str, Any]], Awaitable[None]]
