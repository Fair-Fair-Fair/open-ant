"""Server orchestrator for worker-based architecture."""

import asyncio
import logging
from typing import TYPE_CHECKING

import uvicorn

from ant.storage.db import create_database_if_missing, run_migrations
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
    from ant.bus.outbox import OutboxPublisher
    from ant.core.context import SharedContext

logger = logging.getLogger(__name__)


class Server:
    """Orchestrates workers with queue-based communication."""

    def __init__(self, context: "SharedContext"):
        self.context = context
        self.workers: list[Worker] = []
        self._api_task: asyncio.Task | None = None
        self._outbox_publisher: "OutboxPublisher | None" = None
        self._outbox_task: asyncio.Task | None = None
        self.config_reloader: ConfigReloader = ConfigReloader(self.context.config)

    async def run(self) -> None:
        """Start all workers and monitor for crashes."""
        # Startup self-healing (must run inside this async context):
        # 1) GC abandoned sandbox Docker volumes (improve.md #7 leak).
        # 2) Auto-ingest configured docs (improve.md #22 — no more
        #    run_until_complete inside SharedContext.__init__).
        await self._cleanup_orphan_volumes()
        await self.context.auto_ingest_docs()

        # Phase 1 startup order:
        # (a) mysql backend → CREATE DATABASE + alembic migrations.
        await self._bootstrap_storage()

        # (b) event bus — a failed connect aborts startup with a clear error.
        await self._start_bus()

        try:
            # (c) outbox publisher (only when durable events go via the outbox).
            self._start_outbox_publisher()

            # (d) existing workers
            self._setup_workers()
            self._start_workers()

            # Start API server if configured
            if self.context.config.api:
                self._api_task = asyncio.create_task(self._run_api())

            await self._monitor_workers()
        except asyncio.CancelledError:
            logger.info("Server shutting down...")
            raise
        finally:
            await self._stop_all()

    async def _bootstrap_storage(self) -> None:
        """(a) CREATE DATABASE IF NOT EXISTS + alembic upgrade head.

        Only for ``config.storage.backend == "mysql"``; when credentials are
        missing the history already fell back to JSONL, so there is nothing
        to bootstrap.
        """
        if self.context.config.storage.backend != "mysql":
            return
        dsn = self.context._mysql_dsn
        if dsn is None:
            logger.warning(
                "storage.backend=mysql but MySQL credentials are missing — "
                "history already fell back to JSONL; skipping DB bootstrap"
            )
            return
        await create_database_if_missing(dsn)
        await run_migrations(dsn)
        logger.info("MySQL bootstrap complete (database + migrations)")

    async def _start_bus(self) -> None:
        """(b) Start the event bus; a connect failure aborts startup."""
        try:
            await self.context.eventbus.start()
        except Exception as exc:
            logger.error("EventBus failed to start: %s", exc)
            await self._try_stop_bus()
            raise RuntimeError(
                f"EventBus ({self.context.bus_backend} backend) failed to "
                f"start: {exc}"
            ) from exc
        logger.info("EventBus started (backend=%s)", self.context.bus_backend)

    async def _try_stop_bus(self) -> None:
        try:
            await self.context.eventbus.stop()
        except Exception:
            logger.debug("EventBus stop raised", exc_info=True)

    def _start_outbox_publisher(self) -> None:
        """(c) Start OutboxPublisher when durable events go via the outbox.

        The publisher drains ``outbox_events`` into the durable bus
        (never the CompositeBus itself — that would re-enter the outbox).
        """
        if self.context.outbox_writer is None:
            return
        from ant.bus.outbox import OutboxPublisher

        if self.context._session_factory is None:
            logger.error(
                "Outbox mode requires a MySQL session factory — "
                "skipping OutboxPublisher; events stay in the outbox table"
            )
            return
        self._outbox_publisher = OutboxPublisher(
            session_factory=self.context._session_factory,
            bus=self.context._durable_bus,
        )
        self._outbox_task = asyncio.create_task(self._outbox_publisher.run())
        logger.info("OutboxPublisher started")

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
                or await self.context.history_store.get_session_info(session_id)
                is not None
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

        # NOTE: the CompositeBus (self.context.eventbus) is NOT a Worker —
        # it is started/stopped explicitly in run()/_stop_all.
        self.workers = [
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
        """Stop everything in reverse startup order.

        Phase 1 order (plan.md §3.7 graceful shutdown):
        outbox publisher → workers → event bus → uvicorn API task → config
        reloader.  The API task was never stopped in Phase 0 — cancelled
        here (uvicorn's serve() exits cleanly on task cancellation).
        """
        # (1) outbox publisher first — stop new events entering the bus
        if self._outbox_publisher is not None:
            try:
                await self._outbox_publisher.stop()
            except Exception:
                logger.error("OutboxPublisher stop failed", exc_info=True)
        if self._outbox_task is not None:
            self._outbox_task.cancel()
            try:
                await self._outbox_task
            except asyncio.CancelledError:
                pass

        # (2) workers
        for worker in self.workers:
            await worker.stop()

        # (3) event bus
        await self._try_stop_bus()

        # (4) uvicorn API task (Phase 0 leftover — never stopped before)
        await self._stop_api_task()

        # (5) config reloader
        if self.config_reloader is not None:
            self.config_reloader.stop()

    async def _stop_api_task(self) -> None:
        """Cancel the uvicorn API task (Phase 0 leftover)."""
        if self._api_task is None:
            return
        self._api_task.cancel()
        try:
            await self._api_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.error("API task stop failed", exc_info=True)

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
