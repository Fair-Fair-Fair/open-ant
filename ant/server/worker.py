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
        self.logger = logging.getLogger(f"mybot.server.{self.__class__.__name__}")
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

    async def stop(self) -> None:
        """Gracefully stop the worker"""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


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