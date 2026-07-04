"""Base classes for memory providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from ant.utils.config import Config


class MemoryDocument(BaseModel):
    """Normalized memory document from vector store."""
    id: str
    content: str
    metadata: dict = field(default_factory=dict)
    score: float = 0.0


class EmbeddingProvider(ABC):
    """Abstract base class for embedding providers."""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts into vectors."""
        pass

    @staticmethod
    def from_config(config: "Config") -> "EmbeddingProvider":
        """Create an embedding provider from config."""
        model = config.memory.embedding_model
        # 如果模型名称包含常见本地模型标识，或配置了 provider 字段
        if (model and any(model.startswith(prefix) for prefix in ["BAAI/", "sentence-transformers/", "intfloat/"])) \
                or getattr(config.memory, "embedding_provider", "") == "sentence_transformers":
            from .sentence_transformer import SentenceTransformerEmbeddingProvider
            return SentenceTransformerEmbeddingProvider.from_config(config)
        else:
            # 否则使用 litellm（OpenAI 等云端 API）
            from .embedding import LiteLLMEmbeddingProvider
            return LiteLLMEmbeddingProvider(config)


class VectorStore(ABC):
    """Abstract base class for vector stores."""

    @abstractmethod
    async def add(
        self,
        documents: list[str],
        metadatas: list[dict] | None = None,
        ids: list[str] | None = None,
    ) -> None:
        """Add documents to the vector store."""
        pass

    @abstractmethod
    async def delete(self, ids: list[str]) -> None:
        """Delete documents by ids."""
        pass

    @abstractmethod
    async def get(self, ids: list[str]) -> list[MemoryDocument]:
        """Retrieve documents by ids."""
        pass

    @abstractmethod
    async def update(
            self,
            id: str,
            document: str,
            metadata: dict,
    ) -> None:
        """Update existing memory."""
        pass

    @abstractmethod
    async def query(
        self, query_text: str, top_k: int = 5
    ) -> list[MemoryDocument]:
        """Query the vector store for similar documents."""
        pass

    @staticmethod
    def from_config(config: "Config", embedding_provider: EmbeddingProvider) -> "VectorStore":
        """Create a vector store from config."""
        if config.memory is None:
            raise ValueError("No memory configuration found")
        match config.memory.provider:
            case "chroma":
                from .chroma_store import ChromaVectorStore
                return ChromaVectorStore(config, embedding_provider)
            case _:
                raise ValueError(f"Unknown memory provider: {config.memory.provider}")