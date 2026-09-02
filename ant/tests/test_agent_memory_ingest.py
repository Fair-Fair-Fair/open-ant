"""Phase 7 — 记忆图入库修复 + where 隔离过滤的单元测试（无网络）。

背景：验收发现 ``AgentSession._maybe_extract_memories`` 从不调用
``MemoryGraph.ingest`` —— 生产图是空的，detect_conflicts / mark_superseded /
expand 全部 no-op（"记忆仲裁" 在真实管线里从未生效）；且向量 create 路径
不传 ids，Qdrant 点 id 与图 memory_id 对不上。本文件覆盖修复后的契约：

  * create/update/fallback 三条路径都同步入图（entities/session_id/时间戳）；
  * 图失败只降级，绝不阻塞向量入库；
  * MemoryRetriever.retrieve / retrieve_semantic 的 where 过滤透传
    （qdrant 透传、chroma 忽略并告警）；
  * MemoryGuard.extract_memories 把 where 贯穿到语义去重查询。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from ant.core.agent import AgentSession
from ant.core.memory_guard import MemoryGuard
from ant.core.memory_retriever import MemoryRetriever
from ant.provider.memory.base import MemoryDocument

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeVectorStore:
    """Records add/update/get calls; returns scripted get results."""

    def __init__(self, old_docs=()):
        self.old_docs = list(old_docs)
        self.add_calls = []
        self.update_calls = []
        self.query_calls = []

    async def add(self, documents, metadatas=None, ids=None):
        self.add_calls.append(
            {"documents": list(documents), "metadatas": metadatas, "ids": ids}
        )

    async def update(self, id, document, metadata):
        self.update_calls.append(
            {"id": id, "document": document, "metadata": metadata}
        )

    async def get(self, ids):
        return self.old_docs

    async def query(self, query_text, **kwargs):
        self.query_calls.append((query_text, kwargs))
        return []


class FakeGraph:
    """Records ingest calls; can be scripted to raise (degradation test)."""

    def __init__(self, error=None):
        self.error = error
        self.ingest_calls = []

    async def ingest(self, memory):
        self.ingest_calls.append(memory)
        if self.error is not None:
            raise self.error


class FakeMemoryGuard:
    """MemoryGuard stand-in: returns scripted memories from extract_memories."""

    def __init__(self, memories=()):
        self.memories = list(memories)
        self.calls = []

    async def extract_memories(self, messages, where=None):
        self.calls.append((list(messages), where))
        return list(self.memories)


class FakeLLM:
    """LLMProvider stand-in for the guard-level test."""

    def __init__(self, tool_calls=()):
        self.tool_calls = list(tool_calls)
        self.calls = []

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append((messages, tools, kwargs))
        return ("", list(self.tool_calls), "stop")


class FakeMemoryConfig:
    def __init__(self, **overrides):
        defaults = dict(
            extraction_threshold=1,
            top_k=5,
            merge_top_k=3,
            merge_similarity=0.85,
            min_importance=5,
            doc_similarity_threshold=0.75,
            vector_backend="qdrant",
            reranker="none",
            query_rewrite_enabled=False,
        )
        defaults.update(overrides)
        for k, v in defaults.items():
            setattr(self, k, v)


def _make_session(memories=(), graph=None, store=None, guard=None):
    """Build a bare AgentSession with only the fields _maybe_extract_memories uses.

    AgentSession is a dataclass whose ``session_id`` / ``shared_context`` are
    properties delegating to ``state`` — so ``__new__`` + a state namespace
    carrying those fields is the lightweight construction path.
    """
    session = AgentSession.__new__(AgentSession)
    ctx = SimpleNamespace(
        config=SimpleNamespace(memory=FakeMemoryConfig()),
        memory_guard=guard if guard is not None else FakeMemoryGuard(memories),
        vector_store=store or FakeVectorStore(),
        graph=graph,
    )
    session.state = SimpleNamespace(
        session_id="s1",
        source=None,
        shared_context=ctx,
        messages=[{"role": "user", "content": "hi"}],
        _last_extracted_idx=0,
    )
    return session


def _mem(**overrides):
    mem = {
        "content": "user's favorite color is blue",
        "category": "user_pref",
        "importance": 7,
        "keywords": ["color"],
        "entities": [{"name": "favorite color", "type": "preference"}],
        "memory_id": "abc123",
    }
    mem.update(overrides)
    return mem


# ---------------------------------------------------------------------------
# 1. _maybe_extract_memories：三条路径都同步入图
# ---------------------------------------------------------------------------


async def test_create_path_uses_memory_id_and_ingests_graph():
    store = FakeVectorStore()
    graph = FakeGraph()
    session = _make_session(memories=[_mem()], store=store, graph=graph)

    await session._maybe_extract_memories()

    # 向量侧：显式 ids=[memory_id]（Qdrant 点 id 与图 memory_id 对齐）
    assert len(store.add_calls) == 1
    assert store.add_calls[0]["ids"] == ["abc123"]
    # 图侧：MERGE 节点 + 实体 + 边，携带 entities/session_id/时间戳
    assert len(graph.ingest_calls) == 1
    ingested = graph.ingest_calls[0]
    assert ingested["memory_id"] == "abc123"
    assert ingested["content"] == "user's favorite color is blue"
    assert ingested["entities"] == [{"name": "favorite color", "type": "preference"}]
    assert ingested["session_id"] == "s1"
    assert ingested["created_at"] and ingested["updated_at"]


async def test_update_path_ingests_graph_after_vector_update():
    old_doc = MemoryDocument(
        id="old1", content="old content",
        metadata={"created_at": "2026-01-01T00:00:00"}, score=0.0,
    )
    store = FakeVectorStore(old_docs=[old_doc])
    graph = FakeGraph()
    mem = _mem(_action="update", _target="old1")
    session = _make_session(memories=[mem], store=store, graph=graph)

    await session._maybe_extract_memories()

    assert len(store.update_calls) == 1
    assert store.update_calls[0]["id"] == "old1"
    assert len(graph.ingest_calls) == 1
    assert graph.ingest_calls[0]["memory_id"] == "old1"
    # created_at 保留旧值，updated_at 刷新
    assert graph.ingest_calls[0]["created_at"] == "2026-01-01T00:00:00"


async def test_fallback_create_when_update_target_missing():
    store = FakeVectorStore(old_docs=[])  # get 返回空 → fallback create
    graph = FakeGraph()
    mem = _mem(_action="update", _target="ghost")
    session = _make_session(memories=[mem], store=store, graph=graph)

    await session._maybe_extract_memories()

    assert len(store.add_calls) == 1
    assert store.add_calls[0]["ids"] == ["ghost"]  # 使用原 ID
    assert len(graph.ingest_calls) == 1
    assert graph.ingest_calls[0]["memory_id"] == "ghost"


async def test_graph_failure_degrades_vector_storage_still_happens():
    store = FakeVectorStore()
    graph = FakeGraph(error=RuntimeError("neo4j down"))
    session = _make_session(memories=[_mem()], store=store, graph=graph)

    # 不抛异常；向量入库照常完成
    await session._maybe_extract_memories()
    assert len(store.add_calls) == 1
    assert len(graph.ingest_calls) == 1


async def test_graph_none_skips_ingest_silently():
    store = FakeVectorStore()
    session = _make_session(memories=[_mem()], store=store, graph=None)

    await session._maybe_extract_memories()
    assert len(store.add_calls) == 1


# ---------------------------------------------------------------------------
# 2. MemoryRetriever：where 过滤透传
# ---------------------------------------------------------------------------


def _make_retriever(store=None, backend="qdrant"):
    return MemoryRetriever(
        SimpleNamespace(
            config=SimpleNamespace(
                memory=FakeMemoryConfig(vector_backend=backend),
                llm=SimpleNamespace(summarize_model=None),
            ),
            vector_store=store or FakeVectorStore(),
            graph=None,
        )
    )


async def test_retrieve_passes_where_to_qdrant_store():
    store = FakeVectorStore()
    retriever = _make_retriever(store=store)
    flt = {"eval_instance": 7}

    await retriever.retrieve("hello", where=flt)

    _, kwargs = store.query_calls[0]
    assert kwargs["where"] == flt
    assert kwargs["prefer_hybrid"] is True


async def test_retrieve_semantic_passes_where():
    store = FakeVectorStore()
    retriever = _make_retriever(store=store)
    flt = {"eval_instance": 7}

    await retriever.retrieve_semantic("hello", where=flt)

    _, kwargs = store.query_calls[0]
    assert kwargs["where"] == flt
    assert kwargs["prefer_hybrid"] is False  # 语义去重走纯 dense


async def test_retrieve_ignores_where_on_chroma_backend():
    store = FakeVectorStore()
    retriever = _make_retriever(store=store, backend="chroma")

    results = await retriever.retrieve("hello", where={"eval_instance": 7})

    assert results == []
    # chroma 路径不传 where（FakeVectorStore.query 只记录 kwargs）
    _, kwargs = store.query_calls[0]
    assert "where" not in kwargs


# ---------------------------------------------------------------------------
# 3. MemoryGuard：where 贯穿到语义去重
# ---------------------------------------------------------------------------


def test_extract_memories_threads_where_to_semantic_dedup():
    class CapturingRetriever:
        def __init__(self):
            self.calls = []

        async def retrieve_semantic(self, query, top_k=None, where=None):
            self.calls.append({"query": query, "top_k": top_k, "where": where})
            return []  # 无相似 → 直接保留候选

    guard = MemoryGuard.__new__(MemoryGuard)
    guard.llm = FakeLLM(
        tool_calls=[
            SimpleNamespace(
                name="extract_memories",
                arguments=json.dumps(
                    {
                        "content": "user likes coffee",
                        "category": "user_pref",
                        "importance": 6,
                        "keywords": ["coffee"],
                        "entities": [{"name": "coffee", "type": "preference"}],
                    }
                ),
            )
        ]
    )
    retriever = CapturingRetriever()
    guard.context = SimpleNamespace(
        config=SimpleNamespace(memory=FakeMemoryConfig()),
        memory_retriever=retriever,
        graph=None,
    )

    import asyncio

    memories = asyncio.run(
        guard.extract_memories(
            [{"role": "user", "content": "I like coffee"}],
            where={"eval_instance": 3},
        )
    )

    assert len(memories) == 1
    assert memories[0]["content"] == "user likes coffee"
    assert retriever.calls[0]["where"] == {"eval_instance": 3}
