"""RabbitMQ integration tests (skipped when the broker is unreachable).

These tests require a reachable RabbitMQ server with credentials provided in
``.env`` (``RABBITMQ_USERNAME`` / ``RABBITMQ_PASSWORD``; host/port default to
127.0.0.1:5672).  They are *skipped* (never failed) when credentials are
incomplete or the broker cannot be reached; the skip reason names only the
failure class — never the URL or credentials.

When run, they exercise ``RabbitMqBus`` end-to-end against a real broker:

* publish -> subscribe round trip,
* distinct message_id per publish, both delivered,
* message_id preserved through the whole DLX/retry chain, and
* a failing handler eventually lands the message in ``ant.dlq``.
"""

import asyncio
import json
import time
import uuid

import aio_pika
import pytest

from ant.bus import RabbitMqBus
from ant.core.events import CliEventSource, Event, deserialize_event
from ant.utils.settings import InfraSettings


class _BusProbeEvent(Event):
    """Test-only event type: keeps integration probes off the app's queues."""


def _probe_deserialize(data: dict) -> Event:
    """Deserialize the probe event, falling back to the app's registry."""
    if data.get("type") == "_BusProbeEvent":
        return _BusProbeEvent.from_dict(data)
    return deserialize_event(data)


def _probe_event(content: str = "probe") -> _BusProbeEvent:
    return _BusProbeEvent(
        session_id=f"it-{uuid.uuid4().hex[:8]}",
        source=CliEventSource(),
        content=content,
        timestamp=float(time.time()),
    )


@pytest.fixture(scope="module")
async def rabbitmq_url():
    """AMQP URL from .env; skip the whole module when unusable.

    Credentials discipline: the skip reason only names the failure class
    (e.g. AMQPConnectionError / TimeoutError) — never the URL or password.
    """
    infra = InfraSettings()
    url = infra.rabbitmq_url()
    if url is None:
        pytest.skip(
            "RabbitMQ credentials not configured in .env "
            "(RABBITMQ_USERNAME / RABBITMQ_PASSWORD missing)"
        )
    try:
        connection = await aio_pika.connect_robust(url, timeout=5)
        await connection.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"RabbitMQ unreachable ({type(exc).__name__}); integration tests skipped")
    return url


async def test_publish_subscribe_round_trip(rabbitmq_url):
    bus = RabbitMqBus(rabbitmq_url, deserializer=_probe_deserialize)
    received = asyncio.Queue()
    async def handler(event):
        await received.put(event)
    bus.subscribe(_BusProbeEvent, handler)
    await bus.start()
    try:
        event = _probe_event(content="hello round-trip")
        await bus.publish(event)
        got = await asyncio.wait_for(received.get(), timeout=20)
        assert got.session_id == event.session_id
        assert got.content == "hello round-trip"
        assert got.source == event.source
    finally:
        await bus.stop()


async def test_duplicate_publishes_get_distinct_message_ids_and_both_deliver(rabbitmq_url):
    bus = RabbitMqBus(rabbitmq_url, deserializer=_probe_deserialize)
    received = asyncio.Queue()
    async def handler(event):
        await received.put(event)
    bus.subscribe(_BusProbeEvent, handler)
    await bus.start()
    try:
        event_one = _probe_event(content="one")
        event_two = _probe_event(content="two")
        mid_one = await bus.publish(event_one)
        mid_two = await bus.publish(event_two)
        assert len(mid_one) == 32 and len(mid_two) == 32  # uuid4 hex
        assert mid_one != mid_two

        got = [await asyncio.wait_for(received.get(), timeout=20) for _ in range(2)]
        assert {g.content for g in got} == {"one", "two"}
    finally:
        await bus.stop()


async def test_failing_handler_event_lands_in_dlq(rabbitmq_url):
    """Handler failure -> nack -> DLX -> retry ladder -> ant.dlq.

    Uses a compressed retry ladder (100-500 ms) so the six failures complete
    in a couple of seconds instead of the production ladder (5s/30s/2m/10m/30m);
    the message_id is asserted to survive the entire chain unchanged.
    """
    bus = RabbitMqBus(
        rabbitmq_url,
        deserializer=_probe_deserialize,
        retry_delays_ms=(100, 200, 300, 400, 500),
        max_retries=5,
    )
    async def failing_handler(event):
        raise RuntimeError("probe failure")
    bus.subscribe(_BusProbeEvent, failing_handler)
    await bus.start()
    try:
        event = _probe_event(content="will fail")
        mid = await bus.publish(event)

        found = None
        deadline = time.monotonic() + 60
        connection = await aio_pika.connect_robust(rabbitmq_url)
        try:
            channel = await connection.channel()
            dlq = await channel.declare_queue("ant.dlq", durable=True)
            while time.monotonic() < deadline:
                try:
                    message = await dlq.get(timeout=2)
                except aio_pika.exceptions.QueueEmpty:
                    continue
                if message.message_id == mid:
                    found = message
                    break
                # Leftover from an earlier run — discard and keep polling.
                try:
                    await message.ack()
                except Exception:  # noqa: BLE001 — cleanup only
                    pass
        finally:
            await connection.close()

        assert found is not None, "event did not reach ant.dlq within 60s"
        body = json.loads(found.body.decode("utf-8"))
        assert body["type"] == "_BusProbeEvent"
        assert body["session_id"] == event.session_id
        assert body["content"] == "will fail"
    finally:
        await bus.stop()
