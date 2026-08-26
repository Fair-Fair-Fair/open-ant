"""Embedding provider: legacy litellm ``embed`` + Phase-3 ``aembed`` pipeline.

- ``embed()`` — the original entry point used by the Chroma backend
  (litellm ``aembedding``); kept unchanged so the legacy path keeps working.
- ``aembed()`` — the Phase-3 entry point used by QdrantStore and the
  document ingester.  Dispatches on the ``EMBED_MODEL_TYPE`` env var
  (per the ``.env`` convention: ``local`` → sentence-transformers,
  anything else/empty → API):

    * ``local`` → sentence-transformers encode offloaded via
      ``asyncio.to_thread`` so the event loop is never blocked
      (design principle 4);
    * otherwise → httpx POST to an OpenAI-compatible ``/embeddings``
      endpoint (DashScope by default); API key from ``EMBED_API_KEY``
      (fallback ``DASHSCOPE_API_KEY``), model from ``EMBED_MODEL_NAME``
      (fallback ``config.memory.embedding_model``).  A missing key is a
      clear error, never a silent fallback.

When ``memory.embedding_cache_enabled`` is set, vectors are cached in
Redis (``sha256(text)`` → JSON vector, 30-day TTL).  A Redis outage
degrades to direct computation with a warning — it never breaks the
main path (design principle 11).
"""

import asyncio
import hashlib
import json
import logging
import os
from typing import TYPE_CHECKING, Any

import httpx
from litellm import aembedding

from .base import EmbeddingProvider

if TYPE_CHECKING:
    from ant.utils.config import Config

logger = logging.getLogger(__name__)

EMBED_CACHE_PREFIX = "ant:embed:"
EMBED_CACHE_TTL_SECONDS = 30 * 24 * 3600  # 30 days
EMBED_API_TIMEOUT_SECONDS = 30.0
DEFAULT_EMBED_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class EmbeddingError(RuntimeError):
    """Raised when the embedding pipeline cannot produce vectors
    (missing API key, API failure, malformed response)."""


class LiteLLMEmbeddingProvider(EmbeddingProvider):
    """Embedding provider using litellm (legacy) + Phase-3 aembed pipeline."""

    def __init__(self, config: "Config"):
        self.model = config.memory.embedding_model
        self.api_key = config.llm.api_key
        self.api_base = config.llm.api_base
        self._cache_enabled = bool(getattr(config.memory, "embedding_cache_enabled", True))
        self._redis: Any | None = None  # redis.asyncio client (lazy)
        self._redis_unavailable = False
        self._local_provider = None  # lazy sentence-transformers provider
        self._infra_settings: Any | None = None  # lazy InfraSettings

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts using litellm (legacy path — unchanged)."""
        if not texts:
            return []

        kwargs: dict = {
            "model": self.model,
            "input": texts,
            "api_key": self.api_key,
        }
        if self.api_base:
            kwargs["api_base"] = self.api_base

        response = await aembedding(**kwargs)
        return [item["embedding"] for item in response.data]

    # ── Phase-3: async embedding with Redis cache ────────────────────────

    async def aembed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts, caching vectors in Redis when enabled.

        Dispatches on ``EMBED_MODEL_TYPE``: ``local`` runs the
        sentence-transformers encode in a worker thread; anything else
        uses the OpenAI-compatible HTTP embedding API.
        """
        if not texts:
            return []
        if not self._cache_enabled:
            return await self._embed_uncached(texts)
        return await self._embed_cached(texts)

    async def _embed_uncached(self, texts: list[str]) -> list[list[float]]:
        embed_type = (os.environ.get("EMBED_MODEL_TYPE") or "").strip().lower()
        if embed_type == "local":
            return await self._embed_local(texts)
        return await self._embed_api(texts)

    async def _embed_local(self, texts: list[str]) -> list[list[float]]:
        """Local sentence-transformers encode, off the event loop.

        NOTE: ``aembed`` can't reuse the async ``embed()`` here — an async
        function wrapped in ``asyncio.to_thread`` returns a coroutine and
        its body would run (and block) on the main loop.  The sync encode
        runs in the worker thread instead.
        """
        return await asyncio.to_thread(self._local_encode_sync, texts)

    def _local_encode_sync(self, texts: list[str]) -> list[list[float]]:
        if self._local_provider is None:
            from .sentence_transformer import SentenceTransformerEmbeddingProvider

            self._local_provider = SentenceTransformerEmbeddingProvider(self.model)
        embeddings = self._local_provider.model.encode(texts, normalize_embeddings=True)
        return embeddings.tolist()

    async def _embed_api(self, texts: list[str]) -> list[list[float]]:
        """OpenAI-compatible ``/embeddings`` POST via httpx (DashScope default)."""
        api_key = os.environ.get("EMBED_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
        if not api_key:
            raise EmbeddingError(
                "EMBED_MODEL_TYPE is not 'local' but no embedding API key is set — "
                "add EMBED_API_KEY (or DASHSCOPE_API_KEY) to .env"
            )
        base_url = (os.environ.get("EMBED_BASE_URL") or DEFAULT_EMBED_BASE_URL).rstrip("/")
        model = os.environ.get("EMBED_MODEL_NAME") or self.model
        headers = {"Authorization": f"Bearer {api_key}"}
        payload = {"model": model, "input": texts}
        try:
            async with httpx.AsyncClient(timeout=EMBED_API_TIMEOUT_SECONDS) as client:
                resp = await client.post(f"{base_url}/embeddings", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"Embedding API request failed: {exc}") from exc
        if resp.status_code != 200:
            raise EmbeddingError(
                f"Embedding API error: HTTP {resp.status_code} — {resp.text[:200]}"
            )
        data = resp.json()
        try:
            embeddings = [item["embedding"] for item in data["data"]]
        except (KeyError, TypeError) as exc:
            raise EmbeddingError(f"Malformed embedding API response: {exc}") from exc
        if len(embeddings) != len(texts):
            raise EmbeddingError(
                f"Embedding API returned {len(embeddings)} vectors for {len(texts)} texts"
            )
        return embeddings

    async def _embed_cached(self, texts: list[str]) -> list[list[float]]:
        """Redis-cached embedding: hit → return, miss → compute + write back.

        Any Redis failure marks the cache unavailable for this provider
        instance and degrades to direct computation (warning only) — the
        main path keeps working (design principle 11).
        """
        redis = await self._get_redis()
        keys = [self._cache_key(text) for text in texts]
        cached: list[list[float] | None] = [None] * len(texts)
        if redis is not None:
            try:
                raw_values = await redis.mget(keys)
                for i, raw in enumerate(raw_values):
                    if raw:
                        cached[i] = json.loads(raw)
            except Exception as exc:  # noqa: BLE001 — cache must never break the main path
                self._log_redis_unavailable("read", exc)
                redis = None
        misses = [i for i, value in enumerate(cached) if value is None]
        if misses:
            computed = await self._embed_uncached([texts[i] for i in misses])
            for i, vector in zip(misses, computed):
                cached[i] = vector
            if redis is not None:
                try:
                    pipe = redis.pipeline()
                    for i in misses:
                        pipe.set(
                            keys[i],
                            json.dumps(cached[i], ensure_ascii=False),
                            ex=EMBED_CACHE_TTL_SECONDS,
                        )
                    await pipe.execute()
                except Exception as exc:  # noqa: BLE001
                    self._log_redis_unavailable("write", exc)
        return [vector for vector in cached if vector is not None]

    async def _get_redis(self) -> Any | None:
        """Lazy redis.asyncio client from ``InfraSettings.redis_url``.

        Never raises: on any failure the cache is disabled for this
        provider instance and computation proceeds directly.
        """
        # 顺序关键：降级标志必须先于缓存 client 判断——读失败后 client 仍是
        # 坏实例，先返回它会让每次调用都重试 mget，违背"一次失败永久降级"。
        if self._redis_unavailable:
            return None
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as redis_async

            self._redis = redis_async.Redis.from_url(self._infra().redis_url)
        except Exception as exc:  # noqa: BLE001
            self._log_redis_unavailable("init", exc)
            self._redis = None
        return self._redis

    def _log_redis_unavailable(self, reason: str, exc: Exception) -> None:
        if self._redis_unavailable:
            return
        self._redis_unavailable = True
        logger.warning(
            "Redis embedding cache unavailable (%s failed: %s) — degrading to direct "
            "computation; retrieval keeps working",
            reason,
            exc,
        )

    @staticmethod
    def _cache_key(text: str) -> str:
        return EMBED_CACHE_PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _infra(self) -> Any:
        if self._infra_settings is None:
            from ant.utils.settings import InfraSettings

            self._infra_settings = InfraSettings()
        return self._infra_settings
