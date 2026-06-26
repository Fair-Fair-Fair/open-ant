"""Base class for web search poviders"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from ant.utils.config import Config


class SearchResult(BaseModel):
    """Normalized search result from any provider"""

    title: str
    url: str
    snippet: str


class WebSearchProvider(ABC):
    """Abstract base class for web search providers"""

    @abstractmethod
    async def search(self, query: str) -> list[SearchResult]:
        """Search the web and return normalized results"""
        pass

    @staticmethod
    def from_config(config: "Config") -> "WebSearchProvider":
        """Create a web search provider from a config object"""
        if config.websearch is None:
            raise ValueError("No web search provider configured")
        match config.websearch.provider:
            case "brave":
                from .brave import BraveSearchProvider
                return BraveSearchProvider(config)
            case "tavily":
                from .tavily import TavilySearchProvider
                return TavilySearchProvider(config)
            case _:
                raise ValueError(f"Unknown web search provider: {config.websearch.provider}")
