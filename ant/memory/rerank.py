"""Cross-encoder reranking for the retrieval pipeline (Phase 3C).

``rerank()`` re-scores retrieved memories with
``sentence_transformers.CrossEncoder("BAAI/bge-reranker-base")`` and
returns the top-``top_n`` in model order.

Failure contract (设计原则 11: 降级绝不影响主链路): the model is loaded
lazily and cached; if loading fails the documents come back in their
original order with exactly one warning per process.  Scoring failures
warn and keep the original order too — reranking must never break the
retrieval chain.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ant.provider.memory.base import MemoryDocument

logger = logging.getLogger(__name__)

RERANKER_MODEL = "BAAI/bge-reranker-base"

_model: object | None = None
_warned = False


def _get_model() -> object | None:
    """Lazy singleton cross-encoder; None on any load failure (never raises)."""
    global _model, _warned
    if _model is not None:
        return _model
    try:
        from sentence_transformers import CrossEncoder

        _model = CrossEncoder(RERANKER_MODEL)
    except Exception as exc:  # noqa: BLE001 — offline / model missing
        if not _warned:
            logger.warning(
                "Cross-encoder %r unavailable (%s) — rerank skipped, "
                "keeping original order",
                RERANKER_MODEL,
                exc,
            )
            _warned = True
        _model = None
    return _model


async def rerank(
    query: str,
    documents: list["MemoryDocument"],
    top_n: int,
) -> list["MemoryDocument"]:
    """Rerank *documents* by cross-encoder relevance to *query*.

    Returns the top-``top_n`` documents reordered by model score (scores
    overwrite ``doc.score``).  When the model cannot be loaded or scoring
    fails, the documents are returned in their original order, sliced to
    ``top_n``.
    """
    if not documents or top_n <= 0:
        return documents
    model = _get_model()
    if model is None:
        return documents[:top_n]
    try:
        # CrossEncoder.score is synchronous (torch) — offload it so the
        # event loop never blocks.
        scores = await asyncio.to_thread(
            model.score, query, [doc.content for doc in documents]
        )
    except Exception as exc:  # noqa: BLE001 — graceful degradation
        logger.warning("Cross-encoder scoring failed (%s) — keeping original order", exc)
        return documents[:top_n]
    ordered = sorted(zip(documents, scores), key=lambda pair: pair[1], reverse=True)
    return [
        doc.model_copy(update={"score": float(score)})
        for doc, score in ordered[:top_n]
    ]
