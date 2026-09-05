"""Phase-3A QdrantStore tests — no network (fake AsyncQdrantClient).

Covers: collection auto-creation with dual named vectors, add/update/
delete/get round-trip, hybrid prefetch structure (dense+sparse+RRF),
pure-dense fallback, payload-filter passthrough, one-step
``delete_by_filter``, score normalization, and clear errors for missing
credentials / unreachable service (with the API key never leaking).
"""

from types import SimpleNamespace

import pytest

from ant.provider.memory.qdrant_store import QdrantStore, QdrantStoreError


class FakePoint:
    """Stand-in for a qdrant_client Record."""

    def __init__(self, id, payload=None, score=None):
        self.id = id
        self.payload = payload or {}
        self.score = score


class FakeSparseEmbedding:
    """Stand-in for a fastembed SparseEmbedding."""

    def __init__(self, indices, values):
        self.indices = indices
        self.values = values


class FakeSparseEmbedder:
    """Stand-in for fastembed SparseTextEmbedding (sync .embed)."""

    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [FakeSparseEmbedding([1, 3], [0.5, 0.25]) for _ in texts]


class FakeEmbedder:
    """Embedding provider returning deterministic dense vectors."""

    def __init__(self, vector_size=16, fail=False):
        self.vector_size = vector_size
        self.fail = fail
        self.calls = []

    async def aembed(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("embedding API down (fake)")
        return [[0.1 * (i + 1)] * self.vector_size for i in range(len(texts))]


class FakeInfraSettings:
    """Duck-typed InfraSettings for the store (never touches .env)."""

    def __init__(
        self,
        url="https://qdrant.example",
        api_key="secret",
        collection="test_col",
        vector_size=16,
        distance="Cosine",
        timeout=5,
    ):
        self._url = url
        self._api_key = api_key
        self.qdrant_collection = collection
        self.qdrant_vector_size = vector_size
        self.qdrant_distance = distance
        self.qdrant_timeout = timeout

    def qdrant_url(self):
        return self._url

    def qdrant_api_key(self):
        return self._api_key

    def masked_qdrant_url(self):
        return "https://qdrant.example"


class FakeQdrantClient:
    """In-memory stand-in for qdrant_client.AsyncQdrantClient.

    Stores points like the real server and records every call for
    structural assertions (prefetch shape, filter passthrough, …).
    """

    def __init__(self):
        self.points = {}  # id -> {"dense": ..., "sparse": ..., "payload": ...}
        self.collection_exists = False
        self.fail_connect = False
        self.created_collections = []
        self.get_collection_calls = 0
        self.upserts = []
        self.deletes = []
        self.retrieves = []
        self.scrolls = []
        self.query_calls = []
        self.query_points_result = []

    async def get_collection(self, name):
        self.get_collection_calls += 1
        if self.fail_connect:
            raise RuntimeError("connection refused (fake)")
        if not self.collection_exists:
            raise RuntimeError(f"Collection {name} not found (fake 404)")
        return {"name": name}

    async def create_collection(self, **kwargs):
        if self.fail_connect:
            raise RuntimeError("connection refused (fake)")
        self.created_collections.append(kwargs)
        self.collection_exists = True

    async def upsert(self, collection_name=None, points=None, **kwargs):
        for point in points:
            self.points[str(point.id)] = {
                "dense": point.vector["dense"],
                "sparse": point.vector["sparse"],
                "payload": point.payload,
            }
        self.upserts.append((collection_name, list(points)))

    async def retrieve(self, collection_name=None, ids=None, **kwargs):
        self.retrieves.append((collection_name, list(ids)))
        return [
            FakePoint(id=pid, payload=self.points[str(pid)]["payload"])
            for pid in ids
            if str(pid) in self.points
        ]

    async def delete(self, collection_name=None, points_selector=None, **kwargs):
        self.deletes.append((collection_name, points_selector))
        selector = points_selector if isinstance(points_selector, list) else []
        for pid in selector:
            self.points.pop(str(pid), None)

    async def scroll(
        self, collection_name=None, scroll_filter=None, limit=100, offset=None, **kwargs
    ):
        self.scrolls.append((collection_name, scroll_filter, limit, offset))
        matched = [
            FakePoint(id=pid, payload=rec["payload"])
            for pid, rec in self.points.items()
            if self._matches(scroll_filter, rec["payload"])
        ]
        return matched, None  # single batch — offset semantics irrelevant here

    async def query_points(self, **kwargs):
        self.query_calls.append(kwargs)
        return SimpleNamespace(points=self.query_points_result)

    @staticmethod
    def _matches(qfilter, payload):
        if qfilter is None:
            return True
        for cond in qfilter.must or []:
            value = payload.get(cond.key)
            match = cond.match
            if match is None:
                continue
            # 新版 qdrant-client：match 是 Union[MatchValue, MatchAny, ...]
            if hasattr(match, "value") and value != match.value:
                return False
            if hasattr(match, "any") and value not in match.any:
                return False
        return True


def make_store(monkeypatch, settings=None):
    """Build a QdrantStore with fake client/embedder/sparse-model."""
    import qdrant_client

    fake = FakeQdrantClient()
    captured = {}

    def fake_client_factory(**kwargs):
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(qdrant_client, "AsyncQdrantClient", fake_client_factory)
    store = QdrantStore(
        config=SimpleNamespace(),
        embedding_provider=FakeEmbedder(),
        settings=settings or FakeInfraSettings(),
    )
    store._sparse_embedder = FakeSparseEmbedder()
    return store, fake, captured


# ── collection bootstrap ────────────────────────────────────────────────


async def test_collection_auto_created_with_dual_vectors(monkeypatch):
    store, fake, captured = make_store(monkeypatch)

    await store.add(documents=["hello world"], metadatas=[{"source": "a.md"}], ids=["p1"])

    # client built from InfraSettings values
    assert captured["url"] == "https://qdrant.example"
    assert captured["api_key"] == "secret"
    # missing collection detected + created exactly once
    assert fake.get_collection_calls == 1
    assert len(fake.created_collections) == 1
    cfg = fake.created_collections[0]
    assert cfg["collection_name"] == "test_col"
    dense = cfg["vectors_config"]["dense"]
    assert dense.size == 16
    assert dense.distance.value == "Cosine"
    assert "sparse" in cfg["sparse_vectors_config"]

    # second call reuses the existing client + collection
    await store.add(documents=["again"], ids=["p2"])
    assert len(fake.created_collections) == 1
    assert fake.get_collection_calls == 1


# ── protocol round-trip ─────────────────────────────────────────────────


async def test_add_get_update_delete_round_trip(monkeypatch):
    store, fake, _ = make_store(monkeypatch)

    await store.add(
        documents=["alpha content", "beta content"],
        metadatas=[{"source": "a.md", "importance": 8}, {"source": "b.md", "importance": 3}],
        ids=["a", "b"],
    )

    # payload = metadata + full content + content preview; dual vectors
    # 字符串 id 经 _normalize_id 归一化为 UUID，假存储按 str(point.id) 键控
    key_a = str(QdrantStore._normalize_id("a"))
    key_b = str(QdrantStore._normalize_id("b"))
    rec = fake.points[key_a]
    assert rec["payload"]["source"] == "a.md"
    assert rec["payload"]["importance"] == 8
    assert rec["payload"]["content"] == "alpha content"
    assert rec["payload"]["content_preview"] == "alpha content"
    assert rec["payload"]["_original_id"] == "a"  # 读取路径透明还原
    assert rec["dense"] == [0.1] * 16
    assert rec["sparse"].indices == [1, 3]
    assert rec["sparse"].values == [0.5, 0.25]

    got = await store.get(["a", "b", "missing"])
    assert [d.id for d in got] == ["a", "b"]
    assert got[0].content == "alpha content"
    assert got[0].metadata == {"source": "a.md", "importance": 8}

    await store.update("a", "alpha v2", {"source": "a.md", "importance": 9})
    assert fake.points[key_a]["payload"]["content"] == "alpha v2"
    assert fake.points[key_a]["payload"]["importance"] == 9

    await store.delete(["a"])
    assert key_a not in fake.points
    assert key_b in fake.points


# ── query: hybrid prefetch / pure dense / filters / scores ──────────────


async def test_query_hybrid_uses_dense_sparse_prefetch_with_rrf(monkeypatch):
    store, fake, _ = make_store(monkeypatch)

    await store.query("hello world", top_k=5)

    call = fake.query_calls[-1]
    assert call["collection_name"] == "test_col"
    assert call["limit"] == 5
    assert call["with_payload"] is True
    prefetches = call["prefetch"]
    assert len(prefetches) == 2
    assert {p.using for p in prefetches} == {"dense", "sparse"}
    assert all(p.limit >= 10 for p in prefetches)  # overfetch before RRF
    assert call["query"].fusion.value == "rrf"


async def test_query_pure_dense_skips_prefetch(monkeypatch):
    store, fake, _ = make_store(monkeypatch)

    await store.query("hello", top_k=3, prefer_hybrid=False)

    call = fake.query_calls[-1]
    assert "prefetch" not in call
    assert call["using"] == "dense"
    assert call["limit"] == 3


async def test_query_filter_passthrough(monkeypatch):
    # Match 在新版 qdrant-client 是 Union 别名不可实例化，用 MatchValue
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    store, fake, _ = make_store(monkeypatch)

    where = Filter(must=[FieldCondition(key="source", match=MatchValue(value="docs/a.md"))])
    await store.query("hello", top_k=2, where=where)
    assert fake.query_calls[-1]["query_filter"] is where

    # 普通 dict 会归一化为 Filter 模型（Phase 7 修复：docstring 声称支持
    # 字典但实现把 dict 直接塞给 query_filter，真云会 400）
    await store.query("hello", top_k=2, where={"source": "docs/b.md"})
    normalized = fake.query_calls[-1]["query_filter"]
    assert isinstance(normalized, Filter)
    assert normalized.must[0].key == "source"
    assert normalized.must[0].match.value == "docs/b.md"


async def test_query_scores_min_max_normalized(monkeypatch):
    store, fake, _ = make_store(monkeypatch)
    fake.query_points_result = [
        FakePoint(id="p1", payload={"content": "c1", "source": "s1"}, score=0.8),
        FakePoint(id="p2", payload={"content": "c2", "source": "s2"}, score=0.2),
    ]

    docs = await store.query("hello", top_k=2)

    assert [d.id for d in docs] == ["p1", "p2"]
    assert docs[0].score == 1.0
    assert docs[1].score == 0.25
    assert docs[0].content == "c1"
    assert docs[0].metadata == {"source": "s1"}


# ── delete_by_filter: the one-step delete_by_source fix ─────────────────


async def test_delete_by_filter_removes_all_matching_points(monkeypatch):
    store, fake, _ = make_store(monkeypatch)
    for pid, source in (("c1", "docs/a.md"), ("c2", "docs/a.md"), ("c3", "docs/b.md")):
        fake.points[pid] = {
            "dense": [],
            "sparse": {},
            "payload": {"source": source, "content": "x"},
        }

    n = await store.delete_by_filter({"source": "docs/a.md"})

    assert n == 2  # ALL matching chunks, not just one
    assert sorted(fake.deletes[-1][1]) == ["c1", "c2"]
    assert set(fake.points) == {"c3"}


async def test_delete_by_filter_no_match_returns_zero(monkeypatch):
    store, fake, _ = make_store(monkeypatch)
    fake.points["c3"] = {"dense": [], "sparse": {}, "payload": {"source": "docs/b.md"}}

    assert await store.delete_by_filter({"source": "docs/missing.md"}) == 0
    assert fake.deletes == []


# ── failure modes ───────────────────────────────────────────────────────


async def test_missing_credentials_raise_clear_error(monkeypatch):
    store = QdrantStore(
        config=SimpleNamespace(),
        embedding_provider=FakeEmbedder(),
        settings=FakeInfraSettings(url=None, api_key=None),
    )
    # construction must NOT raise — only method calls do
    with pytest.raises(QdrantStoreError, match="QDRANT_URL"):
        await store.query("hello")
    with pytest.raises(QdrantStoreError, match="QDRANT_API_KEY"):
        await store.add(documents=["x"], ids=["1"])


async def test_connection_failure_raises_masked_error(monkeypatch):
    store, fake, _ = make_store(monkeypatch)
    fake.fail_connect = True

    with pytest.raises(QdrantStoreError) as excinfo:
        await store.query("hello")

    message = str(excinfo.value)
    assert "Qdrant" in message
    assert "secret" not in message  # API key never leaks into errors/logs
