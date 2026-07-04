"""SentenceTransformer embedding provider for local models."""

import os
import logging
import datetime
from typing import TYPE_CHECKING

from .base import EmbeddingProvider

if TYPE_CHECKING:
    from ant.utils.config import Config

logger = logging.getLogger(__name__)


class SentenceTransformerEmbeddingProvider(EmbeddingProvider):
    """Embedding provider using sentence-transformers (local models)."""

    def __init__(self, model_name: str, device: str = "cpu"):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["SENTENCE_TRANSFORMERS_OFFLINE"] = "1"

        from sentence_transformers import SentenceTransformer
        print(datetime.datetime.now(), "Loading sentence-transformers model start")
        self.model = SentenceTransformer(model_name, device=device)
        logger.info(f"Loaded sentence-transformers model: {model_name} on {device}")
        print(datetime.datetime.now(), "Loaded sentence-transformers model done")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts using local sentence-transformer model."""
        if not texts:
            return []
        # 同步方法，但我们在异步函数中调用，可通过 run_in_executor 避免阻塞
        # 这里直接调用，因为 sentence-transformers 本身是同步的，但数据量通常很小
        embeddings = self.model.encode(texts, normalize_embeddings=True)
        return embeddings.tolist()

    @staticmethod
    def from_config(config: "Config") -> "SentenceTransformerEmbeddingProvider":
        """Create from config."""
        model = config.memory.embedding_model or "BAAI/bge-small-zh-v1.5"
        return SentenceTransformerEmbeddingProvider(model)
