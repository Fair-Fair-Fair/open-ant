"""Base class for web read providers."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from ant.utils.config import Config


class ReadResult(BaseModel):
    """Normalized result from reading a web page."""

    url: str
    title: str
    content: str  # Markdown content
    error: str | None = None


class WebReadProvider(ABC):
    """Abstract base class for web page reading providers."""

    @abstractmethod
    async def read(self, url: str) -> ReadResult:
        """Read a web page and return normalized content."""
        pass

    @staticmethod
    def from_config(config: "Config") -> "WebReadProvider":
        """Factory method to create provider from config."""
        if config.webread is None:
            raise ValueError("Webread not configured")

        match config.webread.provider:
            case "langchain":
                from .langchain_loader import LangChainWebReadProvider
                return LangChainWebReadProvider()
            case "crawl4ai":
                from .crawl4ai import Crawl4AIProvider
                return Crawl4AIProvider()
            case _:
                raise ValueError(f"Unknown webread provider: {config.webread.provider}")