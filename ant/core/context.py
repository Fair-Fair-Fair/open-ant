import asyncio
import logging

from ant.core.agent_loader import AgentLoader
from ant.core.commands.registry import CommandRegistry
from ant.core.history import HistoryStore
from ant.core.skill_loader import SkillLoader
from ant.core.eventbus import EventBus
from ant.utils.config import Config
from typing import Any

from ant.channel.base import Channel

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ant.server.websocket_worker import WebSocketWorker

from ant.core.routing import RoutingTable

from ant.core.sandbox import Sandbox
from ant.core.guardrails import Guardrails

# 12-cron-heartbeat
from ant.core.cron_loader import CronLoader

# 13 multi-layer-prompt
from ant.core.prompt_builder import PromptBuilder

# 16-rag-memory
from ant.core.memory_guard import MemoryGuard
from ant.core.memory_retriever import MemoryRetriever
from ant.provider.memory.base import EmbeddingProvider, VectorStore

# 17-rag-document-ingestion
from ant.provider.memory.doc_ingester import DocumentIngester

logger = logging.getLogger(__name__)


class SharedContext:
    """Global shared state for the application"""

    config: Config
    agent_loader: AgentLoader
    skill_loader: SkillLoader
    command_registry: CommandRegistry
    history_store: HistoryStore
    eventbus: EventBus
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

    # 17 rag-document-ingestion
    doc_ingester: 'DocumentIngester | None'

    # harness: input/output guardrails
    guardrails: Guardrails

    def __init__(self, config: Config,
                 channels: list[Channel[Any]] | None = None) -> None:
        self.config = config
        self.history_store = HistoryStore.from_config(config)
        self.agent_loader = AgentLoader.from_config(config)
        self.skill_loader = SkillLoader.from_config(config)
        self.command_registry = CommandRegistry.with_builtins()
        self.eventbus = EventBus(self)

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

        # 16 rag-memory
        self._init_memory(config)

    def _init_memory(self, config: Config) -> None:
        """Initialize RAG memory components if enabled."""
        if not config.memory.enabled:
            self.memory_guard = None
            self.memory_retriever = None
            self.embedding_provider = None
            self.vector_store = None
            self.doc_ingester = None
            return

        self.embedding_provider = EmbeddingProvider.from_config(config)
        self.vector_store = VectorStore.from_config(config, self.embedding_provider)
        # Create retriever before guard because guard depends on retriever
        self.memory_retriever = MemoryRetriever(self)
        self.memory_guard = MemoryGuard(self)

        # 17 document ingester
        self.doc_ingester = DocumentIngester(
            vector_store=self.vector_store,
            chunk_size=config.memory.chunk_size,
            chunk_overlap=config.memory.chunk_overlap,
        )

        # Auto-ingest docs_path on startup if configured
        if config.memory.docs_path:
            docs_path = config.workspace / config.memory.docs_path
            if docs_path.is_file():
                logger.info("<context>:Auto-ingesting document: %s", docs_path)
                asyncio.get_event_loop().run_until_complete(
                    self.doc_ingester.ingest_file(str(docs_path))
                )
            elif docs_path.is_dir():
                logger.info("<context>:Auto-ingesting documents from: %s", docs_path)
                asyncio.get_event_loop().run_until_complete(
                    self.doc_ingester.ingest_directory(str(docs_path))
                )
            else:
                logger.warning("<context>:Configured docs_path does not exist: %s", docs_path)
