"""回归测试：tools 层三个 P0 修复（workspace/improve.md #6 #8 #9）。

覆盖：
  (a) ToolRegistry.execute_tool 对未知工具 / 幻觉参数 / 工具内部异常
      不再 raise 炸掉整轮，而是返回错误字符串（traceback 记录进日志）；
  (b) subagent_dispatch 等待子 agent 结果带超时，超时返回明确错误串；
  (c) harness_stream_chat 入口每个新 turn 调用 governance.reset_turn_counts()，
      保证 max_calls_per_turn 是 per-turn 限额而非 session 累计。
"""

import asyncio
import logging
import sys
import types
from pathlib import Path

import pytest

# 根 pyproject.toml 的 pytest pythonpath=src 配置由并行代理负责，
# 目前尚未写入，这里临时把 src 加入 sys.path 以保证 `import ant.*` 可用。
_SRC = Path(__file__).resolve().parents[2]  # src/ant/tests -> src
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ant.core.agent import AgentSession  # noqa: E402
from ant.core.events import AgentEventSource, DispatchResultEvent  # noqa: E402
from ant.tools import subagent_tool  # noqa: E402
from ant.tools.base import FunctionTool  # noqa: E402
from ant.tools.policy import ToolGovernance, ToolPolicy  # noqa: E402
from ant.tools.registry import ToolRegistry  # noqa: E402
from ant.tools.subagent_tool import create_subagent_dispatch_tool  # noqa: E402

# ---------------------------------------------------------------------------
# (a) ToolRegistry.execute_tool —— 错误回传策略（improve.md #9）
# ---------------------------------------------------------------------------

def _make_tool(name: str, impl) -> FunctionTool:
    return FunctionTool(
        name,
        "test tool",
        {"type": "object", "properties": {}, "required": []},
        impl,
    )


async def test_execute_tool_unknown_tool_returns_error_string() -> None:
    """未知工具（幻觉 tool name）不再 raise，返回错误串。"""
    registry = ToolRegistry()
    result = await registry.execute_tool("no_such_tool", session=object())
    assert isinstance(result, str)
    assert result.startswith("Tool not found")


async def test_execute_tool_bad_param_returns_error_string() -> None:
    """幻觉参数被 JSON Schema 校验拦截：返回错误串、工具不执行（Phase 2 参数校验）。

    注意：Phase 2 起多余参数由 validate_args 在 execute_tool 内提前拦截，
    不再走到函数调用 TypeError 分支；错误串以「参数校验失败」开头并点名参数。
    """
    executed: list[bool] = []

    async def _impl(session) -> str:
        executed.append(True)
        return "ok"

    registry = ToolRegistry()
    registry.register(_make_tool("simple_tool", _impl))

    result = await registry.execute_tool(
        "simple_tool", session=object(), hallucinated_param=1
    )
    assert isinstance(result, str)
    assert result.startswith("参数校验失败")
    assert "hallucinated_param" in result
    assert executed == []  # 校验失败的工具不执行


async def test_execute_tool_exception_returns_error_and_logs_traceback(
    caplog,
) -> None:
    """工具内部异常返回错误串，且完整 traceback 记入日志（不静默吞）。"""
    async def _boom(session) -> str:
        raise RuntimeError("boom")

    registry = ToolRegistry()
    registry.register(_make_tool("boom_tool", _boom))

    with caplog.at_level(logging.ERROR, logger="ant.tools.registry"):
        result = await registry.execute_tool("boom_tool", session=object())

    assert result == "Tool execution error: boom"
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "expected an ERROR log record with traceback"
    record = error_records[0]
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError


async def test_execute_tool_exception_still_recorded_by_governance() -> None:
    """错误回传改为字符串后，治理审计仍记录该次调用。"""
    async def _boom(session) -> str:
        raise RuntimeError("boom")

    governance = ToolGovernance(ToolPolicy())
    registry = ToolRegistry(governance=governance)
    registry.register(_make_tool("boom_tool", _boom))

    await registry.execute_tool("boom_tool", session=object())

    summary = governance.get_audit_summary()
    assert summary["total_calls"] == 1
    assert summary["calls_by_tool"] == {"boom_tool": 1}
    assert summary["recent_log"][0]["tool"] == "boom_tool"


async def test_execute_tool_sandbox_violation_path_unchanged() -> None:
    """SandboxViolation 的既有错误回传策略不被破坏。"""
    from ant.core.sandbox import SandboxViolation

    async def _violate(session) -> str:
        raise SandboxViolation("blocked path", violation_type="path")

    registry = ToolRegistry()
    registry.register(_make_tool("path_tool", _violate))

    result = await registry.execute_tool("path_tool", session=object())
    assert result.startswith("Safety violation (path):")


# ---------------------------------------------------------------------------
# (b) subagent_dispatch 超时（improve.md #8）
# ---------------------------------------------------------------------------

class _FakeAgentDef:
    id = "other"
    description = "stub subagent"


class _FakeAgentLoader:
    def discover_agents(self):
        return [_FakeAgentDef()]

    def load(self, agent_id):
        return _FakeAgentDef()


class _SilentEventBus:
    """publish 后不派发任何 DispatchResultEvent —— 子 agent 永远不回。"""

    def __init__(self):
        self.handlers = []
        self.published = []

    def subscribe(self, event_cls, handler):
        self.handlers.append(handler)

    def unsubscribe(self, handler):
        if handler in self.handlers:
            self.handlers.remove(handler)

    async def publish(self, event):
        self.published.append(event)


class _ResultEventBus(_SilentEventBus):
    """publish 时派发结果 —— 模拟子 agent 正常返回。

    handler 是 async 函数，须与真实 EventBus._notify_subscribers 一样 await。
    """

    async def publish(self, event):
        await super().publish(event)
        for handler in list(self.handlers):
            await handler(DispatchResultEvent(
                session_id="sub-session-1",
                source=AgentEventSource(agent_id="other"),
                content="sub done",
            ))


class _FakeAgent:
    def __init__(self, agent_def, shared_context):
        self.agent_def = agent_def

    async def new_session(self, source):
        return types.SimpleNamespace(session_id="sub-session-1")


def _stub_agent_class(monkeypatch) -> None:
    """把工具函数内惰性导入的 ant.core.agent.Agent 换成桩，
    避免单测拉起真实 Agent / LLM 链路。"""
    fake_module = types.ModuleType("ant.core.agent")
    fake_module.Agent = _FakeAgent
    monkeypatch.setitem(sys.modules, "ant.core.agent", fake_module)


def _make_context(eventbus) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        agent_loader=_FakeAgentLoader(),
        eventbus=eventbus,
    )


def test_subagent_default_timeout_is_180_seconds() -> None:
    """模块级默认超时为 180s（improve.md #8 要求）。"""
    assert subagent_tool.SUBAGENT_DISPATCH_TIMEOUT_SECONDS == 180.0


async def test_subagent_timeout_returns_error_string(monkeypatch) -> None:
    """子 agent 不返回时，超时后返回明确错误串而非永久挂死。"""
    _stub_agent_class(monkeypatch)
    monkeypatch.setattr(
        subagent_tool, "SUBAGENT_DISPATCH_TIMEOUT_SECONDS", 0.05
    )

    context = _make_context(_SilentEventBus())
    tool = create_subagent_dispatch_tool("main-agent", context)
    assert tool is not None

    result = await tool.execute(
        session=types.SimpleNamespace(session_id="main-session"),
        agent_id="other",
        task="do the thing",
    )
    assert "timed out" in result
    assert "0.05s" in result


async def test_subagent_success_path_still_works(monkeypatch) -> None:
    """加超时后，子 agent 正常返回的路径不受影响。"""
    _stub_agent_class(monkeypatch)
    # 测试内同样使用短超时，防止回归时把测试套件挂 180s
    monkeypatch.setattr(
        subagent_tool, "SUBAGENT_DISPATCH_TIMEOUT_SECONDS", 5.0
    )

    context = _make_context(_ResultEventBus())
    tool = create_subagent_dispatch_tool("main-agent", context)
    assert tool is not None

    result = await tool.execute(
        session=types.SimpleNamespace(session_id="main-session"),
        agent_id="other",
        task="do the thing",
    )
    assert '"sub done"' in result
    assert '"sub-session-1"' in result


# ---------------------------------------------------------------------------
# (c) per-turn 限额重置（improve.md #6）
# ---------------------------------------------------------------------------

class _FakeGovernance:
    def __init__(self):
        self.reset_calls = 0

    def reset_turn_counts(self):
        self.reset_calls += 1


class _FakeConfirmationBroker:
    def reset_turn(self, session_id):
        pass


async def _retrieve_memories():
    return None


def _make_fake_session(governance) -> types.SimpleNamespace:
    """构造 harness_stream_chat 入口所需的最小假 session。

    入口依次调用 _truncate_old_tool_results → confirmation_broker.reset_turn
    → (reset_turn_counts) → _retrieve_memories，之后才会触碰 fsm/tracer。
    这里补齐前几项，使生成器能执行到 reset 点；缺少的 fsm 让生成器在
    reset 之后抛 AttributeError（预期，见各测试）。
    """
    return types.SimpleNamespace(
        _truncate_old_tool_results=lambda: None,
        _retrieve_memories=_retrieve_memories,
        session_id="s1",  # AgentSession 的 session_id 是 property，fake 需直接提供
        state=types.SimpleNamespace(messages=[], session_id="s1"),
        shared_context=types.SimpleNamespace(
            confirmation_broker=_FakeConfirmationBroker(),
            memory_retriever=None,
        ),
        tools=types.SimpleNamespace(_governance=governance),
    )


def test_harness_stream_chat_entry_resets_turn_counts() -> None:
    """harness_stream_chat 入口在每个新 turn 调用 reset_turn_counts()。

    说明：pipeline 后续阶段依赖真实 LLM 调用，单测无法完整驱动；
    此处用假 session 驱动生成器到 reset 点，验证入口行为。假 session
    缺少 fsm/tracer，reset 之后会抛 AttributeError，属预期，只断言
    reset 已被调用。
    """
    governance = _FakeGovernance()
    generator = AgentSession.harness_stream_chat(
        _make_fake_session(governance), "hello"
    )

    with pytest.raises(AttributeError):
        asyncio.run(anext(generator))

    assert governance.reset_calls == 1


def test_harness_stream_chat_skips_reset_without_governance() -> None:
    """未配置 governance 的 session（tools._governance 为 None）不报错。"""
    generator = AgentSession.harness_stream_chat(
        _make_fake_session(None), "hello"
    )

    with pytest.raises(AttributeError):
        asyncio.run(anext(generator))


def test_governance_turn_counts_are_per_turn_after_reset() -> None:
    """reset_turn_counts 清空 per-turn 计数、保留 session 计数。"""
    policy = ToolPolicy(
        max_calls_per_turn={"bash": 1},
        max_calls_per_session={"bash": 5},
    )
    governance = ToolGovernance(policy)

    governance.record_call("bash", {}, "ok", 0.1)
    allowed, _ = governance.check_permission("bash", session=object())
    assert allowed is False  # per-turn 额度已用尽

    governance.reset_turn_counts()  # 模拟新 turn 开始

    allowed, reason = governance.check_permission("bash", session=object())
    assert allowed is True  # 新 turn 重新获得额度
    assert reason == ""

    summary = governance.get_audit_summary()
    assert summary["total_calls"] == 1  # session 计数不受 reset 影响
