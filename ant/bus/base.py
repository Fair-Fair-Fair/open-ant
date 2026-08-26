"""Event bus interface contract (Phase 1, workspace/plan.md §4).

Every bus implementation (in-memory, RabbitMQ, composite) must satisfy this
protocol.  All methods are intentionally async — except ``subscribe`` /
``unsubscribe`` — so a future outbox/worker layer can swap backends without
touching callers.

Semantics shared by every implementation:

* ``publish`` — enqueue/send one event (at-least-once for durable buses).
* ``subscribe`` / ``unsubscribe`` — register / remove a handler for one event
  class (multiple handlers per class are allowed).
* ``ack`` — confirm delivery of an event (delete pending persistence).
* ``nack`` — negative acknowledgement (``requeue=False`` -> DLQ path on the
  broker; no-op on buses that cannot negatively acknowledge).
* ``start`` / ``stop`` — lifecycle; both idempotent.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Protocol, runtime_checkable

from ant.core.events import Event

Handler = Callable[[Event], Awaitable[None]]


@runtime_checkable
class EventBus(Protocol):
    """Minimal pub/sub contract implemented by the bus backends."""

    async def publish(self, event: Event) -> None: ...

    def subscribe(self, event_class: type[Event], handler: Handler) -> None: ...

    def unsubscribe(self, handler: Callable) -> None: ...

    async def ack(self, event: Event) -> None: ...

    async def nack(self, event: Event, requeue: bool = False) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
