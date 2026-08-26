"""Worker 可靠性单元测试：投递失败不 ack、成功 ack、channel 缺失不 ack。

覆盖 improve.md §3 的 P0 bug #5（投递失败后无条件 ack 导致消息永久丢失）
以及 #2（agent 不存在时 agent_def 未赋值被引用导致 UnboundLocalError）。

全部使用假 eventbus / channel / history_store 构造，不依赖真实 SharedContext。
"""
import asyncio

from ant.core.events import AgentEventSource, CliEventSource, InboundEvent, OutboundEvent
from ant.server.agent_worker import AgentWorker
from ant.server.delivery_worker import DeliveryWorker
from ant.utils.def_loader import DefNotFoundError

# ─────────────────────────── 假对象 ───────────────────────────

class FakeEventBus:
    """记录 publish/ack 调用的假事件总线。

    Phase 1 起满足 ant.bus.base.EventBus 协议：publish/ack/nack/start/stop
    均为 async，subscribe/unsubscribe 为同步。
    """

    def __init__(self):
        self.published = []
        self.acked = []
        self.nacked = []

    def subscribe(self, event_class, handler):
        pass

    def unsubscribe(self, handler):
        pass

    async def publish(self, event):
        self.published.append(event)

    async def ack(self, event):
        self.acked.append(event)

    async def nack(self, event, requeue=False):
        self.nacked.append(event)

    async def start(self):
        pass

    async def stop(self):
        pass


class FakeSession:
    """HistorySession 的鸭子类型替身（只暴露 delivery_worker 用到的接口）"""

    def __init__(self, session_id: str, source_str: str):
        self.id = session_id
        self.source = source_str  # HistorySession 的 source 是序列化后的 EventSource 字符串

    def get_source(self):
        from ant.core.events import EventSource
        return EventSource.from_string(self.source)


class FakeHistoryStore:
    """Async HistoryRepository 协议的测试替身（Phase 1 全部方法为 async）"""

    def __init__(self, sessions):
        self._sessions = sessions

    async def list_sessions(self):
        return list(self._sessions)

    async def get_session_info(self, session_id):
        for session in self._sessions:
            if session.id == session_id:
                return session
        return None


class FakeConfig:
    default_delivery_source = ""


class FakeChannel:
    def __init__(self, platform_name="cli", fail=False):
        self.platform_name = platform_name
        self.fail = fail
        self.replies = []

    async def reply(self, content, source):
        if self.fail:
            raise RuntimeError("channel offline")
        self.replies.append(content)


class FakeContext:
    """DeliveryWorker/AgentWorker 依赖的最小上下文"""

    def __init__(self, channels=None, sessions=None, bus_backend="memory"):
        self.eventbus = FakeEventBus()
        self.channels = channels or []
        self.history_store = FakeHistoryStore(sessions or [])
        self.config = FakeConfig()
        self.routing_table = _FakeRouting()
        self.agent_loader = _FakeAgentLoader()
        self.bus_backend = bus_backend
        self._session_factory = None

    def make_delivery_worker(self):
        return DeliveryWorker(self)

    def make_agent_worker(self):
        return AgentWorker(self)


class _FakeRouting:
    def resolve(self, source_str: str) -> str:
        return "ghost_agent"


class _FakeAgentLoader:
    def load(self, agent_id: str):
        raise DefNotFoundError("agent", agent_id)


def _make_event(session_id="s1", content="hello world") -> OutboundEvent:
    return OutboundEvent(
        session_id=session_id,
        source=AgentEventSource(agent_id="a1"),
        content=content,
    )


async def _no_sleep(seconds):
    """测试中禁用真实退避 sleep，避免 5 次重试累计 10 分钟等待"""


# ─────────────────────────── 测试用例 ───────────────────────────

def test_delivery_failure_does_not_ack(monkeypatch):
    """(a) 投递失败（重试耗尽）时不得 ack，消息留给持久化文件 + _recover 重投"""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    ctx = FakeContext(
        channels=[FakeChannel(fail=True)],
        sessions=[FakeSession("s1", "platform-cli:cli-user")],
    )
    worker = ctx.make_delivery_worker()
    event = _make_event()

    asyncio.run(worker.handle_event(event))

    assert ctx.eventbus.acked == [], (
        f"投递失败后不应 ack，实际 acked={ctx.eventbus.acked}"
    )


def test_delivery_success_acks(monkeypatch):
    """(b) 投递成功时必须 ack 一次"""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    channel = FakeChannel(fail=False)
    ctx = FakeContext(channels=[channel], sessions=[FakeSession("s1", "platform-cli:cli-user")])
    worker = ctx.make_delivery_worker()
    event = _make_event()

    asyncio.run(worker.handle_event(event))

    assert channel.replies == ["hello world"]
    assert ctx.eventbus.acked == [event]


def test_channel_missing_does_not_ack(monkeypatch):
    """(c) 会话有合法平台来源但找不到对应 channel 时不得 ack"""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    ctx = FakeContext(channels=[], sessions=[FakeSession("s1", "platform-cli:cli-user")])
    worker = ctx.make_delivery_worker()
    event = _make_event()

    asyncio.run(worker.handle_event(event))

    assert ctx.eventbus.acked == [], (
        f"channel 缺失时不应 ack，实际 acked={ctx.eventbus.acked}"
    )


def test_agent_not_found_emits_error_response():
    """回归 improve.md #2：load 抛 DefNotFoundError 时返回错误消息而非 UnboundLocalError"""
    ctx = FakeContext()
    worker = ctx.make_agent_worker()
    event = InboundEvent(
        session_id="s9",
        source=CliEventSource(),
        content="hi",
    )

    # 修复前此处会抛 UnboundLocalError（agent_def 未赋值被引用）
    asyncio.run(worker.dispatch_event(event))

    assert len(ctx.eventbus.published) == 1
    outbound = ctx.eventbus.published[0]
    assert isinstance(outbound, OutboundEvent)
    assert outbound.content == "Agent not found: ghost_agent"
    assert outbound.error == "Agent not found: ghost_agent"
    # 响应来源回退到解析出的 agent_id，而不是崩溃
    assert outbound.source.agent_id == "ghost_agent"
