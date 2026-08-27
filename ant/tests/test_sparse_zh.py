"""Phase 5E sparse-zh tests — no real network (fakes only).

Covers the configurable sparse generator in QdrantStore:
- ``_jieba_sparse_vectors`` — jieba word segmentation → term-frequency
  sparse vectors (structure, determinism, index bounds);
- ``sparse_model="jieba"`` wiring — the fastembed model must never be
  loaded when the jieba generator is selected.

jieba is a pyproject dependency (``jieba>=0.42``); when it is missing from
the running environment the jieba-specific tests skip instead of failing.
"""

from types import SimpleNamespace

import pytest

from ant.provider.memory.qdrant_store import QdrantStore

try:
    import jieba  # noqa: F401 — presence probe only

    _HAS_JIEBA = True
except ImportError:  # pragma: no cover — env-dependent
    _HAS_JIEBA = False

jieba_required = pytest.mark.skipif(
    not _HAS_JIEBA,
    reason="jieba 未安装（pip install -e src 或 pip install jieba 后本组测试生效）",
)


class _FakeDenseEmbedder:
    """Embedding provider returning deterministic dense vectors."""

    async def aembed(self, texts):
        return [[0.1] * 16 for _ in texts]


class _FakeSettings:
    """Duck-typed InfraSettings (never touches .env)."""

    qdrant_collection = "test_col"
    qdrant_vector_size = 16
    qdrant_distance = "Cosine"
    qdrant_timeout = 5

    def qdrant_url(self):
        return "https://qdrant.example"

    def qdrant_api_key(self):
        return "secret"

    def masked_qdrant_url(self):
        return "https://qdrant.example"


class _MinimalFakeClient:
    """Enough of AsyncQdrantClient for a single query() in jieba mode."""

    def __init__(self):
        self.created = []
        self.query_calls = []
        self._exists = False

    async def get_collection(self, name):
        if not self._exists:
            raise RuntimeError("Collection not found (fake 404)")
        return {"name": name}

    async def create_collection(self, **kwargs):
        self.created.append(kwargs)
        self._exists = True

    async def create_payload_index(self, **kwargs):
        return None

    async def query_points(self, **kwargs):
        self.query_calls.append(kwargs)
        return SimpleNamespace(points=[])


# ── jieba sparse generator ────────────────────────────────────────────────


@jieba_required
def test_jieba_sparse_structure_and_term_frequency():
    """分词 → {index: term_frequency}：重复 term 合并为单条，value=频率。"""
    result = QdrantStore._jieba_sparse_vectors(["苹果 苹果 香蕉"])[0]

    assert set(result) == {"indices", "values"}
    assert len(result["indices"]) == len(result["values"])
    assert all(isinstance(i, int) for i in result["indices"])
    assert all(isinstance(v, float) for v in result["values"])
    assert result["indices"] == sorted(result["indices"])

    counts = dict(zip(result["indices"], result["values"]))
    assert 2.0 in counts.values()  # "苹果" 出现两次 → 频率 2
    assert 1.0 in counts.values()  # "香蕉" 出现一次 → 频率 1


@jieba_required
def test_jieba_blank_text_yields_empty_sparse():
    """空白/空文本没有 term → 空 indices/values。"""
    result = QdrantStore._jieba_sparse_vectors(["", "   "])
    assert result == [
        {"indices": [], "values": []},
        {"indices": [], "values": []},
    ]


@jieba_required
def test_jieba_hash_stable_across_calls():
    """同一文本两次调用结果完全一致（确定性 sha256，跨进程稳定）。"""
    text = "用户喜欢用 Rust 写系统程序，最近在学计算机图形学。"
    assert QdrantStore._jieba_sparse_vectors([text]) == QdrantStore._jieba_sparse_vectors(
        [text]
    )
    assert QdrantStore._jieba_term_index("苹果") == QdrantStore._jieba_term_index("苹果")
    assert QdrantStore._jieba_term_index("苹果") != QdrantStore._jieba_term_index("香蕉")


@jieba_required
def test_jieba_index_range_within_fixed_space():
    """所有索引落在固定 1M 索引空间内（hash % 1_000_000）。"""
    result = QdrantStore._jieba_sparse_vectors(
        ["中文分词索引范围测试 0123456789 混合词 foo-bar 2026"]
    )[0]
    assert result["indices"]
    assert all(0 <= i < 1_000_000 for i in result["indices"])


@jieba_required
def test_jieba_multiple_texts_parallel_output():
    """批量输入：输出条数一致，每条都是 indices/values 结构。"""
    texts = ["第一段话，讲咖啡。", "第二段话，讲马拉松。"]
    out = QdrantStore._jieba_sparse_vectors(texts)
    assert len(out) == len(texts)
    for item in out:
        assert set(item) == {"indices", "values"}


# ── QdrantStore wiring: sparse_model="jieba" skips fastembed ──────────────


def test_memory_config_sparse_model_field():
    """MemoryConfig.sparse_model 默认 fastembed，只接受 fastembed|jieba。"""
    from ant.utils.config import MemoryConfig

    assert MemoryConfig().sparse_model == "fastembed"
    assert MemoryConfig(sparse_model="jieba").sparse_model == "jieba"
    with pytest.raises(ValueError):
        MemoryConfig(sparse_model="bm25")


def test_store_sparse_model_default_is_fastembed():
    """无 memory 配置（旧调用方）回退 fastembed——向后兼容。"""
    store = QdrantStore(
        config=SimpleNamespace(), embedding_provider=_FakeDenseEmbedder()
    )
    assert store._sparse_model() == "fastembed"


@jieba_required
async def test_store_jieba_mode_never_loads_fastembed(monkeypatch):
    """sparse_model=jieba 时 fastembed.SparseTextEmbedding 必须零调用。"""
    import fastembed
    import qdrant_client

    fake = _MinimalFakeClient()
    monkeypatch.setattr(qdrant_client, "AsyncQdrantClient", lambda **kw: fake)

    loaded: list[str] = []

    def boom(*args, **kwargs):
        loaded.append("fastembed loaded")
        raise AssertionError("fastembed must not load when sparse_model=jieba")

    monkeypatch.setattr(fastembed, "SparseTextEmbedding", boom)

    store = QdrantStore(
        config=SimpleNamespace(memory=SimpleNamespace(sparse_model="jieba")),
        embedding_provider=_FakeDenseEmbedder(),
        settings=_FakeSettings(),
    )
    assert store._sparse_model() == "jieba"

    await store.query("中文混合检索查询", top_k=3)

    assert loaded == []
    # sparse 侧走了 jieba：prefetch 携带的是 jieba 分词后的索引而非 fastembed 输出
    prefetches = fake.query_calls[-1]["prefetch"]
    sparse_prefetch = [p for p in prefetches if p.using == "sparse"]
    assert len(sparse_prefetch) == 1
    sparse_query = sparse_prefetch[0].query
    assert sparse_query.indices and sparse_query.values
    assert all(0 <= i < 1_000_000 for i in sparse_query.indices)
