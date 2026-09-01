"""RabbitMQ-backed durable event bus (``RabbitMqBus``, aio-pika 10.x API).

Topology (all entities durable)
-------------------------------
``ant.events``   topic exchange   — messages are published here with the event
                                   class name as routing key.
``ant.<Class>``  queue (durable)  — one per subscribed event class, bound to
                                   ``ant.events`` with routing key = class
                                   name; ``x-dead-letter-exchange=ant.dlx``.
``ant.dlx``      fanout exchange  — dead-letter exchange for every ant.* queue.
``ant.retry``    queue (durable)  — retry queue with ``x-dead-letter-exchange
                                   = ant.events`` so an expired message
                                   re-enters its original ``ant.<Class>``
                                   queue (the original routing key is
                                   preserved through dead-lettering).
``ant.dlq``      queue (durable)  — final dead-letter queue.

Delivery semantics
------------------
* Consumer: prefetch=1, manual ack.
* Subscribe wrapper (the core contract): the handler returns normally ->
  automatic ``basic_ack``; the handler raises -> ``basic_nack(requeue=False)``
  and the message is dead-lettered to ``ant.dlx`` -> ``ant.retry``.
* Retry ladder: implemented with a *header-based dispatcher* consumer on
  ``ant.retry`` (chosen over a fixed TTL queue chain so the wait can grow per
  attempt).  The dispatcher reads the ``x-death`` header and re-publishes the
  message to ``ant.retry`` with a per-message ``expiration``: 1st failure 5s,
  then 30s / 2min / 10min / 30min by death count (``retry_delays_ms``).  Once
  the death count exceeds ``max_retries`` (5), the message is routed to
  ``ant.dlq`` instead.

``ack()`` / ``nack()`` are NO-OPs on this bus — broker-side acks are handled
automatically by the subscribe wrapper.  Workers may keep calling them for
API compatibility; the at-least-once semantics live here.

``publish()`` returns the generated ``message_id`` (uuid4 hex) so callers can
use it as an idempotency key.  ``start()`` raises on connection failure (the
caller decides whether to fall back to ``InMemoryBus``); ``stop()`` is
idempotent.  Credentials never appear in logs (URLs are masked).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from typing import Any, Callable

import aio_pika

from ant.bus.base import Handler
from ant.core.events import Event, deserialize_event
from ant.observability import tracing

logger = logging.getLogger(__name__)

_EXCHANGE_NAME = "ant.events"
_DLX_NAME = "ant.dlx"
_RETRY_QUEUE = "ant.retry"
_DLQ_QUEUE = "ant.dlq"
_PREFETCH_COUNT = 1
_DEFAULT_RETRY_DELAYS_MS = (5_000, 30_000, 120_000, 600_000, 1_800_000)

try:
    _PERSISTENT_DELIVERY_MODE = aio_pika.DeliveryMode.PERSISTENT
except AttributeError:  # pragma: no cover — older top-level layout
    _PERSISTENT_DELIVERY_MODE = getattr(aio_pika.Message, "PERSISTENT_DELIVERY_MODE", 2)


def _mask_url(url: str) -> str:
    """Mask the password (keep the username) so URLs are safe to log."""
    if "://" not in url or "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    authority, _, tail = rest.rpartition("@")
    if ":" in authority:
        user, _, _ = authority.partition(":")
        authority = f"{user}:***"
    return f"{scheme}://{authority}@{tail}"


class RabbitMqBus:
    """Durable topic-exchange event bus backed by RabbitMQ (aio-pika 10.x)."""

    def __init__(
        self,
        url: str,
        *,
        serializer: Callable[[Event], dict[str, Any]] | None = None,
        deserializer: Callable[[dict[str, Any]], Event] | None = None,
        retry_delays_ms: tuple[int, ...] = _DEFAULT_RETRY_DELAYS_MS,
        max_retries: int = 5,
        connect_timeout: float = 10.0,
    ) -> None:
        self._url = url
        self._serializer = serializer or (lambda event: event.to_dict())
        self._deserializer = deserializer or deserialize_event
        self.retry_delays_ms = retry_delays_ms
        self.max_retries = max_retries
        self._connect_timeout = connect_timeout
        self._handlers: dict[type[Event], list[Handler]] = defaultdict(list)
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._channel: aio_pika.abc.AbstractChannel | None = None
        self._exchange: aio_pika.abc.AbstractExchange | None = None
        self._declared: set[type[Event]] = set()
        self._declare_lock = asyncio.Lock()

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect, declare topology, start consumers. Idempotent.

        Raises the underlying connection error on failure so the caller can
        fall back to an in-process bus.
        """
        if self._connection is not None:
            return
        connection = None
        try:
            connection = await aio_pika.connect_robust(
                self._url, timeout=self._connect_timeout
            )
            await self._apply_connection_qos(connection)
            # Publisher confirms: channel() parameter on aio-pika 10.x (the
            # runtime set_publish_confirms() toggle was removed there).
            channel = await connection.channel(publisher_confirms=True)
            await channel.set_qos(prefetch_count=_PREFETCH_COUNT)
            exchange = await channel.declare_exchange(
                _EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True
            )
            self._connection, self._channel, self._exchange = connection, channel, exchange
            await self._setup_retry_infrastructure(channel)
            # _ensure_declared takes the declare lock itself (not reentrant).
            for event_class in list(self._handlers):
                await self._ensure_declared(event_class)
            logger.info("RabbitMqBus connected to %s", _mask_url(self._url))
        except BaseException:
            if connection is not None:
                try:
                    await connection.close()
                except Exception:
                    logger.debug("Failed to close partially-initialized connection")
            self._connection = self._channel = self._exchange = None
            raise

    async def stop(self) -> None:
        """Close the connection (cancels all consumers). Idempotent."""
        if self._connection is None:
            return
        connection, self._connection = self._connection, None
        self._channel = self._exchange = None
        self._declared.clear()
        try:
            await connection.close()
        finally:
            logger.info("RabbitMqBus stopped (%s)", _mask_url(self._url))

    # ── delivery contract ──────────────────────────────────────────────────

    async def publish(self, event: Event) -> str:
        """Publish an event (durable, persistent). Returns the message_id."""
        if self._exchange is None:
            raise RuntimeError("RabbitMqBus is not started")
        await self._ensure_declared(type(event))
        message_id = uuid.uuid4().hex
        message = aio_pika.Message(
            body=json.dumps(self._serializer(event), ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
            message_id=message_id,
            delivery_mode=_PERSISTENT_DELIVERY_MODE,
        )
        await self._exchange.publish(message, routing_key=type(event).__name__)
        return message_id

    def subscribe(self, event_class: type[Event], handler: Handler) -> None:
        """Register a handler; the queue is declared lazily (start or publish)."""
        self._handlers[event_class].append(handler)
        if self._connection is not None:
            asyncio.create_task(self._ensure_declared(event_class))

    def unsubscribe(self, handler: Callable) -> None:
        """Remove a handler from every event class it subscribed to."""
        for event_class in list(self._handlers):
            if handler in self._handlers[event_class]:
                self._handlers[event_class].remove(handler)

    async def ack(self, event: Event) -> None:
        """No-op — broker acks are handled automatically by the subscribe wrapper."""

    async def nack(self, event: Event, requeue: bool = False) -> None:
        """No-op — failing handlers are nacked(requeue=False) by the wrapper."""

    # ── internals ──────────────────────────────────────────────────────────

    async def _apply_connection_qos(self, connection: aio_pika.abc.AbstractConnection) -> None:
        """Connection-level prefetch (aio-pika 10.x) so consumer channels inherit it.

        Best-effort: older versions only have channel-level QoS, which applies
        to the publisher channel; consumers then use the broker default.
        """
        setter = getattr(connection, "set_qos", None)
        if callable(setter):
            try:
                await setter(prefetch_count=_PREFETCH_COUNT)
            except Exception:  # noqa: BLE001
                logger.debug("Connection-level QoS unavailable; using channel-level only")

    async def _setup_retry_infrastructure(self, channel: aio_pika.abc.AbstractChannel) -> None:
        dlx = await channel.declare_exchange(
            _DLX_NAME, aio_pika.ExchangeType.FANOUT, durable=True
        )
        retry_queue = await channel.declare_queue(
            _RETRY_QUEUE,
            durable=True,
            arguments={"x-dead-letter-exchange": _EXCHANGE_NAME},
        )
        await retry_queue.bind(dlx, routing_key="")
        await channel.declare_queue(_DLQ_QUEUE, durable=True)
        await retry_queue.consume(self._on_retry_message)

    async def _ensure_declared(self, event_class: type[Event]) -> None:
        """Declare + bind + consume the ``ant.<Class>`` queue exactly once."""
        if event_class in self._declared:
            return
        if self._channel is None or self._exchange is None:
            raise RuntimeError("RabbitMqBus is not started")
        async with self._declare_lock:
            if event_class in self._declared:
                return
            queue = await self._channel.declare_queue(
                f"ant.{event_class.__name__}",
                durable=True,
                arguments={"x-dead-letter-exchange": _DLX_NAME},
            )
            await queue.bind(self._exchange, routing_key=event_class.__name__)
            await queue.consume(self._on_message)
            self._declared.add(event_class)

    async def _on_message(self, message: aio_pika.abc.AbstractIncomingMessage) -> None:
        """Subscribe wrapper: ack on success, nack(requeue=False) on failure."""
        try:
            event = self._deserializer(json.loads(message.body.decode("utf-8")))
            # 把 broker 消息的 message_id 挂到事件上，供消费端幂等去重
            # （processed_messages 表）。Event 是无 slots 的 dataclass，动态属性安全。
            event.message_id = message.message_id
            # Phase 6 tracing：从事件载荷提取 traceparent，创建 consume span
            # 并以 use_span 包裹 handler——后续 Agent/LLM/Tool span 全部挂到
            # 消费链下（trace.md §8/§9：异步总线上的 Trace 传播）。
            from opentelemetry import trace as otel_trace

            span = tracing.start_consume_span(
                type(event).__name__, getattr(event, "traceparent", None), "rabbitmq"
            )
            with otel_trace.use_span(span, end_on_exit=True):
                for handler in list(self._handlers.get(type(event), ())):
                    await handler(event)
            await message.ack()
        except Exception:
            logger.exception(
                "Event handling failed; nacking to DLX (message_id=%s)", message.message_id
            )
            try:
                await message.nack(requeue=False)
            except Exception:
                logger.error("Failed to nack message %s", message.message_id)

    async def _on_retry_message(self, message: aio_pika.abc.AbstractIncomingMessage) -> None:
        """Retry dispatcher: re-publish with escalating TTL, or route to DLQ."""
        death_count = self._death_count(message)
        try:
            if death_count > self.max_retries:
                logger.warning(
                    "Event exceeded max retries (%d); routing to %s (message_id=%s)",
                    self.max_retries,
                    _DLQ_QUEUE,
                    message.message_id,
                )
                await self._relay_to(_DLQ_QUEUE, message, expiration=None)
            else:
                index = min(max(death_count, 1) - 1, len(self.retry_delays_ms) - 1)
                delay_ms = self.retry_delays_ms[index]
                logger.info(
                    "Scheduling retry in %d ms (death_count=%d, message_id=%s)",
                    delay_ms,
                    death_count,
                    message.message_id,
                )
                await self._relay_to(_RETRY_QUEUE, message, expiration=str(delay_ms))
            await message.ack()
        except Exception:
            logger.exception("Retry dispatcher failed; moving message to %s", _DLQ_QUEUE)
            try:
                await self._relay_to(_DLQ_QUEUE, message, expiration=None)
                await message.ack()
            except Exception:
                logger.error("Retry dispatcher could not salvage message %s", message.message_id)

    async def _relay_to(
        self,
        routing_key: str,
        message: aio_pika.abc.AbstractIncomingMessage,
        *,
        expiration: str | None,
    ) -> None:
        """Re-publish a message to another queue (retry with TTL, or DLQ)."""
        if self._channel is None:
            raise RuntimeError("RabbitMqBus is not started")
        relayed = aio_pika.Message(
            body=message.body,
            content_type=message.content_type,
            message_id=message.message_id,
            delivery_mode=_PERSISTENT_DELIVERY_MODE,
            headers=dict(message.headers or {}),
            expiration=expiration,
        )
        await self._channel.default_exchange.publish(relayed, routing_key=routing_key)

    @staticmethod
    def _death_count(message: aio_pika.abc.AbstractIncomingMessage) -> int:
        """Number of failures so far, derived from the ``x-death`` header.

        Only dead-letter hops originating from an ``ant.<Class>`` queue count
        as failures; hops via ``ant.retry`` (TTL expiry back to the main
        queue) are not failures.
        """
        x_death = (message.headers or {}).get("x-death")
        if not isinstance(x_death, list):
            return 0
        total = 0
        for entry in x_death:
            if isinstance(entry, dict) and entry.get("queue") != _RETRY_QUEUE:
                total += int(entry.get("count", 0))
        return total
