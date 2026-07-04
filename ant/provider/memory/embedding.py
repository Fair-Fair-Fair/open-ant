"""Embedding provider using LiteLLM."""

import logging
from typing import TYPE_CHECKING

from litellm import aembedding
from .base import EmbeddingProvider

if TYPE_CHECKING:
    from ant.utils.config import Config

logger = logging.getLogger(__name__)


class LiteLLMEmbeddingProvider(EmbeddingProvider):
    """Embedding provider using litellm for multi-provider support."""

    def __init__(self, config: "Config"):
        self.model = config.memory.embedding_model
        self.api_key = config.llm.api_key
        self.api_base = config.llm.api_base

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts using litellm."""
        if not texts:
            return []

        kwargs: dict = {
            "model": self.model,
            "input": texts,
            "api_key": self.api_key,
        }
        if self.api_base:
            kwargs["api_base"] = self.api_base

        response = await aembedding(**kwargs)
        return [item["embedding"] for item in response.data]
