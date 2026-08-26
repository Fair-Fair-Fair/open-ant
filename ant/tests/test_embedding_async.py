"""Phase-3A aembed tests — no network.

Covers: the API path (fake httpx client, incl. missing-key and HTTP-error
branches), the local path (sync encode offloaded via asyncio.to_thread),
the Redis cache (hit → no recompute; miss → compute + write back), and
Redis-down degradation (warning, direct computation keeps working).
"""

import hashlib
import json
import logging
from types import SimpleNamespace

import pytest

from ant.provider.memory.embedding import (
    EMBED_CACHE_PREFIX,
    EmbeddingError,
    LiteLLMEmbeddingProvider,
)


def make_provider(cache_enabled=True):
    """LiteLLMEmbeddingProvider with a lightweight fake config."""
    memory = SimpleNamespace(
        embedding_model="BAAI/bge-small-zh-v1.5",
        embedding_cache_enabled=cache_enabled,
    )
    llm = SimpleNamespace(api_key="sk-test", api_base=None)
    return LiteLLMEmbeddingProvider(SimpleNamespace(memory=memory, llm=llm))


class FakeResponse:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data
        self.text = ""

    def json(self):
        return self._data


class FakeHttpxClient:
    """Records the request; returns a canned response."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        self.calls.append((url, json, headers))
        return self.response


# ── API path (EMBED_MODEL_TYPE != "local") ──────────────────────────────


async def test_aembed_api_path_posts_to_embeddings_endpoint(monkeypatch):
    import ant.provider.memory.embedding as embedding_mod

    response = FakeResponse(data={"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]})
    fake_client = FakeHttpxClient(response)
    monkeypatch.setattr(embedding_mod.httpx, "AsyncClient", lambda **kwargs: fake_client)
    monkeypatch.setenv("EMBED_MODEL_TYPE", "dashscope")
    monkeypatch.setenv("EMBED_API_KEY", "sk-embed-test")
    monkeypatch.delenv("EMBED_MODEL_NAME", raising=False)
    monkeypatch.delenv("EMBED_BASE_URL", raising=False)

    provider = make_provider(cache_enabled=False)
    out = await provider.aembed(["text one", "text two"])

    assert out == [[0.1, 0.2], [0.3, 0.4]]
    url, payload, headers = fake_client.calls[0]
    assert url.endswith("/embeddings")
    assert payload["model"] == "BAAI/bge-small-zh-v1.5"
    assert payload["input"] == ["text one", "text two"]
    assert headers["Authorization"] == "Bearer sk-embed-test"


async def test_aembed_api_non_200_raises_embedding_error(monkeypatch):
    import ant.provider.memory.embedding as embedding_mod

    fake_client = FakeHttpxClient(FakeResponse(status_code=429))
    monkeypatch.setattr(embedding_mod.httpx, "AsyncClient", lambda **kwargs: fake_client)
    monkeypatch.setenv("EMBED_MODEL_TYPE", "dashscope")
    monkeypatch.setenv("EMBED_API_KEY", "sk-embed-test")
    monkeypatch.delenv("EMBED_MODEL_NAME", raising=False)
    monkeypatch.delenv("EMBED_BASE_URL", raising=False)

    provider = make_provider(cache_enabled=False)
    with pytest.raises(EmbeddingError, match="HTTP 429"):
        await provider.aembed(["x"])


async def test_aembed_api_missing_key_raises_clear_error(monkeypatch):
    monkeypatch.setenv("EMBED_MODEL_TYPE", "dashscope")
    monkeypatch.delenv("EMBED_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    provider = make_provider(cache_enabled=False)
    with pytest.raises(EmbeddingError, match="EMBED_API_KEY"):
        await provider.aembed(["x"])


# ── local path (EMBED_MODEL_TYPE == "local") ────────────────────────────


async def test_aembed_local_runs_sync_encode_in_thread(monkeypatch):
    monkeypatch.setenv("EMBED_MODEL_TYPE", "local")
    provider = make_provider(cache_enabled=False)

    calls = []

    def fake_encode_sync(texts):
        calls.append(list(texts))
        return [[1.0, 2.0] for _ in texts]

    provider._local_encode_sync = fake_encode_sync  # monkeypatch the sync encode
    out = await provider.aembed(["a", "b"])

    assert out == [[1.0, 2.0], [1.0, 2.0]]
    assert calls == [["a", "b"]]  # encode happened once, in the thread


async def test_aembed_empty_input_returns_empty():
    provider = make_provider()
    assert await provider.aembed([]) == []


# ── Redis cache ─────────────────────────────────────────────────────────


class FakeRedis:
    """Minimal fake for redis.asyncio.Redis (mget / pipeline set)."""

    def __init__(self, store=None, fail_commands=False):
        self.store = store if store is not None else {}
        self.fail_commands = fail_commands
        self.set_calls = []
        self.mget_calls = 0

    async def mget(self, keys):
        self.mget_calls += 1
        if self.fail_commands:
            raise ConnectionError("fake redis down")
        return [self.store.get(k) for k in keys]

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self._ops = []

    def set(self, key, value, ex=None):
        self._ops.append((key, value, ex))
        return self

    async def execute(self):
        for key, value, ex in self._ops:
            self.redis.store[key] = value
            self.redis.set_calls.append((key, ex))


def patch_redis(monkeypatch, fake):
    """Replace redis.asyncio.Redis.from_url with the fake."""

    import redis.asyncio as redis_async

    class _FakeRedisFactory:
        @staticmethod
        def from_url(url, **kwargs):
            return fake

    monkeypatch.setattr(redis_async, "Redis", _FakeRedisFactory)


async def test_aembed_cache_hit_skips_recompute(monkeypatch):
    monkeypatch.setenv("EMBED_MODEL_TYPE", "local")
    key_alpha = EMBED_CACHE_PREFIX + hashlib.sha256(b"alpha").hexdigest()
    key_beta = EMBED_CACHE_PREFIX + hashlib.sha256(b"beta").hexdigest()
    fake = FakeRedis(store={key_alpha: json.dumps([9.0, 9.0])})
    patch_redis(monkeypatch, fake)

    provider = make_provider()
    calls = []

    def fake_encode_sync(texts):
        calls.append(list(texts))
        return [[1.0, 2.0] for _ in texts]

    provider._local_encode_sync = fake_encode_sync
    out = await provider.aembed(["alpha", "beta"])

    assert out[0] == [9.0, 9.0]  # cache hit — returned without recompute
    assert out[1] == [1.0, 2.0]  # cache miss — computed once
    assert calls == [["beta"]]  # only the miss reached the embedder
    assert [k for k, _ in fake.set_calls] == [key_beta]  # write-back only for miss


async def test_aembed_redis_down_degrades_to_direct(monkeypatch, caplog):
    monkeypatch.setenv("EMBED_MODEL_TYPE", "local")
    patch_redis(monkeypatch, FakeRedis(fail_commands=True))

    provider = make_provider()
    calls = []
    provider._local_encode_sync = (
        lambda texts: calls.append(list(texts)) or [[7.0] for _ in texts]
    )

    with caplog.at_level(logging.WARNING, logger="ant.provider.memory.embedding"):
        out = await provider.aembed(["only"])

    # Redis outage never breaks the main path — direct computation wins
    assert out == [[7.0]]
    assert calls == [["only"]]
    assert any("Redis" in record.getMessage() for record in caplog.records)


async def test_aembed_second_call_skips_redis_after_outage(monkeypatch):
    """After one Redis failure the cache is disabled for good (no retry
    latency on the hot path)."""
    monkeypatch.setenv("EMBED_MODEL_TYPE", "local")
    fake = FakeRedis(fail_commands=True)
    patch_redis(monkeypatch, fake)

    provider = make_provider()
    provider._local_encode_sync = lambda texts: [[5.0] for _ in texts]

    await provider.aembed(["a"])
    await provider.aembed(["b"])

    assert fake.mget_calls == 1  # second call never touches Redis
    assert provider._redis_unavailable is True
