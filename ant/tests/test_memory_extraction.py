"""Unit tests for ``ant.memory.extraction`` (no real LLM).

A fake LLMProvider returns scripted tool calls; tests assert on arguments
parsing, per-item fault isolation (设计原则 3: 单个坏数据不连坐整批),
schema completeness and the low-temperature constraint.
"""

import json

import pytest

from ant.memory.extraction import (
    EXTRACT_TOOL_NAME,
    EXTRACT_TOOLS,
    extract_memories,
    normalize_entities,
)
from ant.provider.llm import LLMToolCall


class FakeLLM:
    """Minimal LLMProvider stand-in returning scripted (content, tools, reason)."""

    def __init__(self, content="", tool_calls=(), stop_reason="stop", error=None):
        self.content = content
        self.tool_calls = list(tool_calls)
        self.stop_reason = stop_reason
        self.error = error
        self.last_messages = None
        self.last_tools = None
        self.last_kwargs = None

    async def chat(self, messages, tools=None, **kwargs):
        self.last_messages = messages
        self.last_tools = tools
        self.last_kwargs = kwargs
        if self.error is not None:
            raise self.error
        return (self.content, self.tool_calls, self.stop_reason)


class FakeMemoryConfig:
    def __init__(self, min_importance=5):
        self.min_importance = min_importance


class FakeConfig:
    def __init__(self, min_importance=5):
        self.memory = FakeMemoryConfig(min_importance)


def tool_call(arguments: str, name: str = EXTRACT_TOOL_NAME) -> LLMToolCall:
    return LLMToolCall(id="call-1", name=name, arguments=arguments)


def good_memory(**overrides) -> dict:
    memory = {
        "content": "Alice prefers dark mode",
        "category": "user_pref",
        "importance": 8,
        "keywords": ["alice", "theme"],
        "entities": [{"name": "Alice", "type": "person"}],
    }
    memory.update(overrides)
    return memory


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

async def test_extract_parses_single_tool_call():
    llm = FakeLLM(tool_calls=[tool_call(json.dumps(good_memory()))])
    memories = await extract_memories(
        llm, [{"role": "user", "content": "hi"}], FakeConfig()
    )
    assert memories == [good_memory()]
    assert llm.last_kwargs.get("temperature") == 0.2


async def test_extract_multiple_tool_calls():
    llm = FakeLLM(
        tool_calls=[
            tool_call(json.dumps(good_memory())),
            tool_call(
                json.dumps(
                    good_memory(
                        content="User works at Acme",
                        category="personal",
                        importance=6,
                        keywords=["acme"],
                        entities=[{"name": "Acme", "type": "company"}],
                    )
                )
            ),
        ]
    )
    memories = await extract_memories(llm, [], FakeConfig())
    assert len(memories) == 2
    assert memories[1]["category"] == "personal"
    assert memories[1]["entities"] == [{"name": "Acme", "type": "company"}]


async def test_extract_arguments_may_be_a_json_array():
    # Some providers emit one tool call whose arguments are a JSON array
    # despite the schema; lenient parsing must still land every memory.
    llm = FakeLLM(
        tool_calls=[
            tool_call(
                json.dumps(
                    [good_memory(), good_memory(content="Second fact", importance=5)]
                )
            )
        ]
    )
    memories = await extract_memories(llm, [], FakeConfig())
    assert len(memories) == 2


async def test_extract_skips_single_bad_item_keeps_rest():
    llm = FakeLLM(
        tool_calls=[
            tool_call("not-json-{{{"),
            tool_call(json.dumps(good_memory())),
        ]
    )
    memories = await extract_memories(llm, [], FakeConfig())
    assert memories == [good_memory()]


async def test_extract_unparseable_arguments_return_empty():
    llm = FakeLLM(tool_calls=[tool_call("not json at all")])
    assert await extract_memories(llm, [], FakeConfig()) == []


async def test_extract_no_tool_calls_returns_empty():
    # Content-only answer is NOT parsed: constrained output is the contract.
    llm = FakeLLM(content='[{"content": "ignored", "importance": 8}]')
    assert await extract_memories(llm, [], FakeConfig()) == []


async def test_extract_ignores_foreign_tool_calls():
    llm = FakeLLM(
        tool_calls=[
            tool_call(json.dumps(good_memory()), name="some_other_tool"),
            tool_call(json.dumps(good_memory(content="Real fact"))),
        ]
    )
    memories = await extract_memories(llm, [], FakeConfig())
    assert len(memories) == 1
    assert memories[0]["content"] == "Real fact"


# ---------------------------------------------------------------------------
# Validation / clamping / filtering
# ---------------------------------------------------------------------------

async def test_extract_importance_below_minimum_filtered():
    llm = FakeLLM(tool_calls=[tool_call(json.dumps(good_memory(importance=3)))])
    assert await extract_memories(llm, [], FakeConfig(min_importance=5)) == []


async def test_extract_importance_cleaned_and_clamped():
    llm = FakeLLM(
        tool_calls=[
            tool_call(json.dumps(good_memory(importance="high"))),
            tool_call(json.dumps(good_memory(importance=5.5, content="f2"))),
            tool_call(json.dumps(good_memory(importance=99, content="f3"))),
        ]
    )
    memories = await extract_memories(llm, [], FakeConfig(min_importance=1))
    assert [m["importance"] for m in memories] == [5, 5, 10]


async def test_extract_entities_normalized_and_keywords_coerced():
    llm = FakeLLM(
        tool_calls=[
            tool_call(
                json.dumps(
                    {
                        "content": "fact",
                        "importance": 7,
                        "entities": ["Alice", {"name": "py", "type": "language"}],
                        "keywords": "not-a-list",
                    }
                )
            )
        ]
    )
    memories = await extract_memories(llm, [], FakeConfig(min_importance=1))
    assert memories[0]["entities"] == [
        {"name": "Alice", "type": "fact"},
        {"name": "py", "type": "language"},
    ]
    assert memories[0]["keywords"] == []
    assert memories[0]["category"] == "fact"


async def test_extract_empty_content_dropped():
    llm = FakeLLM(
        tool_calls=[
            tool_call(json.dumps(good_memory(content="   "))),
            tool_call(json.dumps(good_memory(content="Real fact"))),
        ]
    )
    memories = await extract_memories(llm, [], FakeConfig())
    assert len(memories) == 1
    assert memories[0]["content"] == "Real fact"


# ---------------------------------------------------------------------------
# Failure contract
# ---------------------------------------------------------------------------

async def test_extract_llm_failure_propagates():
    llm = FakeLLM(error=RuntimeError("provider down"))
    with pytest.raises(RuntimeError, match="provider down"):
        await extract_memories(llm, [], FakeConfig())


# ---------------------------------------------------------------------------
# Schema completeness
# ---------------------------------------------------------------------------

async def test_extract_tool_schema_is_strict_and_complete():
    llm = FakeLLM()
    await extract_memories(llm, [{"role": "user", "content": "hi"}], FakeConfig())

    assert len(EXTRACT_TOOLS) == 1
    schema = EXTRACT_TOOLS[0]
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == EXTRACT_TOOL_NAME

    params = fn["parameters"]
    assert params["type"] == "object"
    assert params["additionalProperties"] is False
    required = {"content", "category", "importance", "keywords", "entities"}
    assert set(params["required"]) == required
    assert set(params["properties"]) == required

    importance = params["properties"]["importance"]
    assert importance["type"] == "integer"
    assert importance["minimum"] == 1
    assert importance["maximum"] == 10

    entity_items = params["properties"]["entities"]["items"]
    assert entity_items["type"] == "object"
    assert set(entity_items["required"]) == {"name", "type"}
    assert entity_items["additionalProperties"] is False
    assert entity_items["properties"]["name"]["type"] == "string"

    # The chat call passes the tools list and a low temperature
    assert llm.last_tools == EXTRACT_TOOLS
    assert llm.last_kwargs.get("temperature") == 0.2


async def test_extract_prompt_serializes_user_messages():
    llm = FakeLLM()
    await extract_memories(
        llm,
        [
            {"role": "system", "content": "system stuff"},
            {"role": "user", "content": "remember this"},
            {"role": "tool", "content": "tool result"},
        ],
        FakeConfig(),
    )
    prompt = llm.last_messages[0]["content"]
    assert "extract_memories" in prompt  # tool-call instruction present
    assert "USER: remember this" in prompt
    assert "system stuff" not in prompt
    # 按序列化标记断言（裸词 "tool result" 会与指令文本里的措辞撞车）
    assert "TOOL: tool result" not in prompt


def test_normalize_entities_handles_dicts_strings_and_garbage():
    assert normalize_entities(
        [
            {"name": "Alice", "type": "person"},
            "Python",
            {"type": "x"},              # no name -> dropped
            {"name": "  ", "type": "y"},  # blank name -> dropped
            42,                          # neither dict nor str -> dropped
            "  ",
        ]
    ) == [
        {"name": "Alice", "type": "person"},
        {"name": "Python", "type": "fact"},
    ]
    assert normalize_entities("not-a-list") == []
