"""Channel hardening tests — Phase 4D (improve.md #15 crash-loop / #19 PII).

Covers:
- Telegram/Discord channel re-entrancy: a crashed/stopped run must never
  leave the channel stuck in "already running" (crash-loop fix).
- PII log masking: full user message text never reaches the logs.
- Worker.stop() bounded shutdown timeout (no infinite hang).
- Server monitor exponential restart backoff (5s → 10s → … → 120s cap)
  with crash-count reset after 5 stable minutes.

Hermetic: fake ``telegram`` / ``discord`` SDKs are injected into
``sys.modules`` before importing the channel modules, so the tests need
neither the SDKs installed nor any network.
"""
import asyncio
import importlib
import logging
import sys
import time
import types
from unittest.mock import AsyncMock

import pytest

from ant.channel.base import _mask_pii
from ant.server.channel_worker import ChannelWorker
from ant.server.server import Server, _next_backoff
from ant.server.worker import Worker

# ─────────────────────────── fake SDKs ───────────────────────────


class _FakeTelegramUpdater:
    """Duck-typed python-telegram-bot updater.

    ``default_running = False`` simulates a dead updater (the crashed-run
    seed for the #15 crash-loop); ``stop()`` flips the flag.
    """

    default_running = True

    def __init__(self):
        self.running = type(self).default_running
        self.stopped = False

    async def start_polling(self):
        pass

    async def stop(self):
        self.running = False
        self.stopped = True


class _FakeTelegramBuilder:
    """``Application.builder().token(...).build()`` chain."""

    def __init__(self):
        self.token_value = None

    def token(self, token):
        self.token_value = token
        return self

    def build(self):
        return FakeTelegramApp()


class FakeTelegramApp:
    """Duck-typed python-telegram-bot ``Application``."""

    instances = []

    @classmethod
    def builder(cls):
        return _FakeTelegramBuilder()

    def __init__(self):
        self.handlers = []
        self.updater = _FakeTelegramUpdater()
        self.bot = self
        self.sent = []
        self.initialize_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.shutdown_calls = 0
        type(self).instances.append(self)

    def add_handler(self, handler):
        self.handlers.append(handler)

    async def initialize(self):
        self.initialize_calls += 1

    async def start(self):
        self.start_calls += 1

    async def stop(self):
        self.stop_calls += 1

    async def shutdown(self):
        self.shutdown_calls += 1

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class _FakeTelegramMessageHandler:
    """Duck-typed ``MessageHandler`` that stores the callback."""

    def __init__(self, filters, callback):
        self.filters = filters
        self.callback = callback


class _FakeTelegramUser:
    def __init__(self, user_id):
        self.id = user_id


class _FakeTelegramChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeTelegramMessage:
    def __init__(self, text, from_user):
        self.text = text
        self.from_user = from_user


class _FakeTelegramUpdate:
    def __init__(self, text, user_id, chat_id):
        self.message = _FakeTelegramMessage(text, _FakeTelegramUser(user_id))
        self.effective_chat = _FakeTelegramChat(chat_id)


class _FakeDiscordIntents:
    def __init__(self):
        self.message_content = False
        self.messages = False

    @classmethod
    def default(cls):
        return cls()


class _FakeDiscordAuthor:
    def __init__(self, user_id):
        self.id = user_id


class _FakeDiscordChannel:
    def __init__(self, channel_id):
        self.id = channel_id


class _FakeDiscordMessage:
    def __init__(self, author, channel, content):
        self.author = author
        self.channel = channel
        self.content = content


class FakeDiscordClient:
    """Duck-typed ``discord.Client``.

    ``behavior``: "block" keeps ``start()`` pending until ``close()``;
    "raise" makes ``start()`` fail immediately (crashed run).
    """

    behavior = "block"
    instances = []

    def __init__(self, intents=None):
        self.intents = intents
        self.closed = False
        self.user = _FakeDiscordAuthor(999)  # the bot's own user
        self._handlers = {}
        self._start_future = None
        type(self).instances.append(self)

    def event(self, fn):
        self._handlers[fn.__name__] = fn
        return fn

    async def start(self, token):
        if type(self).behavior == "raise":
            raise RuntimeError("fake network error")
        self._start_future = asyncio.Future()
        await self._start_future

    async def close(self):
        self.closed = True
        if self._start_future is not None and not self._start_future.done():
            self._start_future.set_result(None)

    def get_channel(self, channel_id):
        return None


def _install_fake_telegram_sdk(monkeypatch) -> None:
    """Inject a fake python-telegram-bot and (re)load the channel module."""
    telegram = types.ModuleType("telegram")
    telegram.Update = object
    ext = types.ModuleType("telegram.ext")
    ext.Application = FakeTelegramApp
    ext.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=None)
    ext.MessageHandler = _FakeTelegramMessageHandler
    ext.filters = types.SimpleNamespace(TEXT="text")
    monkeypatch.setitem(sys.modules, "telegram", telegram)
    monkeypatch.setitem(sys.modules, "telegram.ext", ext)
    mod = importlib.import_module("ant.channel.telegram_channel")
    importlib.reload(mod)


def _install_fake_discord_sdk(monkeypatch) -> None:
    """Inject a fake discord.py and (re)load the channel module."""
    discord = types.ModuleType("discord")
    discord.Client = FakeDiscordClient
    discord.Intents = _FakeDiscordIntents
    discord.Message = _FakeDiscordMessage
    monkeypatch.setitem(sys.modules, "discord", discord)
    mod = importlib.import_module("ant.channel.discord_channel")
    importlib.reload(mod)


class _FakeTelegramConfig:
    bot_token = "fake-token"
    allowed_user_ids = []


class _FakeDiscordConfig:
    bot_token = "fake-token"
    channel_id = None
    allowed_user_ids = []


@pytest.fixture
def instant_sleep(monkeypatch):
    """Replace ``asyncio.sleep`` with an instant no-op that still yields
    to the event loop once, so channel tests don't wait a real second per
    monitor iteration."""
    real_sleep = asyncio.sleep

    async def _instant(seconds):
        await real_sleep(0)

    monkeypatch.setattr("asyncio.sleep", _instant)
    return real_sleep


@pytest.fixture(autouse=True)
def _isolate_fakes():
    """Reset fake-SDK class state between tests and drop the (possibly
    fake-SDK-bound) channel modules from sys.modules afterwards, so later
    imports in the same process see the real SDKs again (or fail
    gracefully when they are not installed)."""
    FakeTelegramApp.instances.clear()
    FakeDiscordClient.instances.clear()
    FakeDiscordClient.behavior = "block"
    _FakeTelegramUpdater.default_running = True
    yield
    sys.modules.pop("ant.channel.telegram_channel", None)
    sys.modules.pop("ant.channel.discord_channel", None)


async def _tick(times: int = 3) -> None:
    """Yield to the event loop a few times (works with/without the
    instant_sleep fixture)."""
    for _ in range(times):
        await asyncio.sleep(0)


async def _noop() -> None:
    return None


# ─────────────────────────── _mask_pii (#19) ───────────────────────────


def test_mask_pii_truncates_long_messages():
    long_text = "A" * 100
    assert _mask_pii(long_text) == "A" * 50 + "…(len=100)"


def test_mask_pii_keeps_short_messages_and_reports_length():
    assert _mask_pii("hello") == "hello"
    assert _mask_pii("B" * 50) == "B" * 50  # exactly at the limit
    assert _mask_pii("x" * 51) == "x" * 50 + "…(len=51)"


def test_mask_pii_never_contains_full_long_text():
    secret = "api-key-" + "s" * 80
    masked = _mask_pii(secret)
    assert secret not in masked
    assert masked.startswith(secret[:50])


# ─────────────────────── Telegram channel state ───────────────────────


def _make_telegram_channel(monkeypatch):
    _install_fake_telegram_sdk(monkeypatch)
    from ant.channel.telegram_channel import TelegramChannel

    return TelegramChannel(_FakeTelegramConfig())


async def test_telegram_restart_after_crash(monkeypatch, instant_sleep):
    """#15 regression: a run() that crashed (dead updater) used to leave
    stale state behind so the restart raised "already running" forever.
    Now the restart must clean up and rebuild."""
    chan = _make_telegram_channel(monkeypatch)
    callback = AsyncMock()

    _FakeTelegramUpdater.default_running = False
    with pytest.raises(RuntimeError):
        await chan.run(callback)

    # restart must work — no "already running" crash loop
    _FakeTelegramUpdater.default_running = True
    run_task = asyncio.create_task(chan.run(callback))
    await _tick()
    assert not run_task.done()
    await chan.stop()
    await run_task
    assert chan.application is None
    assert chan._running_task is None
    assert chan._stop_event is None


async def test_telegram_stale_state_cleaned_on_start(monkeypatch, instant_sleep):
    """start() must reset leftover state (finished task + stale app) before
    rebuilding instead of raising "already running"."""
    chan = _make_telegram_channel(monkeypatch)
    callback = AsyncMock()

    stale_app = FakeTelegramApp()
    done_task = asyncio.create_task(_noop())
    await done_task
    chan.application = stale_app
    chan._running_task = done_task
    chan._stop_event = asyncio.Event()

    run_task = asyncio.create_task(chan.run(callback))
    await _tick()
    assert not run_task.done()
    assert chan.application is not stale_app  # rebuilt, not reused
    await chan.stop()
    await run_task
    assert chan.application is None
    assert chan._running_task is None
    assert chan._stop_event is None


async def test_telegram_stop_idempotent_and_reentrant(monkeypatch, instant_sleep):
    """stop() is idempotent and a stopped channel can run() again."""
    chan = _make_telegram_channel(monkeypatch)
    callback = AsyncMock()

    run_task = asyncio.create_task(chan.run(callback))
    await _tick()
    assert not run_task.done()

    app = chan.application
    await chan.stop()  # graceful stop
    await chan.stop()  # idempotent
    await chan.stop()  # repeated stops are no-ops
    await run_task
    assert app.shutdown_calls == 1
    assert chan.application is None
    assert chan._running_task is None
    assert chan._stop_event is None

    # restart after stop works (re-entrant)
    run_task = asyncio.create_task(chan.run(callback))
    await _tick()
    assert not run_task.done()
    await chan.stop()
    await run_task
    assert chan.application is None


async def test_telegram_handler_logs_masked_pii(monkeypatch, instant_sleep, caplog):
    """#19: the incoming-message log line must never contain the full
    message text — only the masked form.  The agent callback still gets
    the complete message."""
    chan = _make_telegram_channel(monkeypatch)
    callback = AsyncMock()
    caplog.set_level(logging.INFO)

    run_task = asyncio.create_task(chan.run(callback))
    await _tick()

    secret = "api-key-" + "t" * 100
    handler = chan.application.handlers[0].callback
    await handler(_FakeTelegramUpdate(text=secret, user_id=7, chat_id=-42), None)

    masked = _mask_pii(secret)
    assert masked in caplog.text
    assert secret not in caplog.text  # full content never logged
    # masking is log-only — the pipeline still receives the full text
    callback.assert_awaited_once()
    assert callback.await_args.args[0] == secret

    await chan.stop()
    await run_task


# ─────────────────────── Discord channel state ───────────────────────


def _make_discord_channel(monkeypatch):
    _install_fake_discord_sdk(monkeypatch)
    from ant.channel.discord_channel import DiscordChannel

    return DiscordChannel(_FakeDiscordConfig())


async def test_discord_restart_after_crash(monkeypatch, instant_sleep):
    """#15 regression: a run() whose start task failed used to leave the
    channel stuck in "already running" — restart must rebuild."""
    chan = _make_discord_channel(monkeypatch)
    callback = AsyncMock()

    FakeDiscordClient.behavior = "raise"
    with pytest.raises(RuntimeError):
        await chan.run(callback)

    FakeDiscordClient.behavior = "block"
    run_task = asyncio.create_task(chan.run(callback))
    await _tick()
    assert not run_task.done()
    await chan.stop()
    await run_task
    assert chan.client is None
    assert chan._running_task is None


async def test_discord_stale_state_cleaned_on_start(monkeypatch, instant_sleep):
    """start() must reset leftover state (finished start task + stale
    client) before rebuilding instead of raising "already running"."""
    chan = _make_discord_channel(monkeypatch)
    callback = AsyncMock()

    stale_client = FakeDiscordClient()
    done_task = asyncio.create_task(_noop())
    await done_task
    chan.client = stale_client
    chan._running_task = done_task

    run_task = asyncio.create_task(chan.run(callback))
    await _tick()
    assert not run_task.done()
    assert chan.client is not stale_client  # rebuilt, not reused
    await chan.stop()
    await run_task
    assert chan.client is None
    assert chan._running_task is None


async def test_discord_stop_idempotent_and_reentrant(monkeypatch, instant_sleep):
    """stop() is idempotent and a stopped channel can run() again."""
    chan = _make_discord_channel(monkeypatch)
    callback = AsyncMock()

    run_task = asyncio.create_task(chan.run(callback))
    await _tick()
    assert not run_task.done()

    await chan.stop()  # graceful stop (close() resolves the start task)
    await chan.stop()  # idempotent
    await run_task
    assert chan.client is None
    assert chan._running_task is None

    run_task = asyncio.create_task(chan.run(callback))
    await _tick()
    assert not run_task.done()
    await chan.stop()
    await run_task
    assert chan.client is None


async def test_discord_handler_logs_masked_pii(monkeypatch, instant_sleep, caplog):
    """#19: the incoming-message log line must never contain the full
    message text — only the masked form.  The agent callback still gets
    the complete message."""
    chan = _make_discord_channel(monkeypatch)
    callback = AsyncMock()
    caplog.set_level(logging.INFO)

    run_task = asyncio.create_task(chan.run(callback))
    await _tick()

    secret = "discord-token-" + "d" * 100
    handler = chan.client._handlers["_on_discord_message"]
    message = _FakeDiscordMessage(
        author=_FakeDiscordAuthor(7), channel=_FakeDiscordChannel(42), content=secret
    )
    await handler(message)

    masked = _mask_pii(secret)
    assert masked in caplog.text
    assert secret not in caplog.text  # full content never logged
    callback.assert_awaited_once()
    assert callback.await_args.args[0] == secret

    await chan.stop()
    await run_task


# ─────────────────────── Worker.stop() timeout ───────────────────────


class _StuckWorker(Worker):
    """run() dies on cancellation but ``_stop_impl`` hangs — the exact
    "stop without timeout" hang scenario (improve.md)."""

    def __init__(self):
        super().__init__(context=None)
        self._gate = None

    async def run(self):
        self._gate = asyncio.Future()
        try:
            await self._gate
        except asyncio.CancelledError:
            pass  # swallow cancellation, then return

    async def _stop_impl(self):
        await asyncio.sleep(60)  # simulated blocked shutdown


async def test_worker_stop_times_out_and_returns(caplog):
    """A stuck shutdown must not hang stop() forever — wait_for bounds it,
    a warning is logged and stop() returns."""
    worker = _StuckWorker()
    worker.start()
    with caplog.at_level(logging.WARNING, logger="ant.server._StuckWorker"):
        await worker.stop(timeout=0.05)
    assert "did not stop within 0.05s" in caplog.text
    assert not worker.is_running()


class _WellBehavedWorker(Worker):
    async def run(self):
        await asyncio.Future()  # runs until cancelled


async def test_worker_stop_normal_and_idempotent():
    worker = _WellBehavedWorker(types.SimpleNamespace())
    task = worker.start()
    await _tick()
    assert worker.is_running()

    await worker.stop()
    await worker.stop()  # idempotent
    await worker.stop()
    assert not worker.is_running()
    assert task.cancelled()


class _CrashWorker(Worker):
    async def run(self):
        raise RuntimeError("boom")


async def test_worker_stop_with_crashed_task_does_not_raise(caplog):
    """stop() on an already-crashed worker must not raise — the exception
    is logged and shutdown proceeds (protects the _stop_all chain)."""
    worker = _CrashWorker(types.SimpleNamespace())
    worker.start()
    await _tick()
    assert worker.has_crashed()

    with caplog.at_level(logging.WARNING, logger="ant.server._CrashWorker"):
        await worker.stop()
    assert "raised during shutdown" in caplog.text


# ──────────────────── monitor backoff (server.py) ────────────────────


def test_next_backoff_sequence_and_cap():
    assert _next_backoff(1) == 5.0
    assert _next_backoff(2) == 10.0
    assert _next_backoff(3) == 20.0
    assert _next_backoff(4) == 40.0
    assert _next_backoff(5) == 80.0
    assert _next_backoff(6) == 120.0  # 5*32=160 → capped
    assert _next_backoff(10) == 120.0
    assert _next_backoff(0) == 5.0  # clamped


class _ScriptedWorker(Worker):
    """run() crashes on the first invocation, then blocks on a gate the
    test releases to simulate the next crash (run ends → restarted).
    ``starts`` records only restarts (not the initial test-driven start)."""

    def __init__(self):
        super().__init__(context=None)
        self.run_count = 0
        self.starts = []
        self.gate = None

    async def run(self):
        self.run_count += 1
        if self.run_count == 1:
            raise RuntimeError("fake crash")
        self.gate = asyncio.Future()
        await self.gate

    def start(self):
        if self.run_count > 0:  # skip the initial start, keep restarts
            self.starts.append(time.monotonic())
        return super().start()


async def test_monitor_workers_backoff_sequence_reset_and_error_counts(
    monkeypatch, caplog
):
    """_monitor_workers must restart crash-looping workers with the
    exponential backoff (5→10→20→40→80→120s cap), log every restart at
    ERROR with the consecutive crash count, and reset the counter after
    5 stable minutes (next crash restarts with backoff 5 again)."""
    clock = {"t": 0.0}
    real_sleep = asyncio.sleep
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])

    async def _fake_sleep(seconds):
        clock["t"] += seconds
        await real_sleep(0)

    monkeypatch.setattr("asyncio.sleep", _fake_sleep)
    caplog.set_level(logging.INFO)

    server = Server.__new__(Server)
    worker = _ScriptedWorker()
    server.workers = [worker]
    worker.start()  # run #1 crashes immediately — the first restart seed
    monitor = asyncio.create_task(server._monitor_workers())

    async def _release_gate_and_wait(expected_starts):
        # make the current run end, then let the monitor detect + restart
        for _ in range(10000):
            if worker.gate is not None and not worker.gate.done():
                worker.gate.set_result(None)
                break
            await real_sleep(0)
        else:
            raise AssertionError("worker never reached its gate")
        for _ in range(10000):
            if len(worker.starts) >= expected_starts:
                return
            await real_sleep(0)
        raise AssertionError("worker never restarted")

    try:
        # first crash detected immediately → restart at t=0
        for _ in range(10000):
            if len(worker.starts) >= 1:
                break
            await real_sleep(0)
        assert worker.starts == [0.0]

        # exponential backoff: restarts at 0, 5, 15, 35, 75, 155, 275
        await _release_gate_and_wait(2)
        await _release_gate_and_wait(3)
        await _release_gate_and_wait(4)
        await _release_gate_and_wait(5)
        await _release_gate_and_wait(6)
        await _release_gate_and_wait(7)
        assert worker.starts[:7] == [0.0, 5.0, 15.0, 35.0, 75.0, 155.0, 275.0]
        gaps = [b - a for a, b in zip(worker.starts, worker.starts[1:])]
        assert gaps[:6] == [5.0, 10.0, 20.0, 40.0, 80.0, 120.0]

        # every restart is logged at ERROR with the crash count
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any(e.endswith("(crash #7)") for e in errors)
        assert "Restarted _ScriptedWorker (crash #6, next backoff 120s)" in caplog.text

        # 5 stable minutes → crash counter reset
        for _ in range(10000):
            if "crash counter reset" in caplog.text:
                break
            await real_sleep(0)
        assert "crash counter reset" in caplog.text

        # a crash after the reset starts the backoff from 5s again
        await _release_gate_and_wait(8)
        await _release_gate_and_wait(9)
        gaps = [b - a for a, b in zip(worker.starts, worker.starts[1:])]
        assert gaps[-1] == 5.0
        # post-reset restart is crash #1 again (counter was reset)
        assert caplog.text.count("(crash #1)") == 2
    finally:
        monitor.cancel()
        with pytest.raises(asyncio.CancelledError):
            await monitor
        if worker._task is not None:  # leave no pending task behind
            worker._task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker._task


# ─────────────────── ChannelWorker stop order ───────────────────


async def test_channel_worker_stops_channels_on_cancel():
    """ChannelWorker: cancelling the run task must stop every channel
    (order: cancel gather → channel.stop() → re-raise)."""
    stopped = []

    class _FakeChannel:
        platform_name = "fake"

        def __init__(self):
            self._gate = None

        async def run(self, on_message):
            self._gate = asyncio.Future()
            await self._gate

        async def stop(self):
            stopped.append(self)

    ctx = types.SimpleNamespace(channels=[_FakeChannel()])
    worker = ChannelWorker(ctx)
    task = worker.start()
    await _tick()
    assert not task.done()

    await worker.stop()  # bounded by Worker.stop(timeout)
    assert stopped == ctx.channels
    assert not worker.is_running()
