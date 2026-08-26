"""DeliveryWorker 的 bus 语义测试（Phase 1）。

覆盖 bus 后端切换后的投递语义：
  * memory 模式：成功 → 显式 ack 一次；失败（channel 抛异常）→ 不 ack；
    channel 缺失 → 不 ack（Phase 0 行为原样保留）。
  * rabbitmq 模式：成功 → 不显式 ack（broker 自动 ack）；
    失败 → 异常自然抛出（触发 broker nack → DLX 重试）；
    channel 缺失 → 抛异常。

用最小假 bus（满足 ant.bus.base.EventBus 协议）构造，不依赖真实
InMemoryBus/RabbitMqBus，也不需要网络。
"""
import pytest

from ant.core.events import AgentEventSource, OutboundEvent
from ant.server.delivery_worker import DeliveryWorker


class RecordingBus:
    """记录调用的最小 EventBus 协议实现（async ack/nack/publish）。"""

    def __init__(self):
        self.acked = []
        self.nacked = []
        self.published = []

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
    def __init__(self, session_id: str, source_str: str):
        self.id = session_id
        self.source = source_str

    def get_source(self):
        from ant.core.events import EventSource
        return EventSource.from_string(self.source)


class FakeHistoryStore:
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
    def __init__(self, channels=None, sessions=None, bus_backend="memory"):
        self.eventbus = RecordingBus()
        self.channels = channels or []
        self.history_store = FakeHistoryStore(sessions or [])
        self.config = FakeConfig()
        self.bus_backend = bus_backend
        self._session_factory = None


def _make_event(session_id="s1", content="hello world") -> OutboundEvent:
    return OutboundEvent(
        session_id=session_id,
        source=AgentEventSource(agent_id="a1"),
        content=content,
    )


# ── memory 模式：Phase 0 语义 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_delivery_success_acks():
    channel = FakeChannel(fail=False)
    ctx = FakeContext(
        channels=[channel],
        sessions=[FakeSession("s1", "platform-cli:cli-user")],
        bus_backend="memory",
    )
    worker = DeliveryWorker(ctx)
    event = _make_event()

    await worker.handle_event(event)

    assert channel.replies == ["hello world"]
    assert ctx.eventbus.acked == [event]


@pytest.mark.asyncio
async def test_memory_delivery_failure_no_ack():
    ctx = FakeContext(
        channels=[FakeChannel(fail=True)],
        sessions=[FakeSession("s1", "platform-cli:cli-user")],
        bus_backend="memory",
    )
    worker = DeliveryWorker(ctx)
    event = _make_event()

    # memory 模式：失败吞异常、不 ack（留给持久化文件 + _recover 重投）
    await worker.handle_event(event)

    assert ctx.eventbus.acked == []
    assert ctx.eventbus.nacked == []


@pytest.mark.asyncio
async def test_memory_channel_missing_no_ack():
    ctx = FakeContext(
        channels=[],
        sessions=[FakeSession("s1", "platform-cli:cli-user")],
        bus_backend="memory",
    )
    worker = DeliveryWorker(ctx)
    event = _make_event()

    await worker.handle_event(event)

    assert ctx.eventbus.acked == []


# ── rabbitmq 模式：单次投递 + 异常即 nack 信号 ─────────────────────────


@pytest.mark.asyncio
async def test_rabbit_delivery_success_no_explicit_ack():
    channel = FakeChannel(fail=False)
    ctx = FakeContext(
        channels=[channel],
        sessions=[FakeSession("s1", "platform-cli:cli-user")],
        bus_backend="rabbitmq",
    )
    worker = DeliveryWorker(ctx)
    event = _make_event()

    await worker.handle_event(event)

    assert channel.replies == ["hello world"]
    # RabbitMqBus 自动 ack：worker 不应显式 ack
    assert ctx.eventbus.acked == []


@pytest.mark.asyncio
async def test_rabbit_delivery_failure_raises():
    ctx = FakeContext(
        channels=[FakeChannel(fail=True)],
        sessions=[FakeSession("s1", "platform-cli:cli-user")],
        bus_backend="rabbitmq",
    )
    worker = DeliveryWorker(ctx)
    event = _make_event()

    # 失败必须抛出 → 订阅包装 nack → DLX 重试
    with pytest.raises(RuntimeError, match="channel offline"):
        await worker.handle_event(event)

    assert ctx.eventbus.acked == []


@pytest.mark.asyncio
async def test_rabbit_channel_missing_raises():
    ctx = FakeContext(
        channels=[],
        sessions=[FakeSession("s1", "platform-cli:cli-user")],
        bus_backend="rabbitmq",
    )
    worker = DeliveryWorker(ctx)
    event = _make_event()

    with pytest.raises(RuntimeError, match="No channel"):
        await worker.handle_event(event)

    assert ctx.eventbus.acked == []
