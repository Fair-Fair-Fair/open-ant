"""Event bus package (Phase 1): protocol + in-memory / RabbitMQ / composite.

Usage::

    from ant.bus import CompositeBus, InMemoryBus, RabbitMqBus

* ``EventBus``   — the Protocol contract (base.py).
* ``InMemoryBus``  — asyncio.Queue + outbound file persistence (dev/test).
* ``RabbitMqBus``  — durable topic-exchange bus with DLX retry ladder + DLQ.
* ``CompositeBus`` — routes persistent events to the durable bus / outbox
  writer and transient events to an internal in-process bus.
"""

from ant.bus.base import EventBus, Handler
from ant.bus.composite import CompositeBus
from ant.bus.memory import InMemoryBus
from ant.bus.rabbitmq import RabbitMqBus

__all__ = ["EventBus", "Handler", "InMemoryBus", "RabbitMqBus", "CompositeBus"]
