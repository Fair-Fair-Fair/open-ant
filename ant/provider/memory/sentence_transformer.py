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
        os.environ["TRANSFORMERS_OFFLINE"] = "1"

        from sentence_transformers import SentenceTransformer

        model_path = self._resolve_local_cache_path(model_name)
        logger.info(f"Loading sentence-transformers model from: {model_path}")
        print(datetime.datetime.now(), "Loading sentence-transformers model start")
        self.model = SentenceTransformer(model_path, device=device, local_files_only=True)
        logger.info(f"Loaded sentence-transformers model: {model_name} on {device}")
        print(datetime.datetime.now(), "Loaded sentence-transformers model done")

    @staticmethod
    def _resolve_local_cache_path(model_name: str) -> str:
        """Resolve model path from local HF cache to avoid network calls."""
        try:
            from huggingface_hub import try_to_load_from_cache
            cache_dir = os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface/hub"))
            config_path = try_to_load_from_cache(
                model_name, cache_dir=cache_dir, filename="config.json"
            )
            if config_path is not None:
                # 返回所在目录（快照目录）
                snapshot_dir = os.path.dirname(config_path)
                logger.info(f"Model {model_name} found in local cache at {snapshot_dir}")
                return snapshot_dir
        except Exception as e:
            logger.debug(f"Cache lookup failed for {model_name}: {e}")
        # 如果未缓存，返回原始模型名（后续会下载）
        logger.info(f"Model {model_name} not found in cache, will download")
        return model_name

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
