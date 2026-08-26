"""Phase 2 #5 测试：subagent_dispatch 的预算传递与取消传播。

覆盖：
  (a) timeout_seconds 参数生效：短超时 + 未完成 future → 超时错误串；
  (b) 缺省 timeout_seconds 沿用模块级 180 常量（monkeypatch 常量验证流向）；
  (c) 主任务被取消时 finally 的 unsubscribe 被调用（先 unsubscribe 再 re-raise），
      且本地 wait 任务被显式取消、从 session._pending_tasks 移除；
  (d) schema 里 timeout_seconds 存在（integer / min 10 / max 600，非 required）——
      additionalProperties 由并行代理处理，此处不断言。

事件结构说明：DispatchEvent 不做字段扩展（在别的作用域），子代理预算
= 主侧 wait_for 超时 + timeout_seconds 参数；event 结构不动。
"""

import asyncio
import sys
import types
from pathlib import Path

import pytest

# 根 pyproject.toml 的 pytest pythonpath=src 配置由并行代理负责，
# 目前尚未写入，这里临时把 src 加入 sys.path 以保证 `import ant.*` 可用。
_SRC = Path(__file__).resolve().parents[2]  # src/ant/tests -> src
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ant.core.events import DispatchEvent  # noqa: E402
from ant.tools import subagent_tool  # noqa: E402
from ant.tools.subagent_tool import (  # noqa: E402
    SUBAGENT_DISPATCH_TIMEOUT_MAX_SECONDS,
    SUBAGENT_DISPATCH_TIMEOUT_SECONDS,
    create_subagent_dispatch_tool,
)


class _FakeAgentDef:
    id = "other"
    description = "stub subagent"


class _FakeAgentLoader:
    def discover_agents(self):
        return [_FakeAgentDef()]

    def load(self, agent_id):
        return _FakeAgentDef()


class _FakeAgent:
    def __init__(self, agent_def, shared_context):
        self.agent_def = agent_def

    async def new_session(self, source):
        return types.SimpleNamespace(session_id="sub-session-1")


class _RecordingEventBus:
    """publish 后不派发结果（子 agent 永不回）；记录 publish 的 DispatchEvent。"""

    def __init__(self):
        self.handlers = []
        self.published = []
        self.unsubscribe_count = 0

    def subscribe(self, event_cls, handler):
        self.handlers.append(handler)

    def unsubscribe(self, handler):
        self.unsubscribe_count += 1
        if handler in self.handlers:
            self.handlers.remove(handler)

    async def publish(self, event):
        self.published.append(event)


def _stub_agent_class(monkeypatch) -> None:
    """把工具函数内惰性导入的 ant.core.agent.Agent 换成桩，
    避免单测拉起真实 Agent / LLM 链路。"""
    fake_module = types.ModuleType("ant.core.agent")
    fake_module.Agent = _FakeAgent
    monkeypatch.setitem(sys.modules, "ant.core.agent", fake_module)


def _make_tool(monkeypatch, bus):
    _stub_agent_class(monkeypatch)
    return create_subagent_dispatch_tool("main-agent", _make_context(bus))


def _make_context(eventbus) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        agent_loader=_FakeAgentLoader(),
        eventbus=eventbus,
    )


# ---------------------------------------------------------------------------
# (a) timeout_seconds 参数生效
# ---------------------------------------------------------------------------

async def test_timeout_seconds_param_controls_wait(monkeypatch) -> None:
    """显式传短 timeout_seconds：未完成 future 超时后返回明确错误串。"""
    bus = _RecordingEventBus()
    tool = _make_tool(monkeypatch, bus)
    assert tool is not None

    result = await tool.execute(
        session=types.SimpleNamespace(session_id="main-session"),
        agent_id="other",
        task="do the thing",
        timeout_seconds=0.05,
    )
    assert "timed out" in result
    assert "0.05s" in result

    # 派发的 DispatchEvent 不新增字段（事件结构在别的作用域，保持不动）
    assert len(bus.published) == 1
    assert isinstance(bus.published[0], DispatchEvent)
    assert bus.published[0].content == "do the thing"


async def test_timeout_seconds_capped_at_max(monkeypatch) -> None:
    """超过 schema 上限的幻觉值被封顶，不能击穿超时安全网。"""
    monkeypatch.setattr(
        subagent_tool, "SUBAGENT_DISPATCH_TIMEOUT_MAX_SECONDS", 0.05
    )
    tool = _make_tool(monkeypatch, _RecordingEventBus())
    assert tool is not None

    result = await tool.execute(
        session=types.SimpleNamespace(session_id="main-session"),
        agent_id="other",
        task="do the thing",
        timeout_seconds=600000,
    )
    assert "timed out" in result
    assert "0.05s" in result


# ---------------------------------------------------------------------------
# (b) 缺省 180 常量
# ---------------------------------------------------------------------------

def test_default_budget_constants() -> None:
    """模块级默认 180s / 上限 600s。"""
    assert SUBAGENT_DISPATCH_TIMEOUT_SECONDS == 180.0
    assert SUBAGENT_DISPATCH_TIMEOUT_MAX_SECONDS == 600.0


async def test_default_timeout_uses_module_constant(monkeypatch) -> None:
    """缺省 timeout_seconds 时沿用模块级常量（默认值在工厂创建时绑定）。"""
    monkeypatch.setattr(
        subagent_tool, "SUBAGENT_DISPATCH_TIMEOUT_SECONDS", 0.05
    )
    tool = _make_tool(monkeypatch, _RecordingEventBus())
    assert tool is not None

    result = await tool.execute(
        session=types.SimpleNamespace(session_id="main-session"),
        agent_id="other",
        task="do the thing",
    )
    assert "timed out" in result
    assert "0.05s" in result


async def test_invalid_timeout_seconds_falls_back_to_default(monkeypatch) -> None:
    """非数值 budget（LLM 幻觉）回退到模块级默认，不炸掉整轮。"""
    tool = _make_tool(monkeypatch, _RecordingEventBus())
    assert tool is not None
    # 回退读取的是函数体内的模块级常量，可在创建后 patch
    monkeypatch.setattr(
        subagent_tool, "SUBAGENT_DISPATCH_TIMEOUT_SECONDS", 0.05
    )

    result = await tool.execute(
        session=types.SimpleNamespace(session_id="main-session"),
        agent_id="other",
        task="do the thing",
        timeout_seconds="not-a-number",
    )
    assert "timed out" in result
    assert "0.05s" in result


# ---------------------------------------------------------------------------
# (c) 主任务取消 → 先 unsubscribe 再 re-raise
# ---------------------------------------------------------------------------

async def test_main_cancellation_unsubscribes_before_reraise(monkeypatch) -> None:
    """主任务被取消：handler 被 unsubscribe（finally 兜底 + CancelledError
    路径先卸载），wait 任务被取消并从 session._pending_tasks 移除。"""
    bus = _RecordingEventBus()
    tool = _make_tool(monkeypatch, bus)
    assert tool is not None

    session = types.SimpleNamespace(
        session_id="main-session",
        _pending_tasks=[],
    )
    call_task = asyncio.create_task(
        tool.execute(session=session, agent_id="other", task="do the thing")
    )

    # 等工具已发布 DispatchEvent 并把 wait 任务注册到 session（子 agent
    # 永不回，不会提前完成；publish 与注册之间没有 await 点，轮询即可）
    for _ in range(200):
        if bus.published and session._pending_tasks:
            break
        await asyncio.sleep(0.01)
    assert bus.published, "tool never published the DispatchEvent"
    assert len(session._pending_tasks) == 1

    call_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call_task
    await asyncio.sleep(0)  # 让 wait_task 的 done 回调有机会执行

    assert bus.unsubscribe_count >= 1
    assert bus.handlers == []
    assert session._pending_tasks == []
    # 取消传播边界：没有 worker 侧取消机制，只能确保本地 wait 被拆除
    assert call_task.cancelled()


# ---------------------------------------------------------------------------
# (d) schema 声明
# ---------------------------------------------------------------------------

def test_schema_declares_timeout_seconds() -> None:
    """timeout_seconds 在 schema 中声明为可选 integer（10~600），
    required 不变。additionalProperties 由并行代理处理，不断言。"""
    tool = create_subagent_dispatch_tool(
        "main-agent", _make_context(_RecordingEventBus())
    )
    assert tool is not None

    props = tool.parameters["properties"]
    assert "timeout_seconds" in props
    spec = props["timeout_seconds"]
    assert spec["type"] == "integer"
    assert spec["minimum"] == 10
    assert spec["maximum"] == 600
    assert "timeout_seconds" not in tool.parameters["required"]
    assert tool.parameters["required"] == ["agent_id", "task"]
