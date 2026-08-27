"""Phase 5A observability-wiring tests: call sites of the Phase 4B helpers.

Covered:
  * registry.execute_tool → observability.observe_tool (success + every
    exception path, via finally);
  * provider.llm.chat completion point → observability.observe_llm;
  * agent_worker.dispatch_event entry → observability.record_event_consumed
    — and principle 11: a broken observability helper never breaks the chain;
  * config 收口: judge_enabled / query_rewrite_enabled are real pydantic
    fields with YAML deserialization;
  * memory_retriever._rewrite_query reads the real field (hasattr defense).

No real network: helpers are monkeypatched with recorders; the LLM router
is a fake ``acompletion``.
"""

import sys
import types
from pathlib import Path

# pytest pythonpath 配置在 pyproject；此兜底与既有测试一致
_SRC = Path(__file__).resolve().parents[2]  # src/ant/tests -> src
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ant.core.events import CliEventSource, InboundEvent  # noqa: E402
from ant.core.memory_retriever import MemoryRetriever  # noqa: E402
from ant.core.sandbox import SandboxViolation  # noqa: E402
from ant.provider.llm.base import LLMProvider  # noqa: E402
from ant.server import observability  # noqa: E402
from ant.server.agent_worker import AgentWorker  # noqa: E402
from ant.tools.registry import ToolRegistry  # noqa: E402
from ant.utils.config import Config, InputGuardrailConfig, MemoryConfig  # noqa: E402
from ant.utils.def_loader import DefNotFoundError  # noqa: E402


class _FakeTool:
    name = "fake_echo"
    description = "test tool"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, session, **kwargs):
        return f"echo:{kwargs.get('text')}"

    def get_tool_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class _FakeCrashTool(_FakeTool):
    name = "fake_crash"

    async def execute(self, session, **kwargs):
        raise RuntimeError("boom")


class _FakeViolationTool(_FakeTool):
    name = "fake_violation"

    async def execute(self, session, **kwargs):
        raise SandboxViolation("blocked", violation_type="path")


# ── 1. registry.execute_tool → observe_tool ───────────────────────────────


async def test_execute_tool_success_observes(monkeypatch):
    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(
        observability, "observe_tool", lambda name, secs: calls.append((name, secs))
    )
    registry = ToolRegistry()
    registry.register(_FakeTool())

    result = await registry.execute_tool("fake_echo", session=object(), text="hi")

    assert result == "echo:hi"
    assert len(calls) == 1
    name, secs = calls[0]
    assert name == "fake_echo"
    assert isinstance(secs, float) and secs >= 0.0


async def test_execute_tool_exception_observes(monkeypatch):
    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(
        observability, "observe_tool", lambda name, secs: calls.append((name, secs))
    )
    registry = ToolRegistry()
    registry.register(_FakeCrashTool())

    result = await registry.execute_tool("fake_crash", session=object())

    assert result.startswith("Tool execution error:")
    assert len(calls) == 1
    assert calls[0][0] == "fake_crash"


async def test_execute_tool_sandbox_violation_observes(monkeypatch):
    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(
        observability, "observe_tool", lambda name, secs: calls.append((name, secs))
    )
    registry = ToolRegistry()
    registry.register(_FakeViolationTool())

    result = await registry.execute_tool("fake_violation", session=object())

    assert result.startswith("Safety violation")
    assert len(calls) == 1
    assert calls[0][0] == "fake_violation"


async def test_execute_tool_survives_broken_observe_tool(monkeypatch):
    """原则 11：观测 helper 抛异常也不打断工具主链路。"""

    def boom(name, secs):
        raise RuntimeError("metrics broke")

    monkeypatch.setattr(observability, "observe_tool", boom)
    registry = ToolRegistry()
    registry.register(_FakeTool())

    result = await registry.execute_tool("fake_echo", session=object(), text="hi")

    assert result == "echo:hi"


# ── 2. provider/llm/base.py chat → observe_llm ────────────────────────────


def _make_provider() -> LLMProvider:
    """最小 LLMProvider 实例（绕过 litellm Router 构造，测试内替换 _router）。"""
    provider = object.__new__(LLMProvider)
    provider.model = "fake-model"
    provider.api_key = "k"
    provider.api_base = None
    provider.temperature = 0.7
    provider.max_tokens = 64
    provider.usage_callback = None
    provider._settings = {}
    return provider


class _FakeResponse:
    usage = types.SimpleNamespace(prompt_tokens=12, completion_tokens=7)
    _hidden_params = {}
    choices = [
        types.SimpleNamespace(
            message=types.SimpleNamespace(content="hello", tool_calls=[]),
            finish_reason="stop",
        )
    ]


async def test_chat_observes_llm(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        observability,
        "observe_llm",
        lambda model, secs, pt, ct: calls.append((model, secs, pt, ct)),
    )
    provider = _make_provider()

    async def fake_acompletion(**kwargs):
        return _FakeResponse()

    # _make_provider 用 object.__new__ 绕过 __init__，_router 属性不存在；
    # monkeypatch.setattr 对缺失属性默认 raising=True 会抛——直接赋值。
    provider._router = types.SimpleNamespace(acompletion=fake_acompletion)

    content, tool_calls, stop_reason = await provider.chat(
        [{"role": "user", "content": "hi"}]
    )

    assert content == "hello"
    assert stop_reason == "stop"
    assert len(calls) == 1
    model, secs, pt, ct = calls[0]
    assert model == "fake-model"
    assert isinstance(secs, float) and secs >= 0.0
    assert pt == 12
    assert ct == 7


# ── 3. agent_worker.dispatch_event → record_event_consumed ────────────────


class _FakeEventBus:
    def __init__(self):
        self.published = []

    def subscribe(self, event_cls, handler):
        pass

    def unsubscribe(self, handler):
        pass

    async def publish(self, event):
        self.published.append(event)


class _BrokenAgentLoader:
    def load(self, agent_id):
        raise DefNotFoundError("agent", agent_id)


async def _no_session_info(sid):
    return None


def _make_worker_context(bus) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        eventbus=bus,
        history_store=types.SimpleNamespace(get_session_info=_no_session_info),
        routing_table=types.SimpleNamespace(resolve=lambda src: "missing-agent"),
        agent_loader=_BrokenAgentLoader(),
        command_registry=None,
        bus_backend="memory",
        _session_factory=None,
    )


def _make_inbound_event() -> InboundEvent:
    return InboundEvent(
        session_id="s1",
        source=CliEventSource.from_string("platform-cli:cli-user"),
        content="hi",
    )


async def test_dispatch_event_records_consumed(monkeypatch):
    # worker 是模块级 `from .observability import record_event_consumed`，
    # 函数引用在 import 时已绑定——必须 patch agent_worker 命名空间里的名字。
    from ant.server import agent_worker as aw_module

    calls: list[object] = []
    monkeypatch.setattr(aw_module, "record_event_consumed", lambda evt: calls.append(evt))
    bus = _FakeEventBus()
    worker = AgentWorker(_make_worker_context(bus))
    event = _make_inbound_event()

    await worker.dispatch_event(event)

    # 入口计数一次，且是同一个事件对象；错误回传照常投递。
    assert calls == [event]
    assert len(bus.published) == 1


async def test_dispatch_event_survives_broken_observability(monkeypatch):
    """原则 11：record_event_consumed 抛异常不能打断事件分发。"""
    from ant.server import agent_worker as aw_module

    def boom(evt):
        raise RuntimeError("observability broke")

    monkeypatch.setattr(aw_module, "record_event_consumed", boom)
    bus = _FakeEventBus()
    worker = AgentWorker(_make_worker_context(bus))
    event = _make_inbound_event()

    await worker.dispatch_event(event)

    assert len(bus.published) == 1  # 错误回传仍发生


# ── 4. config 收口：字段默认值 + YAML 反序列化 ─────────────────────────────


def test_judge_enabled_is_real_field():
    assert InputGuardrailConfig().judge_enabled is False
    assert "judge_enabled" in InputGuardrailConfig.model_fields
    via_yaml = InputGuardrailConfig.model_validate({"judge_enabled": True})
    assert via_yaml.judge_enabled is True


def test_query_rewrite_enabled_is_real_field():
    assert MemoryConfig().query_rewrite_enabled is False
    assert "query_rewrite_enabled" in MemoryConfig.model_fields
    via_yaml = MemoryConfig.model_validate({"query_rewrite_enabled": True})
    assert via_yaml.query_rewrite_enabled is True


def test_full_config_yaml_round_trip():
    data = {
        "workspace": ".",
        "default_agent": "main",
        "llm": {"provider": "openai", "model": "gpt-4o-mini", "api_key": "k"},
        "guardrails": {"input": {"judge_enabled": True}},
        "memory": {"query_rewrite_enabled": True},
    }
    cfg = Config.model_validate(data)
    assert cfg.guardrails.input.judge_enabled is True
    assert cfg.memory.query_rewrite_enabled is True


# ── 5. memory_retriever：直接读真实字段（hasattr 防御兼容） ────────────────


async def test_rewrite_query_reads_real_field(monkeypatch):
    """query_rewrite_enabled=True → 走 LLM 改写（fake LLM 断言调用点）。"""
    calls: list[dict] = []

    class _FakeLLM:
        async def chat(self, messages, tools=None, **kwargs):
            calls.append(kwargs)
            return ("rewritten", [], "stop")

    retriever = MemoryRetriever(
        types.SimpleNamespace(
            config=types.SimpleNamespace(
                memory=MemoryConfig(query_rewrite_enabled=True),
                llm=types.SimpleNamespace(summarize_model=None),
            )
        )
    )
    monkeypatch.setattr(retriever, "_get_rewrite_llm", lambda: _FakeLLM())

    query = await retriever._rewrite_query("hello world")

    assert query == "rewritten"
    assert len(calls) == 1
    assert calls[0]["temperature"] == 0.0
    assert calls[0]["max_tokens"] == 128


async def test_rewrite_query_compat_without_field():
    """旧 fake config 没有 query_rewrite_enabled 字段 → hasattr 防御 → 直通。"""
    retriever = MemoryRetriever(
        types.SimpleNamespace(
            config=types.SimpleNamespace(
                memory=types.SimpleNamespace(),  # 无 query_rewrite_enabled
                llm=types.SimpleNamespace(summarize_model=None),
            )
        )
    )
    assert await retriever._rewrite_query("as-is") == "as-is"
