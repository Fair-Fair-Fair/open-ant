import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ant.bus.base import EventBus as EventBusProtocol
from ant.bus.composite import CompositeBus
from ant.bus.memory import InMemoryBus
from ant.bus.rabbitmq import RabbitMqBus
from ant.channel.base import Channel
from ant.core.agent_loader import AgentLoader
from ant.core.commands.registry import CommandRegistry
from ant.core.events import Event
from ant.core.skill_loader import SkillLoader
from ant.utils.config import Config
from ant.utils.settings import InfraSettings

if TYPE_CHECKING:
    from ant.server.websocket_worker import WebSocketWorker
    from ant.storage.repository import HistoryRepository

from ant.core.confirmation import ConfirmationBroker

# 12-cron-heartbeat
from ant.core.cron_loader import CronLoader
from ant.core.guardrails import Guardrails

# 16-rag-memory
from ant.core.memory_guard import MemoryGuard
from ant.core.memory_retriever import MemoryRetriever

# 13 multi-layer-prompt
from ant.core.prompt_builder import PromptBuilder
from ant.core.routing import RoutingTable
from ant.core.sandbox import Sandbox
from ant.provider.memory.base import EmbeddingProvider, VectorStore

# 17-rag-document-ingestion
from ant.provider.memory.doc_ingester import DocumentIngester

logger = logging.getLogger(__name__)


def _create_history_store(config: Config) -> "HistoryRepository":
    """Factory for the history repository (Phase 1).

    Decision rule:
      * ``config.storage.backend == "mysql"`` AND complete MySQL
        credentials in ``.env`` (``InfraSettings().mysql_dsn()``) →
        ``MysqlHistoryRepository`` (production default).
      * otherwise → ``JsonlHistoryRepository`` over the legacy JSONL
        files, with an explicit WARNING explaining the fallback.

    The decision is synchronous and never touches the network; the MySQL
    engine connects lazily on first use.
    """
    from ant.storage.repository import JsonlHistoryRepository, MysqlHistoryRepository
    from ant.utils.settings import InfraSettings

    if config.storage.backend == "mysql":
        infra = InfraSettings()
        dsn = infra.mysql_dsn()
        if dsn is not None:
            logger.info(
                "<context>:history_store backend=mysql (host=%s db=%s)",
                infra.mysql_host,
                infra.mysql_database,
            )
            return MysqlHistoryRepository(dsn)
        logger.warning(
            "<context>:history_store backend=mysql but MySQL credentials are "
            "missing/incomplete in .env — falling back to JSONL history at %s",
            config.history_path,
        )
        return JsonlHistoryRepository(config.history_path)

    logger.info(
        "<context>:history_store backend=jsonl (config.storage.backend=%r)",
        config.storage.backend,
    )
    return JsonlHistoryRepository(config.history_path)


class SharedContext:
    """Global shared state for the application"""

    config: Config
    agent_loader: AgentLoader
    skill_loader: SkillLoader
    command_registry: CommandRegistry
    history_store: "HistoryRepository"
    eventbus: EventBusProtocol
    # Phase 1 bus assembly: effective backend ("rabbitmq" | "memory"),
    # durable transport, MySQL session factory / DSN (None when not mysql),
    # and the outbox writer closure (None when not mysql-backed).
    bus_backend: str
    outbox_writer: Callable[["Event"], Awaitable[str]] | None
    channels: list[Channel[Any]]
    websocket_worker: 'WebSocketWorker | None'

    # 12 cron-heartbeat
    cron_loader: CronLoader

    # 11 multi-agent-routing
    routing_table: RoutingTable

    # 13 multi-layer-prompt
    prompt_builder: PromptBuilder

    # 16 rag-memory
    memory_guard: MemoryGuard | None
    memory_retriever: MemoryRetriever | None
    embedding_provider: EmbeddingProvider | None
    vector_store: VectorStore | None

    # 3C: Neo4j memory graph (optional; None when disabled or when
    # credentials are missing — retrieval degrades to vector-only).
    graph: Any | None

    # 17 rag-document-ingestion
    doc_ingester: 'DocumentIngester | None'

    # harness: input/output guardrails
    guardrails: Guardrails

    # harness: human-in-the-loop confirmation
    confirmation_broker: ConfirmationBroker

    def __init__(self, config: Config,
                 channels: list[Channel[Any]] | None = None) -> None:
        self.config = config
        self.history_store = _create_history_store(config)
        self.agent_loader = AgentLoader.from_config(config)
        self.skill_loader = SkillLoader.from_config(config)
        self.command_registry = CommandRegistry.with_builtins()

        # ── Phase 1: event bus assembly ────────────────────────────────
        # CompositeBus: durable events (Inbound/Outbound/Dispatch/
        # DispatchResult) go through RabbitMqBus or the MySQL outbox;
        # transient events (StreamChunk/Confirmation*) always stay in the
        # in-process InMemoryBus (design principle: streaming tokens never
        # cross the broker — workspace/plan.md §1).
        self._durable_bus: EventBusProtocol | None = None
        self._session_factory: Any = None
        self._mysql_dsn: str | None = None
        self._infra_settings: InfraSettings | None = None
        self.outbox_writer: Callable[["Event"], Awaitable[str]] | None = None
        self.bus_backend = "memory"
        self.eventbus = self._assemble_bus(config)

        if channels is not None:
            self.channels = channels
        else:
            self.channels = Channel.from_config(config)

        self.websocket_worker = None

        self.routing_table = RoutingTable(self)

        self.cron_loader = CronLoader.from_config(config)
        self.prompt_builder = PromptBuilder(self)

        # harness: security sandbox
        self.sandbox = Sandbox(config.sandbox, config.workspace)

        # harness: input/output guardrails
        self.guardrails = Guardrails(config.guardrails)

        # harness: human-in-the-loop confirmation
        self.confirmation_broker = ConfirmationBroker()

        # 16 rag-memory
        self._init_memory(config)

        # Phase 6: OpenTelemetry tracing 初始化（幂等；禁用时零开销）
        from ant.observability import tracing

        tracing.init_tracing(config)

    # ── Phase 1: event bus assembly ─────────────────────────────────────

    def _assemble_bus(self, config: Config) -> EventBusProtocol:
        """Build the CompositeBus for this process.

        Decision rules (mirror of ``_create_history_store``):
          * ``config.bus.backend == "rabbitmq"`` AND complete RabbitMQ
            credentials in ``.env`` (``InfraSettings().rabbitmq_url()``) →
            ``RabbitMqBus`` as the durable transport.
          * otherwise → ``InMemoryBus`` + a WARNING explaining the fallback.
          * mysql storage with a MySQL-backed history store → durable
            events are written through the outbox writer instead.

        Credentials discipline: the RabbitMQ URL embeds the password —
        never log it; ``InfraSettings.masked_rabbitmq_url()`` exists for
        user-visible output.
        """
        settings = InfraSettings()
        self._infra_settings = settings

        # Preserve the legacy pending-file location for the memory backend
        # (crash recovery reads the same directory as the old EventBus).
        pending_dir = config.event_path / "pending"

        effective = config.bus.backend
        if config.bus.backend == "rabbitmq":
            rabbitmq_url = settings.rabbitmq_url()
            if rabbitmq_url is not None:
                durable: EventBusProtocol = RabbitMqBus(rabbitmq_url)
            else:
                durable = InMemoryBus(pending_dir)
                effective = "memory"
                logger.warning(
                    "<context>:bus.backend=rabbitmq but RabbitMQ credentials "
                    "are missing/incomplete in .env — falling back to the "
                    "in-process InMemoryBus (no cross-restart durability)"
                )
        else:
            durable = InMemoryBus(pending_dir)
            effective = "memory"

        self._durable_bus = durable
        self.outbox_writer = self._build_outbox_writer(config)
        self.bus_backend = effective
        logger.info(
            "<context>:eventbus assembled backend=%s outbox=%s",
            effective,
            self.outbox_writer is not None,
        )
        return CompositeBus(durable, self.outbox_writer)

    def _build_outbox_writer(
        self, config: Config
    ) -> Callable[["Event"], Awaitable[str]] | None:
        """Build the outbox writer closure; None when not MySQL-backed.

        The closure opens its own session, enqueues the event into
        ``outbox_events`` with a fresh ``message_id`` (uuid4 hex) and
        commits — one transaction, so an event is either fully recorded or
        not at all.  ``OutboxPublisher`` drains the table to the durable
        bus afterwards (server startup step (c)).

        The shared MySQL session factory is kept on ``self._session_factory``
        for the server's OutboxPublisher and the workers' dedup.
        """
        if config.storage.backend != "mysql":
            return None
        from ant.storage.outbox_ops import enqueue
        from ant.storage.repository import MysqlHistoryRepository

        if not isinstance(self.history_store, MysqlHistoryRepository):
            logger.warning(
                "<context>:storage.backend=mysql but history_store is not "
                "MySQL-backed — outbox writer disabled"
            )
            return None

        # Foundation-built factory owned by MysqlHistoryRepository (no
        # second connection pool to the same database).
        session_factory = self.history_store._session_factory
        self._session_factory = session_factory
        self._mysql_dsn = self._infra_settings.mysql_dsn()

        async def outbox_writer(event: Event) -> str:
            message_id = uuid4().hex
            async with session_factory() as session:
                # enqueue is synchronous: adds the row to the caller's
                # session; the commit below lands it (same transaction).
                enqueue(session, event, message_id)
                await session.commit()
            return message_id

        logger.info(
            "<context>:outbox writer enabled (mysql backend, host=%s db=%s)",
            self._infra_settings.mysql_host,
            self._infra_settings.mysql_database,
        )
        return outbox_writer

    def _init_memory(self, config: Config) -> None:
        """Initialize RAG memory components if enabled.

        Backend selection (Phase 3C):
          * ``vector_backend == "qdrant"`` → a lazy ``QdrantStore`` (dense +
            BM25 sparse, server-side RRF).  Construction never touches the
            network; runtime failures raise ``QdrantStoreError`` which the
            retriever catches and degrades from.
          * otherwise → the legacy Chroma path, kept intact as the fallback
            (plain store + optional HybridMemoryStore wrap).
          * ``graph_enabled`` and complete Neo4j credentials → a lazy Neo4j
            ``MemoryGraph``; otherwise ``self.graph = None``.
        """
        if not config.memory.enabled:
            self.memory_guard = None
            self.memory_retriever = None
            self.embedding_provider = None
            self.vector_store = None
            self.doc_ingester = None
            self.graph = None
            return

        self.embedding_provider = EmbeddingProvider.from_config(config)

        if getattr(config.memory, "vector_backend", "chroma") == "qdrant":
            from ant.provider.memory.qdrant_store import QdrantStore

            self.vector_store = QdrantStore(config, self.embedding_provider)
        else:
            self.vector_store = VectorStore.from_config(config, self.embedding_provider)
            # Wrap in the hybrid store: vector + BM25 dual index, fused on
            # query. Writes maintain both indexes transparently for all
            # callers.
            if config.memory.hybrid_enabled:
                from ant.provider.memory.hybrid_store import HybridMemoryStore

                self.vector_store = HybridMemoryStore(self.vector_store, config)

        # Create retriever before guard because guard depends on retriever
        self.memory_retriever = MemoryRetriever(self)
        self.memory_guard = MemoryGuard(self)

        # Phase 3C: optional Neo4j memory graph — used by the guard for
        # conflict arbitration and by the retriever for entity expansion.
        self.graph = self._build_graph(config)

        # 17 document ingester
        self.doc_ingester = DocumentIngester(
            vector_store=self.vector_store,
            chunk_size=config.memory.chunk_size,
            chunk_overlap=config.memory.chunk_overlap,
        )

    def _build_graph(self, config: Config) -> Any:
        """Build the Neo4j MemoryGraph when enabled and credentials exist.

        Returns ``None`` — with a WARNING naming only the masked URI — when
        the graph is disabled, credentials are incomplete, or construction
        fails.  The memory guard and retriever already degrade gracefully to
        vector-only operation (design principle 11).
        """
        if not getattr(config.memory, "graph_enabled", False):
            return None
        settings = getattr(self, "_infra_settings", None)
        if settings is None:
            settings = InfraSettings()
        uri = settings.neo4j_uri()
        username = settings.neo4j_username()
        password = settings.neo4j_password()
        if not uri or not username or not password:
            logger.warning(
                "<context>:memory graph enabled but Neo4j credentials are "
                "missing/incomplete in .env — memory graph disabled (uri=%s)",
                settings.masked_neo4j_uri(),
            )
            return None

        from ant.memory.graph import MemoryGraph

        # Connection-pool knobs are read from settings when present (getattr
        # fallback — settings without them use driver defaults).
        driver_kwargs: dict[str, Any] = {}
        for attr, kwarg in (
            ("neo4j_max_connection_pool_size", "max_connection_pool_size"),
            ("neo4j_connection_timeout", "connection_timeout"),
            ("neo4j_max_connection_lifetime", "max_connection_lifetime"),
            ("neo4j_connection_acquisition_timeout", "connection_acquisition_timeout"),
        ):
            value = getattr(settings, attr, None)
            if value is not None:
                driver_kwargs[kwarg] = value

        try:
            return MemoryGraph(
                uri,
                username,
                password,
                database=settings.neo4j_database(),
                **driver_kwargs,
            )
        except Exception as exc:  # noqa: BLE001 — degrade, never crash startup
            logger.warning(
                "<context>:MemoryGraph construction failed (%s) — memory "
                "graph disabled (uri=%s)",
                type(exc).__name__,
                settings.masked_neo4j_uri(),
            )
            return None

    async def auto_ingest_docs(self) -> None:
        """Auto-ingest docs_path on startup (called from the server's async context).

        The old ``asyncio.get_event_loop().run_until_complete(...)`` here ran
        inside ``__init__`` and raised RuntimeError whenever SharedContext was
        built from inside a running event loop (improve.md #22). Ingest failure
        is logged but must not take down server startup.
        """
        if not self.config.memory.docs_path:
            return
        if self.doc_ingester is None:
            logger.warning("<context>:Memory disabled, skip auto-ingest of docs")
            return

        docs_path = self.config.workspace / self.config.memory.docs_path
        try:
            if docs_path.is_file():
                logger.info("<context>:Auto-ingesting document: %s", docs_path)
                await self.doc_ingester.ingest_file(str(docs_path))
            elif docs_path.is_dir():
                logger.info("<context>:Auto-ingesting documents from: %s", docs_path)
                await self.doc_ingester.ingest_directory(str(docs_path))
            else:
                logger.warning("<context>:Configured docs_path does not exist: %s", docs_path)
        except Exception:
            logger.exception("<context>:Auto-ingest failed for %s", docs_path)
