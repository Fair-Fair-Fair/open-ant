"""Phase 4E 测试：AgentWorker 侧子代理任务级联取消（Phase 2 遗留项）。

覆盖：
  (a) 主会话任务被取消（CancelledError 路径）→ 以该会话为 parent 的子代理
      任务被级联 cancel（真实 asyncio 任务），并记 warning 日志；
  (b) 任务完成（正常完成 / 被取消）后从 _session_tasks 注册表移除；
  (c) 无子任务时取消主任务不炸（空注册表安全路径）。

边界：级联取消基于本进程内的任务注册表——rabbitmq 多 worker 模式下子代理
可能运行在别的进程，无法跨进程取消（见 AgentWorker._session_tasks docstring）。

注册表键说明：DispatchEvent（子代理）任务按 parent_session_id 注册，主会话
（InboundEvent）任务按自身 session_id 注册——因此取消主会话时可在 O(1) 内
找到其全部子代理任务。
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

from ant.core.events import (  # noqa: E402
    AgentEventSource,
    CliEventSource,
    DispatchEvent,
    InboundEvent,
)
from ant.server import agent_worker as aw_module  # noqa: E402
from ant.server.agent_worker import AgentWorker  # noqa: E402

# ── 假对象 ───────────────────────────────────────────────────────────────


class _FakeEventBus:
    """记录 publish 的假事件总线（AgentWorker 构造时会被 subscribe）。"""

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


class _FakeHistoryStore:
    async def get_session_info(self, session_id):
        return None  # 回退到 routing


class _FakeRouting:
    def resolve(self, source_str: str) -> str:
        return "main-agent"


class _FakeAgentDef:
    id = "main-agent"
    max_concurrency = 2


class _FakeAgentLoader:
    def load(self, agent_id):
        return _FakeAgentDef()


class _FakeCommandRegistry:
    async def dispatch(self, content, session):
        return None


def _make_context(bus) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        eventbus=bus,
        history_store=_FakeHistoryStore(),
        routing_table=_FakeRouting(),
        agent_loader=_FakeAgentLoader(),
        command_registry=_FakeCommandRegistry(),
        bus_backend="memory",
        _session_factory=None,
        _entered=asyncio.Event(),  # harness_stream_chat 进入后 set，供测试同步
    )


class _FakeSession:
    """Blocking 会话：harness_stream_chat 永久 sleep，直到被取消。"""

    def __init__(self, session_id, entered):
        self.session_id = session_id
        self._entered = entered

    async def harness_stream_chat(self, content):
        self._entered.set()
        while True:
            await asyncio.sleep(3600)
            yield {"type": "token", "data": "x"}


class _FakeAgent:
    """替换 agent_worker 模块里的 Agent（monkeypatch aw_module.Agent）。"""

    def __init__(self, agent_def, context):
        self._entered = context._entered

    async def resume_session(self, session_id):
        return _FakeSession(session_id, self._entered)

    async def new_session(self, source, session_id=None):
        return _FakeSession(session_id or "fresh", self._entered)


async def _wait_entered(ctx, timeout: float = 2.0) -> None:
    try:
        await asyncio.wait_for(ctx._entered.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pytest.fail("session never entered harness_stream_chat")


# ── (a) 主任务取消 → 级联取消子代理 ─────────────────────────────────────


async def test_main_cancellation_cascades_to_subagent(monkeypatch, caplog) -> None:
    """主会话任务取消：子代理任务被级联 cancel + warning 日志 + 注册表清理。"""
    monkeypatch.setattr(aw_module, "Agent", _FakeAgent)
    ctx = _make_context(_FakeEventBus())
    worker = AgentWorker(ctx)

    await worker.dispatch_event(
        InboundEvent(session_id="main-s1", source=CliEventSource(), content="hi")
    )
    main_task = next(iter(worker._session_tasks["main-s1"]))

    await worker.dispatch_event(
        DispatchEvent(
            session_id="sub-s1",
            source=AgentEventSource(agent_id="main-agent"),
            content="sub task",
            parent_session_id="main-s1",
        )
    )
    sub_task = next(t for t in worker._session_tasks["main-s1"] if t is not main_task)
    assert worker._session_tasks["main-s1"] == {main_task, sub_task}

    # 等主会话任务真正进入 harness_stream_chat（block 在 sleep 里）
    await _wait_entered(ctx)

    main_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await main_task
    with pytest.raises(asyncio.CancelledError):
        await sub_task

    assert main_task.cancelled()
    assert sub_task.cancelled()
    # 级联取消的 warning 日志
    assert any("cascading cancel" in r.getMessage() for r in caplog.records)
    await asyncio.sleep(0)  # done callbacks 有机会执行
    assert worker._session_tasks == {}


# ── (b) done 后注册表清理 ────────────────────────────────────────────────


async def test_registry_cleaned_after_normal_completion(monkeypatch) -> None:
    """任务正常完成后从 _session_tasks 移除。"""

    class _DoneSession:
        session_id = "done-s1"

        async def harness_stream_chat(self, content):
            yield {"type": "done"}

    class _DoneAgent:
        def __init__(self, agent_def, context):
            pass

        async def resume_session(self, session_id):
            return _DoneSession()

        async def new_session(self, source, session_id=None):
            return _DoneSession()

    monkeypatch.setattr(aw_module, "Agent", _DoneAgent)
    worker = AgentWorker(_make_context(_FakeEventBus()))

    await worker.dispatch_event(
        InboundEvent(session_id="done-s1", source=CliEventSource(), content="hi")
    )
    task = next(iter(worker._session_tasks["done-s1"]))
    await task  # 正常完成
    await asyncio.sleep(0)  # done callback 移除注册
    assert worker._session_tasks == {}


# ── (c) 无子任务时取消不炸 ───────────────────────────────────────────────


async def test_cancel_without_children_is_safe(monkeypatch) -> None:
    """没有子代理任务时取消主会话：不炸、无级联、注册表清理。"""
    monkeypatch.setattr(aw_module, "Agent", _FakeAgent)
    ctx = _make_context(_FakeEventBus())
    worker = AgentWorker(ctx)

    await worker.dispatch_event(
        InboundEvent(session_id="solo-s1", source=CliEventSource(), content="hi")
    )
    task = next(iter(worker._session_tasks["solo-s1"]))
    await _wait_entered(ctx)

    task.cancel()  # 无子任务 → 安全路径
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert worker._session_tasks == {}
