"""Bus unit tests — no network required.

Covers ``InMemoryBus`` (dispatch, handler exception isolation, outbound
persistence + ack/nack semantics, crash recovery, idempotent lifecycle) and
``CompositeBus`` routing (transient events never reach the durable bus,
persistent events go to the outbox writer or the durable bus, ack/nack and
start/stop pass through).
"""

import asyncio
import json

import pytest

from ant.bus import CompositeBus, InMemoryBus
from ant.core.events import (
    AgentEventSource,
    CliEventSource,
    ConfirmationRequestEvent,
    ConfirmationResponseEvent,
    DispatchEvent,
    InboundEvent,
    OutboundEvent,
    StreamChunkEvent,
)


def _inbound(session_id: str = "s1", content: str = "hi") -> InboundEvent:
    return InboundEvent(session_id=session_id, source=CliEventSource(), content=content)


def _outbound(session_id: str = "s1", content: str = "out") -> OutboundEvent:
    return OutboundEvent(
        session_id=session_id, source=AgentEventSource(agent_id="a1"), content=content
    )


# ─────────────────────────── InMemoryBus ───────────────────────────


async def test_inmemory_dispatches_to_subscribers(tmp_path):
    bus = InMemoryBus(pending_dir=tmp_path)
    received = []
    async def handler(event):
        received.append(event)
    bus.subscribe(InboundEvent, handler)
    await bus.start()
    event = _inbound()
    await bus.publish(event)
    await bus.flush()
    assert received == [event]
    await bus.stop()


async def test_inmemory_handler_exception_isolation(tmp_path):
    bus = InMemoryBus(pending_dir=tmp_path)
    calls = []
    async def bad_handler(event):
        calls.append("bad")
        raise RuntimeError("boom")
    async def good_handler(event):
        calls.append("good")
    bus.subscribe(InboundEvent, bad_handler)
    bus.subscribe(InboundEvent, good_handler)
    await bus.start()
    await bus.publish(_inbound())
    await bus.flush()
    # The raising handler is logged, not re-raised, and the other handler runs.
    assert calls == ["bad", "good"]
    await bus.stop()


async def test_inmemory_outbound_persisted_and_ack_deletes(tmp_path):
    bus = InMemoryBus(pending_dir=tmp_path)
    delivered = asyncio.Event()
    async def handler(event):
        delivered.set()
    bus.subscribe(OutboundEvent, handler)
    await bus.start()
    event = _outbound()
    await bus.publish(event)
    await asyncio.wait_for(delivered.wait(), timeout=5)

    pending = list(tmp_path.glob("*.json"))
    assert len(pending) == 1
    data = json.loads(pending[0].read_text(encoding="utf-8"))
    assert data["type"] == "OutboundEvent"
    assert data["session_id"] == event.session_id
    assert data["content"] == "out"

    await bus.ack(event)
    assert not pending[0].exists()
    await bus.stop()


async def test_inmemory_inbound_not_persisted(tmp_path):
    bus = InMemoryBus(pending_dir=tmp_path)
    bus.subscribe(InboundEvent, lambda event: asyncio.sleep(0))
    await bus.start()
    await bus.publish(_inbound())
    await bus.flush()
    assert list(tmp_path.glob("*.json")) == []
    await bus.stop()


async def test_inmemory_start_recovers_pending(tmp_path):
    event = _outbound(session_id="s2", content="crashed")
    filename = f"{event.timestamp}_{event.session_id}.json"
    (tmp_path / filename).write_text(json.dumps(event.to_dict()), encoding="utf-8")

    bus = InMemoryBus(pending_dir=tmp_path)
    recovered = []
    delivered = asyncio.Event()
    async def handler(recovered_event):
        recovered.append(recovered_event)
        delivered.set()
    bus.subscribe(OutboundEvent, handler)
    await bus.start()  # recovery happens here
    await asyncio.wait_for(delivered.wait(), timeout=5)

    assert recovered[0].session_id == "s2"
    assert recovered[0].content == "crashed"
    # Recovery re-dispatches but does NOT ack: the file survives until ack().
    assert (tmp_path / filename).exists()
    await bus.ack(event)
    assert not (tmp_path / filename).exists()
    await bus.stop()


async def test_inmemory_nack_is_noop(tmp_path):
    bus = InMemoryBus(pending_dir=tmp_path)
    delivered = asyncio.Event()
    async def handler(event):
        delivered.set()
    bus.subscribe(OutboundEvent, handler)
    await bus.start()
    event = _outbound()
    await bus.publish(event)
    await asyncio.wait_for(delivered.wait(), timeout=5)
    assert len(list(tmp_path.glob("*.json"))) == 1

    await bus.nack(event, requeue=True)
    assert len(list(tmp_path.glob("*.json"))) == 1  # untouched
    await bus.stop()


async def test_inmemory_unsubscribe(tmp_path):
    bus = InMemoryBus(pending_dir=tmp_path)
    calls = []
    async def handler(event):
        calls.append(event)
    bus.subscribe(InboundEvent, handler)
    bus.unsubscribe(handler)
    await bus.start()
    await bus.publish(_inbound())
    await bus.flush()
    assert calls == []
    await bus.stop()


async def test_inmemory_publish_before_start_delivers(tmp_path):
    bus = InMemoryBus(pending_dir=tmp_path)
    received = []
    async def handler(event):
        received.append(event)
    bus.subscribe(InboundEvent, handler)
    await bus.publish(_inbound())  # queue first…
    await bus.start()  # …consume later (legacy "queue then run" model)
    await bus.flush()
    assert len(received) == 1
    await bus.stop()


async def test_inmemory_start_stop_idempotent_and_publish_after_stop_raises(tmp_path):
    bus = InMemoryBus(pending_dir=tmp_path)
    await bus.start()
    await bus.start()  # no-op
    await bus.stop()
    await bus.stop()  # no-op
    with pytest.raises(RuntimeError):
        await bus.publish(_inbound())


# ─────────────────────────── CompositeBus ───────────────────────────


class FakeDurableBus:
    """Records every call; stands in for the RabbitMQ bus in routing tests."""

    def __init__(self):
        self.published = []
        self.acked = []
        self.nacked = []
        self.subscribed = []
        self.started = False
        self.stopped = False

    async def publish(self, event):
        self.published.append(event)

    def subscribe(self, event_class, handler):
        self.subscribed.append(event_class)

    def unsubscribe(self, handler):
        pass

    async def ack(self, event):
        self.acked.append(event)

    async def nack(self, event, requeue=False):
        self.nacked.append((event, requeue))

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


async def test_composite_transient_events_never_reach_durable():
    durable = FakeDurableBus()
    composite = CompositeBus(durable=durable)
    received = []
    delivered = asyncio.Event()
    async def handler(event):
        received.append(event)
        if len(received) == 3:
            delivered.set()
    for cls in (StreamChunkEvent, ConfirmationRequestEvent, ConfirmationResponseEvent):
        composite.subscribe(cls, handler)
    await composite.start()

    source = AgentEventSource(agent_id="a1")
    await composite.publish(
        StreamChunkEvent(session_id="s", source=source, content="tok")
    )
    await composite.publish(
        ConfirmationRequestEvent(
            session_id="s", source=source, content="", request_id="r1", tool_name="bash"
        )
    )
    await composite.publish(
        ConfirmationResponseEvent(
            session_id="s", source=source, content="", approved=True, request_session_id="r1"
        )
    )
    await asyncio.wait_for(delivered.wait(), timeout=5)

    assert durable.published == []  # transient events never hit the durable bus
    assert [type(e).__name__ for e in received] == [
        "StreamChunkEvent",
        "ConfirmationRequestEvent",
        "ConfirmationResponseEvent",
    ]
    await composite.stop()


async def test_composite_persistent_with_outbox_writer_goes_to_writer():
    durable = FakeDurableBus()
    written = []
    async def outbox_writer(event):
        written.append(event)
    composite = CompositeBus(durable=durable, outbox_writer=outbox_writer)
    await composite.start()

    event = _inbound()
    await composite.publish(event)
    assert written == [event]
    assert durable.published == []
    await composite.stop()


async def test_composite_persistent_without_writer_goes_to_durable():
    durable = FakeDurableBus()
    composite = CompositeBus(durable=durable)
    await composite.start()

    event = DispatchEvent(
        session_id="s", source=AgentEventSource(agent_id="a1"), content="x", parent_session_id="p"
    )
    await composite.publish(event)
    assert durable.published == [event]
    await composite.stop()


async def test_composite_subscribe_routes_by_event_class():
    durable = FakeDurableBus()
    composite = CompositeBus(durable=durable)
    async def handler(event):
        pass
    composite.subscribe(InboundEvent, handler)
    composite.subscribe(StreamChunkEvent, handler)
    assert durable.subscribed == [InboundEvent]  # only persistent class reached durable


async def test_composite_ack_nack_passthrough_to_durable():
    durable = FakeDurableBus()
    composite = CompositeBus(durable=durable)
    event = _inbound()
    await composite.ack(event)
    await composite.nack(event, requeue=False)
    assert durable.acked == [event]
    assert durable.nacked == [(event, False)]


async def test_composite_start_stop_drives_durable_and_local():
    durable = FakeDurableBus()
    composite = CompositeBus(durable=durable)
    await composite.start()
    assert durable.started
    await composite.stop()
    assert durable.stopped
