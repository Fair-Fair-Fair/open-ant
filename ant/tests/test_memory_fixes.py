"""Regression tests for memory fixes.

Covers improve.md #3 (doc_ingester.delete_by_source deletes ALL chunks of a
source, not just 1) and #4 (memory_guard._parse_response survives dirty LLM
output: safe int conversion + bad item skipped without killing the batch).
"""

import asyncio

import pytest

from ant.core.memory_guard import MemoryGuard
from ant.provider.memory.doc_ingester import DocumentIngester

# ---------------------------------------------------------------------------
# 假对象（避免实例化真实 Chroma / LLM / SharedContext）
# ---------------------------------------------------------------------------

class FakeMemoryConfig:
    def __init__(self, min_importance: int = 5):
        self.min_importance = min_importance


class FakeConfig:
    def __init__(self, min_importance: int = 5):
        self.memory = FakeMemoryConfig(min_importance)


class FakeContext:
    def __init__(self, min_importance: int = 5):
        self.config = FakeConfig(min_importance)


def make_guard(min_importance: int = 5) -> MemoryGuard:
    """构造 MemoryGuard 但不走 __init__（避免拉起 LLMProvider）。"""
    guard = MemoryGuard.__new__(MemoryGuard)
    guard.context = FakeContext(min_importance)
    return guard


class FakeCollection:
    """模仿 Chroma collection：记录 get(where=...) 调用并返回预设 ids。"""

    def __init__(self, ids=None):
        self.ids = list(ids or [])
        self.get_kwargs = None

    def get(self, **kwargs):
        self.get_kwargs = kwargs
        return {
            "ids": self.ids,
            "metadatas": [{}] * len(self.ids),
            "documents": None,
        }


class FakeVectorStore:
    """只实现 delete_by_source 需要的最小接口 + 伪 _collection 属性。"""

    def __init__(self, collection: FakeCollection | None = None):
        self._collection = collection
        self.deleted_ids = None

    async def delete(self, ids: list[str]) -> None:
        self.deleted_ids = list(ids)


# ---------------------------------------------------------------------------
# memory_guard #4：_safe_importance 安全转换
# ---------------------------------------------------------------------------

def test_safe_importance_accepts_int_string():
    assert MemoryGuard._safe_importance("8") == 8


def test_safe_importance_word_like_high_falls_back_to_default():
    # 修复点：旧实现 int("high") 直接 ValueError 炸掉整批提取
    assert MemoryGuard._safe_importance("high") == 5


def test_safe_importance_float_truncates():
    # int(5.5) 本身不抛异常，截断为 5
    assert MemoryGuard._safe_importance(5.5) == 5


def test_safe_importance_none_falls_back():
    assert MemoryGuard._safe_importance(None) == 5


def test_safe_importance_custom_default():
    assert MemoryGuard._safe_importance("high", default=3) == 3


# ---------------------------------------------------------------------------
# memory_guard #4：_parse_response 坏条目跳过、不炸整批
# ---------------------------------------------------------------------------

def test_parse_response_skips_bad_items_keeps_good_ones():
    response = """[
        {"content": "user prefers dark mode", "importance": 8, "keywords": ["theme"]},
        {"content": "user likes python", "importance": "high", "keywords": ["lang"]},
        "not-a-dict",
        {"importance": 9},
        {"content": "   "}
    ]"""
    guard = make_guard()
    valid = guard._parse_response(response)
    # importance="high" 走安全转换取默认 5 保留；非 dict、缺 content、空 content
    # 的坏条目被丢弃；整批解析不抛异常。
    assert len(valid) == 2
    assert valid[0]["content"] == "user prefers dark mode"
    assert valid[0]["importance"] == 8
    assert valid[1]["content"] == "user likes python"
    assert valid[1]["importance"] == 5


def test_parse_response_dirty_importance_does_not_raise():
    # 整批都是 "high" 等脏值：应全部保留为默认 importance=5，而不是 raise
    response = """[
        {"content": "fact A", "importance": "high"},
        {"content": "fact B", "importance": 5.5},
        {"content": "fact C", "importance": 3}
    ]"""
    guard = make_guard(min_importance=1)
    valid = guard._parse_response(response)
    assert len(valid) == 3
    assert [v["importance"] for v in valid] == [5, 5, 3]


def test_parse_response_non_str_content_skipped():
    # content 不是字符串时 .strip() 抛 AttributeError，应丢弃该条而非 raise
    guard = make_guard(min_importance=1)
    valid = guard._parse_response(
        '[{"content": 123, "importance": 7}, {"content": "good fact", "importance": 7}]'
    )
    assert len(valid) == 1
    assert valid[0]["content"] == "good fact"


def test_parse_response_importance_below_min_filtered():
    response = '[{"content": "low importance fact", "importance": 4}]'
    guard = make_guard(min_importance=5)
    assert guard._parse_response(response) == []


def test_parse_response_fenced_json():
    response = "```json\n[{\"content\": \"fenced fact\", \"importance\": 6}]\n```"
    guard = make_guard(min_importance=5)
    valid = guard._parse_response(response)
    assert len(valid) == 1
    assert valid[0]["content"] == "fenced fact"


def test_parse_response_invalid_json_returns_empty():
    guard = make_guard()
    assert guard._parse_response("not json at all") == []


def test_parse_response_not_a_list_returns_empty():
    guard = make_guard()
    assert guard._parse_response('{"content": "single object"}') == []


# ---------------------------------------------------------------------------
# doc_ingester #3：delete_by_source 按 where 过滤取全部 ids 后整体删除
# ---------------------------------------------------------------------------

def test_delete_by_source_deletes_all_matching_chunks():
    collection = FakeCollection(ids=["chunk-a", "chunk-b", "chunk-c"])
    store = FakeVectorStore(collection)
    ingester = DocumentIngester.__new__(DocumentIngester)
    ingester.vector_store = store

    count = asyncio.run(ingester.delete_by_source("docs/guide.md"))

    # get 必须用 where 按 source 过滤
    assert collection.get_kwargs == {"where": {"source": "docs/guide.md"}}
    # 全部 chunk id 一次性交给 delete，而不是只删 1 个
    assert store.deleted_ids == ["chunk-a", "chunk-b", "chunk-c"]
    assert count == 3


def test_delete_by_source_no_match_no_delete():
    collection = FakeCollection(ids=[])
    store = FakeVectorStore(collection)
    ingester = DocumentIngester.__new__(DocumentIngester)
    ingester.vector_store = store

    count = asyncio.run(ingester.delete_by_source("docs/missing.md"))

    assert collection.get_kwargs == {"where": {"source": "docs/missing.md"}}
    assert store.deleted_ids is None
    assert count == 0


def test_delete_by_source_without_collection_returns_zero():
    # 非 Chroma 后端（无 _collection）时安全返回 0，不做旧版"只删 1 条"的
    # 部分删除，避免静默残留。
    store = FakeVectorStore(collection=None)
    ingester = DocumentIngester.__new__(DocumentIngester)
    ingester.vector_store = store

    count = asyncio.run(ingester.delete_by_source("docs/guide.md"))

    assert count == 0
    assert store.deleted_ids is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
