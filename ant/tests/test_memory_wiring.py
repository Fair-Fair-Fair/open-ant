"""Phase-3C wiring tests — ``SharedContext._init_memory`` backend selection.

No network and no model downloads: QdrantStore / MemoryGraph /
InfraSettings / EmbeddingProvider / MemoryGuard are monkeypatched.  The
context is built via ``SharedContext.__new__`` + ``_init_memory`` (the
same pattern as the other Phase-3 test suites) so only the wiring under
test runs.
"""

import logging

from ant.core.context import SharedContext
from ant.provider.memory.hybrid_store import HybridMemoryStore
from ant.utils.config import Config

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeEmbeddingProvider:
    @classmethod
    def from_config(cls, config):
        return cls()


class FakeQdrantStore:
    def __init__(self, config, embedding_provider):
        self.config = config
        self.embedding_provider = embedding_provider


class FakeVectorStore:
    """Stand-in for a ChromaVectorStore (embedding_provider attribute kept)."""

    embedding_provider = None


class FakeChromaVectorStore:
    @staticmethod
    def from_config(config, embedding_provider):
        return FakeVectorStore()


class FakeGraph:
    def __init__(self, uri, username, password, database=None, **kwargs):
        self.uri = uri
        self.username = username
        self.password = password
        self.database = database
        self.kwargs = kwargs


class FakeGuard:
    def __init__(self, context):
        self.context = context


class FakeInfraSettings:
    """Duck-typed InfraSettings — never touches .env / real credentials."""

    def __init__(self, uri=None, username=None, password=None, database=None,
                 pool_size=None):
        self._uri = uri
        self._username = username
        self._password = password
        self._database = database
        self.neo4j_max_connection_pool_size = pool_size

    def neo4j_uri(self):
        return self._uri

    def neo4j_username(self):
        return self._username

    def neo4j_password(self):
        return self._password

    def neo4j_database(self):
        return self._database

    def masked_neo4j_uri(self):
        return self._uri or "(none)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(tmp_path, memory_overrides=None):
    """Minimal valid Config; memory attrs overridden afterwards."""
    config = Config(
        workspace=tmp_path,
        llm={"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-test"},
        default_agent="main",
    )
    for key, value in (memory_overrides or {}).items():
        setattr(config.memory, key, value)
    return config


def patch_context_deps(monkeypatch, qdrant_store=None, graph=None):
    """Patch the context-module names + the lazily imported store/graph."""
    monkeypatch.setattr("ant.core.context.EmbeddingProvider", FakeEmbeddingProvider)
    monkeypatch.setattr("ant.core.context.MemoryGuard", FakeGuard)
    if qdrant_store is not None:
        monkeypatch.setattr("ant.provider.memory.qdrant_store.QdrantStore", qdrant_store)
    if graph is not None:
        monkeypatch.setattr("ant.memory.graph.MemoryGraph", graph)


def build_context(config, infra):
    ctx = SharedContext.__new__(SharedContext)
    ctx._infra_settings = infra
    ctx._init_memory(config)
    return ctx


# ---------------------------------------------------------------------------
# Qdrant backend
# ---------------------------------------------------------------------------


def test_qdrant_backend_wires_qdrant_store(tmp_path, monkeypatch):
    created = []

    def fake_qdrant_store(config, embedding_provider):
        created.append(config)
        return FakeQdrantStore(config, embedding_provider)

    graph_created = []

    def fake_graph(uri, username, password, database=None, **kwargs):
        graph_created.append((uri, username, password, database, kwargs))
        return FakeGraph(uri, username, password, database, **kwargs)

    patch_context_deps(monkeypatch, qdrant_store=fake_qdrant_store, graph=fake_graph)

    infra = FakeInfraSettings(
        uri="bolt://fake:7687", username="u", password="p",
        database="neo4j", pool_size=42,
    )
    config = make_config(tmp_path, memory_overrides={
        "enabled": True, "vector_backend": "qdrant", "graph_enabled": True,
    })

    ctx = build_context(config, infra)

    assert isinstance(ctx.vector_store, FakeQdrantStore)
    assert created == [config]
    assert ctx.embedding_provider is not None
    assert ctx.memory_retriever is not None
    assert ctx.memory_guard is not None
    assert ctx.doc_ingester is not None
    assert isinstance(ctx.graph, FakeGraph)
    assert ctx.graph.database == "neo4j"
    # pool-size knob read from settings via getattr and forwarded
    assert ctx.graph.kwargs == {"max_connection_pool_size": 42}


# ---------------------------------------------------------------------------
# Graph wiring
# ---------------------------------------------------------------------------


def test_neo4j_credentials_missing_disables_graph(tmp_path, monkeypatch, caplog):
    graph_created = []
    monkeypatch.setattr(
        "ant.memory.graph.MemoryGraph",
        lambda *a, **k: graph_created.append(True) or FakeGraph(*a, **k),
    )
    patch_context_deps(
        monkeypatch,
        qdrant_store=lambda c, e: FakeQdrantStore(c, e),
    )

    infra = FakeInfraSettings(uri=None, username=None, password=None)
    config = make_config(tmp_path, memory_overrides={
        "enabled": True, "graph_enabled": True,
    })

    with caplog.at_level(logging.WARNING):
        ctx = build_context(config, infra)

    assert ctx.graph is None
    assert graph_created == []
    assert any("Neo4j credentials" in record.message for record in caplog.records)


def test_graph_disabled_never_constructs(tmp_path, monkeypatch):
    graph_created = []
    monkeypatch.setattr(
        "ant.memory.graph.MemoryGraph",
        lambda *a, **k: graph_created.append(True) or FakeGraph(*a, **k),
    )
    patch_context_deps(
        monkeypatch,
        qdrant_store=lambda c, e: FakeQdrantStore(c, e),
    )

    infra = FakeInfraSettings(uri="bolt://fake:7687", username="u", password="p")
    config = make_config(tmp_path, memory_overrides={
        "enabled": True, "graph_enabled": False,
    })

    ctx = build_context(config, infra)

    assert ctx.graph is None
    assert graph_created == []


def test_graph_construction_failure_disables_graph(tmp_path, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("bad credentials")

    monkeypatch.setattr("ant.memory.graph.MemoryGraph", boom)
    patch_context_deps(
        monkeypatch,
        qdrant_store=lambda c, e: FakeQdrantStore(c, e),
    )

    secret = "super-secret-neo4j-password-3c"
    infra = FakeInfraSettings(uri="bolt://fake:7687", username="u", password=secret)
    config = make_config(tmp_path, memory_overrides={
        "enabled": True, "graph_enabled": True,
    })

    with caplog.at_level(logging.WARNING):
        ctx = build_context(config, infra)

    assert ctx.graph is None
    assert any(
        "MemoryGraph construction failed" in record.message
        for record in caplog.records
    )
    # masked URI only — the password never leaks into the warning
    assert any("bolt://fake:7687" in record.message for record in caplog.records)
    assert not any(secret in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Chroma fallback
# ---------------------------------------------------------------------------


def test_chroma_backend_keeps_hybrid_wrap(tmp_path, monkeypatch):
    patch_context_deps(
        monkeypatch,
        qdrant_store=lambda c, e: FakeQdrantStore(c, e),
    )
    monkeypatch.setattr("ant.core.context.VectorStore", FakeChromaVectorStore)

    infra = FakeInfraSettings(uri=None, username=None, password=None)
    config = make_config(tmp_path, memory_overrides={
        "enabled": True, "vector_backend": "chroma", "hybrid_enabled": True,
    })

    ctx = build_context(config, infra)

    assert isinstance(ctx.vector_store, HybridMemoryStore)
    assert isinstance(ctx.vector_store._store, FakeVectorStore)
    assert ctx.graph is None  # no credentials — graph disabled with warning


def test_chroma_backend_plain_store_when_hybrid_disabled(tmp_path, monkeypatch):
    patch_context_deps(monkeypatch, qdrant_store=lambda c, e: FakeQdrantStore(c, e))
    monkeypatch.setattr("ant.core.context.VectorStore", FakeChromaVectorStore)

    infra = FakeInfraSettings(uri=None, username=None, password=None)
    config = make_config(tmp_path, memory_overrides={
        "enabled": True, "vector_backend": "chroma", "hybrid_enabled": False,
        "graph_enabled": False,
    })

    ctx = build_context(config, infra)

    assert isinstance(ctx.vector_store, FakeVectorStore)
    assert ctx.graph is None


# ---------------------------------------------------------------------------
# Disabled memory
# ---------------------------------------------------------------------------


def test_memory_disabled_all_none(tmp_path, monkeypatch):
    patch_context_deps(monkeypatch, qdrant_store=lambda c, e: FakeQdrantStore(c, e))
    config = make_config(tmp_path, memory_overrides={"enabled": False})

    ctx = build_context(config, FakeInfraSettings())

    assert ctx.memory_guard is None
    assert ctx.memory_retriever is None
    assert ctx.embedding_provider is None
    assert ctx.vector_store is None
    assert ctx.doc_ingester is None
    assert ctx.graph is None
