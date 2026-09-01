"""In-process event bus (``InMemoryBus``) — the dev/test backend.

Dispatch semantics are equivalent to the legacy worker bus
(``ant.core.eventbus.EventBus``, which stays untouched):

* ``publish()`` enqueues into an ``asyncio.Queue``; a background consumer task
  dispatches to subscribers.  Publishing *before* ``start()`` is allowed
  (queue first, consume later — the legacy "queue then run" model).
* ``OutboundEvent``s are persisted to ``<pending_dir>/*.json`` (utf-8, atomic
  tmp-file + fsync + rename) at dispatch time, and re-dispatched to
  subscribers on the next ``start()`` (crash recovery).  The file is only
  removed by ``ack(event)``.
* ``ack(event)`` deletes the corresponding pending file; ``nack()`` is a
  no-op (there is nothing to negatively acknowledge in-process).
* handler exceptions are isolated: logged with traceback, remaining handlers
  still run (a failing handler never breaks the bus or its peers).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import Callable

from ant.bus.base import Handler
from ant.core.events import Event, OutboundEvent, deserialize_event
from ant.observability import tracing

logger = logging.getLogger(__name__)


class _Stop:
    """Sentinel queued on ``stop()`` to shut the consumer down gracefully."""


class InMemoryBus:
    """asyncio.Queue-based pub/sub bus with outbound file persistence."""

    def __init__(self, pending_dir: Path | None = None) -> None:
        # Mirrors the legacy default: config.event_path / "pending" (.events/pending).
        self.pending_dir = pending_dir or (Path(".events") / "pending")
        self._queue: asyncio.Queue[Event | _Stop] = asyncio.Queue()
        self._handlers: dict[type[Event], list[Handler]] = defaultdict(list)
        self._consumer: asyncio.Task | None = None
        self._started = False
        self._stopped = False

    # ── pub/sub ──────────────────────────────────────────────────────────

    def subscribe(self, event_class: type[Event], handler: Handler) -> None:
        """Register a handler for an event class (multiple handlers allowed)."""
        self._handlers[event_class].append(handler)

    def unsubscribe(self, handler: Callable) -> None:
        """Remove a handler from every event class it subscribed to."""
        for event_class in list(self._handlers):
            if handler in self._handlers[event_class]:
                self._handlers[event_class].remove(handler)

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Recover pending outbound events, then start the consumer. Idempotent."""
        if self._started:
            return
        self._started = True
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        recovered = await self._recover()
        if recovered:
            logger.info("InMemoryBus recovered %d pending event(s)", recovered)
        self._consumer = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Drain queued events, then stop the consumer. Idempotent."""
        if not self._started:
            return
        self._started = False
        self._stopped = True
        await self._queue.put(_Stop())
        if self._consumer is not None:
            await self._consumer
            self._consumer = None

    async def flush(self) -> None:
        """Wait until every queued event has been dispatched (test helper)."""
        await self._queue.join()

    # ── delivery contract ────────────────────────────────────────────────

    async def publish(self, event: Event) -> None:
        """Enqueue an event for async dispatch (never blocks on handlers)."""
        if self._stopped:
            raise RuntimeError("InMemoryBus is stopped; cannot publish")
        await self._queue.put(event)

    async def ack(self, event: Event) -> None:
        """Acknowledge delivery: delete the persisted pending file for the event."""
        path = self._pending_file(event)
        if path.exists():
            path.unlink()
            logger.debug("Acked and deleted %s", path.name)

    async def nack(self, event: Event, requeue: bool = False) -> None:
        """No-op: in-process delivery needs no negative acknowledgement."""

    # ── internals ────────────────────────────────────────────────────────

    def _pending_file(self, event: Event) -> Path:
        # Same filename scheme as the legacy bus (ack must find it again).
        return self.pending_dir / f"{event.timestamp}_{event.session_id}.json"

    async def _run(self) -> None:
        try:
            while True:
                item = await self._queue.get()
                try:
                    if isinstance(item, _Stop):
                        break
                    await self._dispatch(item)
                except Exception:
                    logger.exception("Unhandled error while dispatching event")
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            logger.info("InMemoryBus consumer cancelled")
            raise

    async def _dispatch(self, event: Event) -> None:
        await self._persist_outbound(event)
        await self._notify(event)

    async def _persist_outbound(self, event: Event) -> None:
        if not isinstance(event, OutboundEvent):
            return
        final_path = self._pending_file(event)
        tmp_path = self.pending_dir / f".tmp.{os.getpid()}.{final_path.name}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_path), str(final_path))
        logger.debug("Persisted outbound event to %s", final_path.name)

    async def _notify(self, event: Event) -> None:
        # Phase 6 tracing：进程内总线同样从事件载荷重建 consume span——
        # 子代理/回传链（MainAgent → DispatchEvent → SubAgent）在单进程
        # 模型下也串成同一条 Trace（rabbitmq.py 同构逻辑）。
        from opentelemetry import trace as otel_trace

        span = tracing.start_consume_span(
            type(event).__name__, getattr(event, "traceparent", None), "memory"
        )
        with otel_trace.use_span(span, end_on_exit=True):
            for handler in list(self._handlers.get(type(event), [])):
                try:
                    await handler(event)
                except Exception:
                    logger.exception("Error in handler for %s", type(event).__name__)

    async def _recover(self) -> int:
        count = 0
        for path in sorted(self.pending_dir.glob("*.json")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    event = deserialize_event(json.load(f))
                await self._notify(event)
                count += 1
                logger.debug("Recovered event from %s", path.name)
            except Exception:
                logger.exception("Failed to recover event from %s", path)
        return count
