"""Outbox unit tests: ``enqueue`` + ``OutboxPublisher`` (SQLite+aiosqlite).

The ``ant.bus`` package (``__init__.py`` / ``base.py``) is being built by
a parallel change, so these tests never import it: ``outbox.py`` is
loaded by file path and the bus is a minimal fake implementing only
``async publish(event)``.

DB: a per-test SQLite file (tmp_path) with ``NullPool`` gives each
session its own connection/transaction — required to demonstrate that
an un-committed outbox row is invisible to other sessions (an in-memory
DB with ``StaticPool`` would share one connection and leak uncommitted
rows).  The schema is created via ``Base.metadata.create_all``.
"""

import asyncio
import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from ant.core.events import CliEventSource, OutboundEvent
from ant.storage.models import Base, OutboxEventRecord
from ant.storage.outbox_ops import enqueue

# Load ant.bus.outbox by file path so these tests stay independent of the
# parallel ant.bus package (its __init__.py / base.py are out of scope).
_OUTBOX_FILE = Path(__file__).resolve().parent.parent / "bus" / "outbox.py"
_spec = importlib.util.spec_from_file_location("ant_bus_outbox_test", _OUTBOX_FILE)
assert _spec is not None and _spec.loader is not None
_outbox_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_outbox_module)
OutboxPublisher = _outbox_module.OutboxPublisher


class FakeBus:
    """Minimal bus protocol: ``async publish(event)``, records calls."""

    def __init__(self, fail_forever: bool = False, fail_times: int = 0) -> None:
        self.fail_forever = fail_forever
        self.fail_times = fail_times
        self.published: list = []
        self.publish_calls = 0

    async def publish(self, event) -> None:
        self.publish_calls += 1
        if self.fail_forever or self.fail_times > 0:
            if not self.fail_forever:
                self.fail_times -= 1
            raise RuntimeError("bus unavailable")
        self.published.append(event)


def make_event(content: str = "hello") -> OutboundEvent:
    return OutboundEvent(session_id="s1", source=CliEventSource(), content=content)


async def _enqueue(session_factory, event: OutboundEvent, message_id: str) -> None:
    async with session_factory() as session:
        enqueue(session, event, message_id)
        await session.commit()


async def _wait_for(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"condition not met within {timeout}s")


@pytest.fixture
async def db(tmp_path):
    """SQLite+aiosqlite file DB; every session uses its own connection."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'outbox.db'}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield SimpleNamespace(engine=engine, session_factory=factory)
    await engine.dispose()


# ── enqueue ─────────────────────────────────────────────────────────────


async def test_enqueue_uncommitted_invisible_then_visible_after_commit(db):
    async with db.session_factory() as session:
        enqueue(session, make_event("hello"), "msg-1")
        await session.flush()  # INSERT issued, transaction still open
        async with db.session_factory() as other:
            rows = (await other.execute(select(OutboxEventRecord))).scalars().all()
            assert rows == []  # separate connection: uncommitted row not visible
        await session.commit()

    async with db.session_factory() as other:
        rows = (await other.execute(select(OutboxEventRecord))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.event_type == "OutboundEvent"
        assert row.message_id == "msg-1"
        assert row.payload["type"] == "OutboundEvent"
        assert row.payload["content"] == "hello"
        assert row.attempts == 0
        assert row.published_at is None


# ── OutboxPublisher ─────────────────────────────────────────────────────


async def test_publisher_publishes_and_marks_published(db):
    await _enqueue(db.session_factory, make_event("one"), "msg-1")
    await _enqueue(db.session_factory, make_event("two"), "msg-2")

    bus = FakeBus()
    publisher = OutboxPublisher(
        db.session_factory, bus, poll_interval=0.01, max_attempts=3
    )
    task = asyncio.create_task(publisher.run())
    try:
        await _wait_for(lambda: len(bus.published) == 2)
        await publisher.stop()
        await asyncio.wait_for(task, 5)
    finally:
        if not task.done():
            task.cancel()

    assert [e.content for e in bus.published] == ["one", "two"]
    async with db.session_factory() as session:
        rows = (
            await session.execute(select(OutboxEventRecord).order_by(OutboxEventRecord.id))
        ).scalars().all()
        assert all(row.published_at is not None for row in rows)


async def test_publish_failure_increments_attempts_then_succeeds(db):
    await _enqueue(db.session_factory, make_event("retry"), "msg-1")

    bus = FakeBus(fail_times=2)  # first two publishes raise, then succeed
    publisher = OutboxPublisher(
        db.session_factory, bus, poll_interval=0.01, max_attempts=5
    )
    task = asyncio.create_task(publisher.run())
    try:
        await _wait_for(lambda: bus.publish_calls == 3)
        await publisher.stop()
        await asyncio.wait_for(task, 5)
    finally:
        if not task.done():
            task.cancel()

    assert len(bus.published) == 1
    async with db.session_factory() as session:
        row = (await session.execute(select(OutboxEventRecord))).scalars().one()
        assert row.attempts == 2
        assert row.published_at is not None


async def test_max_attempts_exhaustion_stops_retries_and_keeps_row(db):
    await _enqueue(db.session_factory, make_event("stuck"), "msg-1")

    bus = FakeBus(fail_forever=True)
    publisher = OutboxPublisher(db.session_factory, bus, poll_interval=0.01, max_attempts=3)
    task = asyncio.create_task(publisher.run())
    try:
        await _wait_for(lambda: bus.publish_calls == 3)
        await asyncio.sleep(0.1)  # a few more polls must not retry it
        await publisher.stop()
        await asyncio.wait_for(task, 5)
    finally:
        if not task.done():
            task.cancel()

    assert bus.publish_calls == 3  # never retried beyond max_attempts
    async with db.session_factory() as session:
        row = (await session.execute(select(OutboxEventRecord))).scalars().one()
        assert row.attempts == 3
        assert row.published_at is None  # row left in place for manual handling


async def test_each_row_published_exactly_once(db):
    for i in range(3):
        await _enqueue(db.session_factory, make_event(f"e{i}"), f"msg-{i}")

    bus = FakeBus()
    publisher = OutboxPublisher(db.session_factory, bus, poll_interval=0.01)
    task = asyncio.create_task(publisher.run())
    try:
        await _wait_for(lambda: len(bus.published) == 3)
        await asyncio.sleep(0.15)  # several more polls must not re-publish
        await publisher.stop()
        await asyncio.wait_for(task, 5)
    finally:
        if not task.done():
            task.cancel()

    assert len(bus.published) == 3
    assert sorted(e.content for e in bus.published) == ["e0", "e1", "e2"]


async def test_stop_is_idempotent(db):
    publisher = OutboxPublisher(db.session_factory, FakeBus())
    await publisher.stop()
    await publisher.stop()
    await publisher.stop()  # no error


async def test_run_exits_immediately_when_already_stopped(db):
    publisher = OutboxPublisher(db.session_factory, FakeBus())
    await publisher.stop()
    await publisher.run()  # returns without publishing anything


async def test_run_exits_on_cancellation(db):
    await _enqueue(db.session_factory, make_event("x"), "msg-x")

    bus = FakeBus(fail_forever=True)
    publisher = OutboxPublisher(
        db.session_factory, bus, poll_interval=60, max_attempts=100
    )
    task = asyncio.create_task(publisher.run())
    await _wait_for(lambda: bus.publish_calls >= 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
