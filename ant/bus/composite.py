"""Composite event bus: routes events between a durable bus and a local bus.

Architecture decision (workspace/plan.md, design principle 1): streaming
tokens and confirmation handshakes are TRANSIENT — they never touch the
broker.  Only persistent events (inbound / outbound / dispatch /
dispatch-result) are durable: they go through the durable bus (RabbitMQ) or —
when an ``outbox_writer`` is supplied — are first written to the transactional
outbox so the DB transaction and the event emission stay atomic (outbox
pattern, plan.md principle 2).  Transient events are always delivered through
an internal in-process ``InMemoryBus``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Awaitable, Callable

from ant.bus.base import EventBus
from ant.bus.memory import InMemoryBus
from ant.core.events import (
    ConfirmationRequestEvent,
    ConfirmationResponseEvent,
    DispatchEvent,
    DispatchResultEvent,
    Event,
    InboundEvent,
    OutboundEvent,
    StreamChunkEvent,
)
from ant.observability import tracing

TRANSIENT_EVENT_CLASSES: tuple[type[Event], ...] = (
    StreamChunkEvent,
    ConfirmationRequestEvent,
    ConfirmationResponseEvent,
)

PERSISTENT_EVENT_CLASSES: tuple[type[Event], ...] = (
    InboundEvent,
    OutboundEvent,
    DispatchEvent,
    DispatchResultEvent,
)


class CompositeBus:
    """Routes persistent events to a durable bus and transient events locally.

    ``durable`` must satisfy the ``EventBus`` protocol; ``outbox_writer``, when
    given, receives every persistent event instead of the durable bus (the
    writer owns the DB transaction + broker emission).  ``ack``/``nack`` are
    passed through to the durable bus; ``start``/``stop`` drive both buses.
    """

    def __init__(
        self,
        durable: EventBus,
        outbox_writer: Callable[[Event], Awaitable[None]] | None = None,
    ) -> None:
        self._durable = durable
        self._outbox_writer = outbox_writer
        # Transient events never persist, so the internal bus's pending dir is
        # a throwaway temp directory (kept out of the working tree).
        temp_dir = Path(tempfile.mkdtemp(prefix="ant-local-bus-"))
        self._local = InMemoryBus(pending_dir=temp_dir / "pending")

    @staticmethod
    def _is_transient_event(event: Event) -> bool:
        return isinstance(event, TRANSIENT_EVENT_CLASSES)

    @staticmethod
    def _is_transient_class(event_class: type[Event]) -> bool:
        return issubclass(event_class, TRANSIENT_EVENT_CLASSES)

    async def publish(self, event: Event) -> None:
        """Route an event: transient -> local bus; persistent -> outbox/durable.

        Phase 6 tracing：发布前把当前活动 span 的 W3C traceparent 注入事件
        载荷（trace.md §3/§9——异步消息里显式携带 Trace Context），消费端
        据此把 MainAgent → EventBus → SubAgent 串成同一条 Trace。
        """
        if not event.traceparent and tracing.is_enabled():
            event.traceparent = tracing.inject_current_traceparent()
        if self._is_transient_event(event):
            await self._local.publish(event)
            return
        if self._outbox_writer is not None:
            await self._outbox_writer(event)
        else:
            await self._durable.publish(event)

    def subscribe(
        self,
        event_class: type[Event],
        handler: Callable[[Event], Awaitable[None]],
    ) -> None:
        """Route a subscription to the bus that carries that event class."""
        if self._is_transient_class(event_class):
            self._local.subscribe(event_class, handler)
        else:
            self._durable.subscribe(event_class, handler)

    def unsubscribe(self, handler: Callable) -> None:
        """Remove a handler from both the durable and the local bus."""
        self._durable.unsubscribe(handler)
        self._local.unsubscribe(handler)

    async def ack(self, event: Event) -> None:
        """Pass through to the durable bus."""
        await self._durable.ack(event)

    async def nack(self, event: Event, requeue: bool = False) -> None:
        """Pass through to the durable bus."""
        await self._durable.nack(event, requeue=requeue)

    async def start(self) -> None:
        """Start the durable bus first (its failure aborts startup), then local."""
        await self._durable.start()
        await self._local.start()

    async def stop(self) -> None:
        """Stop the local bus first, then the durable bus. Idempotent."""
        await self._local.stop()
        await self._durable.stop()
