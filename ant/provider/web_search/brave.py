"""Brave search API provider"""
from typing import TYPE_CHECKING
import httpx

from .base import WebSearchProvider, SearchResult

if TYPE_CHECKING:
    from ant.utils.config import Config


class BraveSearchProvider(WebSearchProvider):
    """Web search provider using Brave Search API"""
    BASE_URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, config: "Config"):
        """Initialize Brave Search provider"""
        self.api_key = config.websearch.api_key

    async def search(self, query: str) -> list[SearchResult]:
        """Search the web using Brave search API"""
        timeout = httpx.Timeout(30.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                self.BASE_URL,
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self.api_key
                },
                params={
                    "q": query,
                    "count": 10
                },
            )
            response.raise_for_status()
            data = response.json()

        results = []
        for item in data.get("web", {}).get("results", {}):
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("description", "")
                )
            )

        return results
