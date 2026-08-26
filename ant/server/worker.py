"""Base worker lifecycle management"""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ant.core.context import SharedContext


class Worker(ABC):
    """BAse class for all workers with lifecycle management"""

    def __init__(self, context: "SharedContext"):
        self.context = context
        self.logger = logging.getLogger(f"ant.server.{self.__class__.__name__}")
        self._task: asyncio.Task | None = None

    @abstractmethod
    async def run(self) -> None:
        """Main worker loop, Runs until cancelled"""
        pass

    def start(self) -> asyncio.Task:
        """Start the worker as an asyncio Task"""
        self._task = asyncio.create_task(self.run())
        return self._task

    def is_running(self) -> bool:
        """Check if worker is actively running"""
        return self._task is not None and not self._task.done()

    def has_crashed(self) -> bool:
        """Check if worker crashed(done but not cancelled"""
        return (
            self._task is not None
            and self._task.done()
            and not self._task.cancelled()
        )

    def get_exception(self) -> BaseException | None:
        """Get the exception if worker crashed, None otherwise"""
        if self.has_crashed() and self._task is not None:
            return self._task.exception()
        return None

    async def stop(self, timeout: float = 15.0) -> None:
        """Gracefully stop the worker.

        Shutdown is bounded by ``timeout`` seconds (improve.md "停机无超
        时"): a worker that ignores cancellation can no longer hang the
        server forever — on timeout a warning is logged and stop() returns
        anyway.  Idempotent: safe to call multiple times and a no-op when
        the worker was never started (or already finished).

        A channel updater that blocks inside its own ``stop()`` (e.g. a
        stuck ``updater.stop()``) is caught by this same timeout at the
        ChannelWorker level — the channels themselves need no timers.
        """
        if self._task is None:
            return
        self._task.cancel()
        try:
            await asyncio.wait_for(self._stop_impl(), timeout=timeout)
        except asyncio.TimeoutError:
            self.logger.warning(
                f"{self.__class__.__name__} did not stop within {timeout}s "
                "— returning anyway"
            )

    async def _stop_impl(self) -> None:
        """Wait for the worker task to finish after cancellation.

        Split out from stop() so shutdown can be wrapped in a bounded
        timeout (and so tests can simulate a stuck shutdown).  Exceptions
        raised by a crashed worker are logged, not re-raised, so one
        broken worker cannot abort the whole server shutdown chain.
        """
        if self._task is None:
            return
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception:
            self.logger.warning(
                f"{self.__class__.__name__} raised during shutdown",
                exc_info=True,
            )


class SubscribeWorker(Worker):
    """Worker that only subscribes to events, no active loop"""
    async def run(self) -> None:
        """Wait for cancellation - actual work happens in event handlers"""
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            pass
"""
SubscribeWorker 是一个被动型的“空转”占位工作器。它的核心作用不是“做具体工作”，
而是利用 Worker 基类的生命周期管理能力，来托管那些完全依赖事件回调（Event Handlers）的异步组件。

可以把它理解为：“我本身不干活，但我负责把‘自己还在运行’这个状态占住，让框架能统一监控和停止我。”
"""