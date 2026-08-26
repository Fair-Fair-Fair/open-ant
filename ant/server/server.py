"""Server orchestrator for worker-based architecture."""

import asyncio
import logging
from typing import TYPE_CHECKING

import uvicorn

from ant.utils.config import ConfigReloader

from .agent_worker import AgentWorker
from .app import create_app
from .channel_worker import ChannelWorker

# 12 cron heartbeat
from .cron_worker import CronWorker
from .delivery_worker import DeliveryWorker
from .websocket_worker import WebSocketWorker
from .worker import Worker

if TYPE_CHECKING:
    from ant.core.context import SharedContext

logger = logging.getLogger(__name__)


class Server:
    """Orchestrates workers with queue-based communication."""

    def __init__(self, context: "SharedContext"):
        self.context = context
        self.workers: list[Worker] = []
        self._api_task: asyncio.Task | None = None
        self.config_reloader: ConfigReloader = ConfigReloader(self.context.config)

    async def run(self) -> None:
        """Start all workers and monitor for crashes."""
        # Startup self-healing (must run inside this async context):
        # 1) GC abandoned sandbox Docker volumes (improve.md #7 leak).
        # 2) Auto-ingest configured docs (improve.md #22 — no more
        #    run_until_complete inside SharedContext.__init__).
        await self._cleanup_orphan_volumes()
        await self.context.auto_ingest_docs()

        self._setup_workers()
        self._start_workers()

        # Start API server if configured
        if self.context.config.api:
            self._api_task = asyncio.create_task(self._run_api())

        try:
            await self._monitor_workers()
        except asyncio.CancelledError:
            logger.info("Server shutting down...")
            await self._stop_all()
            raise

    async def _cleanup_orphan_volumes(self) -> None:
        """Best-effort GC for abandoned sandbox Docker volumes.

        Every docker-backend bash session creates a named volume
        ``open-ant-sandbox-<session_id>`` (sandbox.py) that is never removed
        when the session dies — a permanent disk leak.  We deliberately do NOT
        import the docker SDK (not a dependency); instead we shell out to the
        CLI, filter for our prefix, and remove volumes whose session_id no
        longer exists in history.  All failures are silent + logged.
        """
        prefix = "open-ant-sandbox-"
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "volume", "ls", "--format", "{{.Name}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
        except (FileNotFoundError, OSError):
            logger.debug("docker CLI unavailable; skip orphan volume cleanup")
            return
        if proc.returncode != 0:
            logger.debug("docker volume ls failed (rc=%s); skip cleanup", proc.returncode)
            return

        removed = kept = 0
        for name in stdout.decode("utf-8", errors="replace").splitlines():
            name = name.strip()
            if not name.startswith(prefix):
                continue
            session_id = name[len(prefix):]
            if (
                not session_id
                or self.context.history_store.get_session_info(session_id) is not None
            ):
                kept += 1
                continue
            try:
                rm_proc = await asyncio.create_subprocess_exec(
                    "docker", "volume", "rm", name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await rm_proc.wait()
            except Exception:
                logger.debug("Failed to remove orphan volume %s", name, exc_info=True)
                continue
            if rm_proc.returncode == 0:
                removed += 1
                logger.info("Removed orphan Docker volume: %s", name)
            else:
                logger.debug("docker volume rm %s failed (rc=%s)", name, rm_proc.returncode)

        if removed or kept:
            logger.info(
                "Orphan volume cleanup done: %d removed, %d in-use kept",
                removed, kept,
            )

    def _setup_workers(self) -> None:
        """Create all workers."""
        self.config_reloader.start()

        # Create WebSocketWorker first and attach to context
        ws_worker = WebSocketWorker(self.context)
        self.context.websocket_worker = ws_worker

        self.workers = [
            self.context.eventbus,  # EventBus (active worker)
            AgentWorker(self.context),  # SubscriberWorker
            DeliveryWorker(self.context),  # SubscriberWorker，
            CronWorker(self.context),  # Background worker for scheduled tasks
            ws_worker,  # WebSocketWorker (SubscriberWorker)
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

    async def _run_api(self) -> None:
        """Run the WebSocket API server."""
        if not self.context.config.api:
            return

        app = create_app(self.context)
        config = uvicorn.Config(
            app,
            host=self.context.config.api.host,
            port=self.context.config.api.port,
        )
        server = uvicorn.Server(config)
        logger.info(
            f"WebSocket server started on {self.context.config.api.host}:{self.context.config.api.port}"  # noqa: E501
        )
        await server.serve()
