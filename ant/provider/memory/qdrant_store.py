"""Qdrant vector store implementation (Phase 3A).

Replaces ChromaDB as the RAG backend:

- **dual named vectors** per point: ``dense`` (embedding model, size from
  ``QDRANT_VECTOR_SIZE``) + ``sparse`` (BM25 via fastembed
  ``SparseTextEmbedding("Qdrant/bm25")``, computed in a worker thread);
- **hybrid retrieval** via server-side prefetch (dense + sparse) fused
  with Reciprocal Rank Fusion (``prefer_hybrid=True``), or pure dense;
- **payload filters**: ``where=`` on ``query`` and a ``delete_by_filter``
  helper — this is where the ``delete_by_source`` fix lands in one step
  (server-side filter delete, no query-then-delete residue);
- **credentials** come from ``.env`` (``QDRANT_URL`` / ``QDRANT_API_KEY``)
  via :class:`ant.utils.settings.InfraSettings`.  Construction never
  touches the network and never raises; method calls raise
  :class:`QdrantStoreError` when credentials are missing or the service
  is unreachable, so callers can fall back or fail loudly (design
  principle 11).  Logged URLs are always masked.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

from .base import EmbeddingProvider, MemoryDocument, VectorStore

if TYPE_CHECKING:
    from ant.utils.config import Config

logger = logging.getLogger(__name__)

# Server-side fusion overfetches per branch so RRF has room to re-rank.
_PREFETCH_MULTIPLIER = 4
_PREFETCH_MIN = 10


class QdrantStoreError(RuntimeError):
    """Raised when the Qdrant backend cannot be used (credentials missing
    or service unreachable).  Callers decide whether to fall back or fail."""


class QdrantStore(VectorStore):
    """Vector store backed by Qdrant (dense + BM25 sparse, server RRF)."""

    def __init__(
        self,
        config: "Config",
        embedding_provider: EmbeddingProvider,
        settings: Any | None = None,
    ):
        self.config = config
        self.embedding_provider = embedding_provider
        self._settings = settings  # InfraSettings | None (lazy)
        self._client: Any | None = None  # AsyncQdrantClient (lazy)
        self._sparse_embedder: Any | None = None  # fastembed model (lazy)

    # ── client / collection bootstrap ────────────────────────────────────

    def _infra(self) -> Any:
        if self._settings is None:
            from ant.utils.settings import InfraSettings

            self._settings = InfraSettings()
        return self._settings

    async def _client_async(self) -> Any:
        """Lazily build the AsyncQdrantClient and ensure the collection.

        Raises :class:`QdrantStoreError` when credentials are missing —
        clear and actionable, never a half-connected client.
        """
        if self._client is not None:
            return self._client
        infra = self._infra()
        url = infra.qdrant_url()
        api_key = infra.qdrant_api_key()
        if not url or not api_key:
            raise QdrantStoreError(
                "Qdrant credentials missing: set QDRANT_URL and QDRANT_API_KEY in .env "
                f"(url={'set' if url else 'missing'}, api_key={'set' if api_key else 'missing'})"
            )
        import qdrant_client

        client = qdrant_client.AsyncQdrantClient(
            url=url,
            api_key=api_key,
            timeout=infra.qdrant_timeout,
        )
        await self._ensure_collection(client)
        self._client = client
        return client

    async def _ensure_collection(self, client: Any) -> None:
        """Create the collection (dense + sparse named vectors) when missing.

        同时幂等创建 payload 索引——Qdrant 云对 filter 字段有硬性要求，
        无索引的字段做 filter/scroll 会返回 400
        ("Index required but not found")。真云冒烟时发现的。
        """
        infra = self._infra()
        name = infra.qdrant_collection
        exists = False
        try:
            await client.get_collection(name)
            exists = True
        except Exception:  # noqa: BLE001 — assume missing, attempt creation
            pass

        if not exists:
            from qdrant_client.models import (
                Modifier,
                SparseVectorParams,
                VectorParams,
            )

            try:
                await client.create_collection(
                    collection_name=name,
                    vectors_config={
                        "dense": VectorParams(
                            size=infra.qdrant_vector_size,
                            distance=self._parse_distance(infra.qdrant_distance),
                        ),
                    },
                    sparse_vectors_config={
                        "sparse": SparseVectorParams(modifier=Modifier.IDF),
                    },
                )
                logger.info(
                    "Created Qdrant collection %r (dense size=%d, sparse=BM25/IDF)",
                    name,
                    infra.qdrant_vector_size,
                )
            except Exception as exc:  # noqa: BLE001 — wrap into a clear, masked error
                raise QdrantStoreError(
                    f"Qdrant unreachable at {infra.masked_qdrant_url()}: {exc}"
                ) from exc

        # 集合已存在也要补索引（老集合可能从未建过）
        await self._ensure_payload_indexes(client, name)

    async def _ensure_payload_indexes(self, client: Any, collection_name: str) -> None:
        """Idempotently create payload indexes for the fields used in filters.

        尽力而为：索引创建失败只记 debug（filter 查询会自行报错降级），
        绝不影响主链路（设计原则 11）。
        """
        from qdrant_client.models import PayloadSchemaType

        for field, schema_type in {
            "source": PayloadSchemaType.KEYWORD,
            "category": PayloadSchemaType.KEYWORD,
            "session_id": PayloadSchemaType.KEYWORD,
            "importance": PayloadSchemaType.INTEGER,
        }.items():
            try:
                await client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field,
                    field_schema=schema_type,
                    wait=True,
                )
            except Exception:  # noqa: BLE001 — index may already exist
                logger.debug(
                    "Payload index %r on %r already exists or unavailable",
                    field,
                    collection_name,
                )

    @staticmethod
    def _parse_distance(raw: str) -> Any:
        from qdrant_client.models import Distance

        normalized = (raw or "cosine").lower()
        mapping = {
            "cosine": Distance.COSINE,
            "dot": Distance.DOT,
            "euclid": Distance.EUCLID,
            "euclidean": Distance.EUCLID,
            "manhattan": Distance.MANHATTAN,
        }
        if normalized not in mapping:
            raise QdrantStoreError(
                f"Unsupported QDRANT_DISTANCE={raw!r}; use cosine/dot/euclid/manhattan"
            )
        return mapping[normalized]

    # ── embedding helpers ────────────────────────────────────────────────

    async def _dense_vectors(self, texts: list[str]) -> list[list[float]]:
        aembed = getattr(self.embedding_provider, "aembed", None)
        if aembed is not None:
            return await aembed(texts)
        return await self.embedding_provider.embed(texts)

    async def _sparse_vectors(self, texts: list[str]) -> list[dict[str, Any]]:
        """BM25 sparse vectors via fastembed, computed off the event loop."""
        if self._sparse_embedder is None:
            from fastembed import SparseTextEmbedding

            self._sparse_embedder = SparseTextEmbedding("Qdrant/bm25")
        return await asyncio.to_thread(self._sparse_embed_sync, texts)

    def _sparse_embed_sync(self, texts: list[str]) -> list[dict[str, Any]]:
        results = self._sparse_embedder.embed(texts)
        return [
            {
                "indices": [int(i) for i in emb.indices],
                "values": [float(v) for v in emb.values],
            }
            for emb in results
        ]

    @staticmethod
    def _normalize_id(doc_id: Any) -> Any:
        """Qdrant point id 只接受 int/UUID——任意字符串 id 确定性归一化。

        字符串 id（如 doc_ingester 的 sha256 chunk id）经 uuid5 转成
        确定性 UUID（无状态映射，重入幂等）；已是 UUID/int 的原样返回。
        真云冒烟时发现：透传字符串 id 会 400 ("not a valid point ID")。
        """
        if isinstance(doc_id, int):
            return doc_id
        if isinstance(doc_id, uuid.UUID):
            return doc_id
        try:
            return uuid.UUID(str(doc_id))
        except (ValueError, AttributeError):
            return str(uuid.uuid5(uuid.NAMESPACE_DNS, str(doc_id)))

    @staticmethod
    def _build_payload(content: str, metadata: dict, original_id: Any = None) -> dict:
        """Qdrant payload: metadata fields + full content + content preview.

        The full text must be stored so ``get()``/``query()`` can rebuild
        :class:`MemoryDocument` faithfully (migration, BM25 rebuild, RAG
        injection); ``content_preview`` is the cheap summary for listings.
        ``original_id``（与归一化 id 不同时）写入 ``_original_id``，供读取
        时透明还原——调用方传入的字符串 id 经 uuid5 归一化后不可逆。
        """
        payload = dict(metadata or {})
        payload["content"] = content
        payload["content_preview"] = (content[:200] or content)
        if original_id is not None:
            payload["_original_id"] = str(original_id)
        return payload

    @staticmethod
    def _restore_id(record_id: Any, payload: dict) -> str:
        """读取路径：payload 有 _original_id 时还原调用方原始 id。"""
        return str(payload.get("_original_id", record_id))

    @staticmethod
    def _clean_metadata(payload: dict) -> dict:
        """Strip the internal content keys, leaving only user metadata."""
        return {
            k: v
            for k, v in payload.items()
            if k not in ("content", "content_preview", "_original_id")
        }

    # ── VectorStore protocol ─────────────────────────────────────────────

    async def add(
        self,
        documents: list[str],
        metadatas: list[dict] | None = None,
        ids: list[str] | None = None,
    ) -> None:
        """Add documents with dense + sparse vectors and payload metadata."""
        if not documents:
            return
        client = await self._client_async()
        if ids is None:
            ids = [str(uuid.uuid4()) for _ in documents]
        if metadatas is None:
            metadatas = [{} for _ in documents]
        if len(ids) != len(documents) or len(metadatas) != len(documents):
            raise ValueError("ids/metadatas length must match documents")

        dense = await self._dense_vectors(documents)
        sparse = await self._sparse_vectors(documents)
        if len(dense) != len(documents) or len(sparse) != len(documents):
            raise QdrantStoreError(
                f"Embedding produced {len(dense)} dense / {len(sparse)} sparse vectors "
                f"for {len(documents)} documents"
            )

        from qdrant_client.models import PointStruct

        points = [
            PointStruct(
                id=self._normalize_id(doc_id),
                vector={"dense": d, "sparse": s},
                payload=self._build_payload(doc, meta, original_id=doc_id),
            )
            for doc_id, doc, meta, d, s in zip(ids, documents, metadatas, dense, sparse)
        ]
        await client.upsert(collection_name=self._collection_name(), points=points)
        logger.info("Qdrant upsert: %d point(s) into %r", len(points), self._collection_name())

    async def delete(self, ids: list[str]) -> None:
        """Delete documents by ids."""
        if not ids:
            return
        client = await self._client_async()
        await client.delete(
            collection_name=self._collection_name(),
            points_selector=[self._normalize_id(i) for i in ids],
        )

    async def get(self, ids: list[str]) -> list[MemoryDocument]:
        """Retrieve documents by ids (payload only, no vectors)."""
        if not ids:
            return []
        client = await self._client_async()
        records = await client.retrieve(
            collection_name=self._collection_name(),
            ids=[self._normalize_id(i) for i in ids],
            with_payload=True,
            with_vectors=False,
        )
        docs = []
        for record in records:
            payload = record.payload or {}
            docs.append(
                MemoryDocument(
                    id=self._restore_id(record.id, payload),
                    content=payload.get("content", ""),
                    metadata=self._clean_metadata(payload),
                )
            )
        return docs

    async def update(self, id: str, document: str, metadata: dict) -> None:
        """Update (upsert) a single document with fresh vectors."""
        client = await self._client_async()
        dense = await self._dense_vectors([document])
        sparse = await self._sparse_vectors([document])

        from qdrant_client.models import PointStruct

        point = PointStruct(
            id=self._normalize_id(id),
            vector={"dense": dense[0], "sparse": sparse[0]},
            payload=self._build_payload(document, metadata, original_id=id),
        )
        await client.upsert(collection_name=self._collection_name(), points=[point])

    async def query(
        self,
        query_text: str,
        top_k: int = 5,
        prefer_hybrid: bool = True,
        where: Any | None = None,
    ) -> list[MemoryDocument]:
        """Hybrid (dense prefetch + sparse prefetch + RRF) or pure dense query.

        ``where`` is passed through to the server as the payload filter —
        accepts a Qdrant ``Filter`` or a plain filter dict.  Result scores
        are min-max normalized to [0, 1] for cross-backend consistency.
        """
        if not query_text.strip() or top_k <= 0:
            return []
        client = await self._client_async()
        dense = (await self._dense_vectors([query_text]))[0]

        if not prefer_hybrid:
            return await self._dense_query(client, dense, top_k, where)

        try:
            sparse = (await self._sparse_vectors([query_text]))[0]
        except Exception as exc:  # noqa: BLE001 — graceful degradation
            logger.warning(
                "Sparse (BM25) embedding unavailable (%s) — falling back to dense-only",
                exc,
            )
            return await self._dense_query(client, dense, top_k, where)

        from qdrant_client.models import Fusion, FusionQuery, Prefetch, SparseVector

        overfetch = max(top_k * _PREFETCH_MULTIPLIER, _PREFETCH_MIN)
        result = await client.query_points(
            collection_name=self._collection_name(),
            prefetch=[
                Prefetch(query=dense, using="dense", limit=overfetch),
                Prefetch(
                    query=SparseVector(indices=sparse["indices"], values=sparse["values"]),
                    using="sparse",
                    limit=overfetch,
                ),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            limit=top_k,
            with_payload=True,
            with_vectors=False,
            query_filter=where,
        )
        return self._docs_from_result(result)

    async def _dense_query(
        self, client: Any, dense: list[float], top_k: int, where: Any | None
    ) -> list[MemoryDocument]:
        """Pure dense vector search against the ``dense`` named vector."""
        result = await client.query_points(
            collection_name=self._collection_name(),
            query=dense,
            using="dense",
            limit=top_k,
            with_payload=True,
            with_vectors=False,
            query_filter=where,
        )
        return self._docs_from_result(result)

    async def delete_by_filter(self, where: dict[str, Any]) -> int:
        """Delete every point matching a simple payload filter in one step.

        Accepts a plain mapping like ``{"source": "docs/guide.md"}``
        (list values → ``MatchAny``).  Returns the number of points
        deleted — the Phase-3 one-step fix for ``delete_by_source``
        (the old Chroma path queried top-1 and left residue behind).
        """
        if not where:
            return 0
        client = await self._client_async()
        qfilter = self._build_filter(where)

        ids: list[str] = []
        offset: Any = None
        while True:
            records, next_offset = await client.scroll(
                collection_name=self._collection_name(),
                scroll_filter=qfilter,
                limit=100,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            ids.extend(str(record.id) for record in records)
            if not records or next_offset is None:
                break
            offset = next_offset

        if ids:
            await client.delete(collection_name=self._collection_name(), points_selector=ids)
        logger.info("delete_by_filter(%r) removed %d point(s)", where, len(ids))
        return len(ids)

    # ── helpers ──────────────────────────────────────────────────────────

    def _collection_name(self) -> str:
        return self._infra().qdrant_collection or "ant_memory"

    @staticmethod
    def _build_filter(where: dict[str, Any]) -> Any:
        """Convert ``{field: value | list}`` into a Qdrant ``Filter``."""
        # 注意：新版 qdrant-client 的 models.Match 是 typing.Union 别名，
        # 不可实例化（Cannot instantiate typing.Union）——必须用 MatchValue/MatchAny。
        from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

        conditions = []
        for key, value in where.items():
            if value is None:
                continue
            if isinstance(value, list):
                conditions.append(
                    FieldCondition(key=key, match=MatchAny(any=[str(v) for v in value]))
                )
            else:
                conditions.append(FieldCondition(key=key, match=MatchValue(value=value)))
        return Filter(must=conditions)

    @staticmethod
    def _docs_from_result(result: Any) -> list[MemoryDocument]:
        """Convert a ``query_points`` response into MemoryDocuments with
        scores min-max normalized to [0, 1]."""
        points = getattr(result, "points", None) or []
        docs: list[MemoryDocument] = []
        for point in points:
            payload = point.payload or {}
            docs.append(
                MemoryDocument(
                    id=QdrantStore._restore_id(point.id, payload),
                    content=payload.get("content", ""),
                    metadata=QdrantStore._clean_metadata(payload),
                    score=float(point.score or 0.0),
                )
            )
        if docs:
            max_score = max(doc.score for doc in docs)
            if max_score > 0:
                for doc in docs:
                    doc.score = doc.score / max_score
        return docs
