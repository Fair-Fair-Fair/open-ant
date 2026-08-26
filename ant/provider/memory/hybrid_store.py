"""Hybrid retrieval store: vector + BM25 keyword fusion (OpenClaw-aligned).

OpenClaw's built-in engine keeps a dual index (vector + FTS5 full-text)
and fuses the two result lists before injection.  This module implements
the same idea on top of our ChromaDB vector store plus a zero-dependency
BM25 index:

    query ──► vector search ──┐
                              ├─► fusion (RRF or 70/30 weighted) ─► threshold
    query ──► BM25 search  ───┘       ─► per-source diversity ─► optional
                                        cross-encoder rerank ─► top-k

Every stage is config-driven (see MemoryConfig) and every optional stage
degrades gracefully — no stage failing can break retrieval.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import TYPE_CHECKING

from .base import MemoryDocument, VectorStore
from .bm25_index import BM25Index

if TYPE_CHECKING:
    from ant.utils.config import Config

logger = logging.getLogger(__name__)

# Reciprocal-rank-fusion constant: a result ranked r contributes 1/(k+r).
# k=60 is the value used in the original RRF paper.
RRF_K = 60

# Oversampling factor: fetch more candidates than needed so fusion has
# room to re-rank (e.g. a strong BM25 hit buried deep in vector results).
_OVERFETCH = 4


class HybridMemoryStore(VectorStore):
    """Facade wrapping a VectorStore with a synchronized BM25 index.

    Writes (add/update/delete) maintain both indexes; queries fuse both.
    When ``hybrid_enabled`` is False this behaves exactly like the
    underlying vector store (pure semantic search).
    """

    def __init__(self, vector_store: VectorStore, config: "Config"):
        self._store = vector_store
        self._memory_config = config.memory
        self._bm25: BM25Index | None = None  # lazy — built on first use
        self._reranker = None

    # ── dual-index maintenance ──

    def _index(self) -> BM25Index:
        """Lazy-load (or rebuild) the BM25 index.

        Self-heal: if the index file is missing/corrupt but the vector
        store has documents, rebuild the keyword side from the store so
        the dual index never silently diverges.
        """
        if self._bm25 is None:
            path = self._memory_config.persist_directory / "bm25_index.json"
            self._bm25 = BM25Index(path)
            if len(self._bm25) == 0:
                self._rebuild_index()
        return self._bm25

    def _rebuild_index(self) -> None:
        try:
            all_docs = self._store.all_documents()  # type: ignore[attr-defined]
        except (AttributeError, Exception):  # noqa: BLE001
            return
        if not all_docs:
            return
        for doc in all_docs:
            self._bm25.add(doc.id, doc.content)
        self._bm25.save()
        logger.info("BM25 index rebuilt from vector store: %d docs", len(all_docs))

    async def add(
        self,
        documents: list[str],
        metadatas: list[dict] | None = None,
        ids: list[str] | None = None,
    ) -> None:
        await self._store.add(documents, metadatas, ids)
        if ids and len(ids) == len(documents):
            index = self._index()
            for doc_id, text in zip(ids, documents):
                index.add(doc_id, text)
            index.save()

    async def delete(self, ids: list[str]) -> None:
        await self._store.delete(ids)
        if ids:
            index = self._index()
            for doc_id in ids:
                index.remove(doc_id)
            index.save()

    async def get(self, ids: list[str]) -> list[MemoryDocument]:
        return await self._store.get(ids)

    async def update(self, id: str, document: str, metadata: dict) -> None:
        await self._store.update(id, document, metadata)
        index = self._index()
        index.add(id, document)
        index.save()

    def all_documents(self) -> list[MemoryDocument]:
        """Pass-through so the self-heal rebuild can read the raw store."""
        return self._store.all_documents()  # type: ignore[attr-defined]

    # ── retrieval ──

    async def query(self, query_text: str, top_k: int = 5) -> list[MemoryDocument]:
        """Hybrid retrieval: vector + BM25 fused, thresholded, diversified.

        Falls back to pure semantic search when hybrid is disabled or the
        query carries no keyword signal (empty string).
        """
        if not self._memory_config.hybrid_enabled or not query_text.strip():
            return await self._store.query(query_text, top_k)

        fetch = max(top_k * _OVERFETCH, 10)

        # 1. semantic side
        vec_docs = await self._store.query(query_text, fetch)
        vec_map = {d.id: d for d in vec_docs}

        # 2. keyword side (BM25)
        index = self._index()
        keyword_hits = index.search(query_text, fetch)
        kw_ids = [doc_id for doc_id, _ in keyword_hits]
        kw_docs = await self._store.get(kw_ids) if kw_ids else []
        kw_scores = dict(keyword_hits)

        # 3. fusion — rank-based RRF or OpenClaw-style weighted fusion
        fused: dict[str, float] = {}
        mode = self._memory_config.fusion_mode
        if mode == "rrf":
            fused = self._rrf_fuse(vec_docs, keyword_hits)
        else:  # "weighted" — normalize both score families, then 70/30
            fused = self._weighted_fuse(vec_docs, kw_docs, kw_scores)

        # 4. threshold + build result list
        threshold = self._memory_config.score_threshold
        kw_map = {d.id: d for d in kw_docs}
        candidates: list[MemoryDocument] = []
        for doc_id, score in fused.items():
            if score < threshold:
                continue
            base = vec_map.get(doc_id) or kw_map.get(doc_id)
            if base is not None:
                candidates.append(base.model_copy(update={"score": score}))
        candidates.sort(key=lambda d: d.score, reverse=True)

        # 5. per-source diversity: keep top-1 per source first, then fill
        if self._memory_config.diversity_by_source:
            candidates = self._diversify(candidates, top_k)
        else:
            candidates = candidates[:top_k]

        # 6. optional cross-encoder rerank (graceful degradation)
        if self._memory_config.reranker == "cross_encoder" and len(candidates) > 1:
            candidates = await self._rerank(query_text, candidates)

        return candidates[:top_k]

    async def semantic_query(self, query_text: str, top_k: int = 5) -> list[MemoryDocument]:
        """Pure vector search — used by memory dedup where semantic
        similarity (not keyword overlap) is the right signal."""
        return await self._store.query(query_text, top_k)

    # ── fusion ──

    def _rrf_fuse(
        self,
        vec_docs: list[MemoryDocument],
        keyword_hits: list[tuple[str, float]],
    ) -> dict[str, float]:
        """Reciprocal Rank Fusion: only ranks matter, so the two score
        scales (cosine ∈ [0,1] vs unbounded BM25) never need aligning.
        Final scores are normalized to [0,1] for display/thresholding."""
        ranks: dict[str, float] = {}
        for r, doc in enumerate(vec_docs, start=1):
            ranks[doc.id] = ranks.get(doc.id, 0.0) + 1.0 / (RRF_K + r)
        for r, (doc_id, _) in enumerate(keyword_hits, start=1):
            ranks[doc_id] = ranks.get(doc_id, 0.0) + 1.0 / (RRF_K + r)
        max_score = 2.0 / (RRF_K + 1)  # found by both lists at rank 1
        return {doc_id: s / max_score for doc_id, s in ranks.items()}

    def _weighted_fuse(
        self,
        vec_docs: list[MemoryDocument],
        kw_docs: list[MemoryDocument],
        kw_scores: dict[str, float],
    ) -> dict[str, float]:
        """OpenClaw-style weighted fusion (default 70% vector / 30% text).
        BM25 scores are min-max normalized within the result list first —
        raw BM25 values are not comparable to cosine similarities."""
        cfg = self._memory_config
        fused: dict[str, float] = {}
        for d in vec_docs:
            v = max(0.0, min(1.0, d.score))  # cosine similarity clamp
            fused[d.id] = fused.get(d.id, 0.0) + cfg.vector_weight * v

        kw_values = [kw_scores.get(d.id, 0.0) for d in kw_docs if d.id in kw_scores]
        if kw_values:
            lo, hi = min(kw_values), max(kw_values)
            span = (hi - lo) or 1.0
            for d in kw_docs:
                if d.id not in kw_scores:
                    continue
                norm = (kw_scores[d.id] - lo) / span
                fused[d.id] = fused.get(d.id, 0.0) + cfg.bm25_weight * norm
        return fused

    # ── diversity ──

    @staticmethod
    def _diversify(docs: list[MemoryDocument], top_k: int) -> list[MemoryDocument]:
        """Keep the best hit per source first, then fill remaining slots by
        score. Prevents one long document from crowding out every other
        source in the context window (interview Q: ranking + truncation)."""
        best_per_source: list[MemoryDocument] = []
        rest: list[MemoryDocument] = []
        seen_sources: set[str] = set()
        for doc in docs:
            source = (
                doc.metadata.get("source")
                or doc.metadata.get("filename")
                or doc.id
            )
            if source in seen_sources:
                rest.append(doc)
            else:
                seen_sources.add(source)
                best_per_source.append(doc)
        return (best_per_source + rest)[:top_k]

    # ── reranking ──

    async def _rerank(
        self, query: str, candidates: list[MemoryDocument]
    ) -> list[MemoryDocument]:
        if self._reranker is None:
            self._reranker = self._build_reranker()
        if self._reranker is None:
            return candidates
        try:
            scores = await asyncio.to_thread(
                self._reranker.score, query, [d.content for d in candidates]
            )
        except Exception as exc:  # noqa: BLE001 — graceful degradation
            logger.warning("Cross-encoder rerank failed, keeping fusion order: %s", exc)
            return candidates
        reranked = [
            d.model_copy(update={"score": s})
            for d, s in sorted(
                zip(candidates, scores), key=lambda pair: pair[1], reverse=True
            )
        ]
        return reranked

    def _build_reranker(self):
        model_name = self._memory_config.reranker_model
        try:
            from sentence_transformers import CrossEncoder

            return CrossEncoder(model_name)
        except Exception as exc:  # noqa: BLE001 — offline/model missing
            logger.warning(
                "Cross-encoder '%s' unavailable, skipping rerank: %s", model_name, exc
            )
            return None
