"""Server orchestrator for worker-based architecture."""

import asyncio
import logging
from typing import TYPE_CHECKING

from .worker import Worker
from .agent_worker import AgentWorker
from .delivery_worker import DeliveryWorker
from .channel_worker import ChannelWorker
from ant.utils.config import ConfigReloader

if TYPE_CHECKING:
    from ant.core.context import SharedContext

logger = logging.getLogger(__name__)


class Server:
    """Orchestrates workers with queue-based communication."""

    def __init__(self, context: "SharedContext"):
        self.context = context
        self.workers: list[Worker] = []
        self.config_reloader: ConfigReloader = ConfigReloader(self.context.config)

    async def run(self) -> None:
        """Start all workers and monitor for crashes."""
        self._setup_workers()
        self._start_workers()

        try:
            await self._monitor_workers()
        except asyncio.CancelledError:
            logger.info("Server shutting down...")
            await self._stop_all()
            raise

    def _setup_workers(self) -> None:
        """Create all workers."""
        self.config_reloader.start()

        self.workers = [
            self.context.eventbus,  # EventBus (active worker)
            AgentWorker(self.context),  # SubscriberWorker
            DeliveryWorker(self.context),  # SubscriberWorker
        ]

        if self.context.config.channels.enabled:
            channels = self.context.channels
            if channels:
                self.workers.append(ChannelWorker(self.context))
                logger.info(f"Channel enabled with {len(channels)} channel(es)")
            else:
                logger.warning("Channel enabled but no channels configured")

        logger.info(f"Server setup complete with {len(self.workers)} core workers")

    def _start_workers(self) -> None:
        """Start all workers as tasks."""
        for worker in self.workers:
            worker.start()
            logger.info(f"Started {worker.__class__.__name__}")

    async def _monitor_workers(self) -> None:
        """Monitor worker tasks, restart on crash."""
        while True:
            for worker in self.workers:
                if worker.has_crashed():
                    exc = worker.get_exception()
                    if exc is None:
                        logger.warning(
                            f"{worker.__class__.__name__} exited unexpectedly"
                        )
                    else:
                        logger.error(f"{worker.__class__.__name__} crashed: {exc}")

                    worker.start()
                    logger.info(f"Restarted {worker.__class__.__name__}")

            await asyncio.sleep(5)

    async def _stop_all(self) -> None:
        """Stop all workers gracefully."""
        for worker in self.workers:
            await worker.stop()

        # Stop config reloader
        if self.config_reloader is not None:
            self.config_reloader.stop()
