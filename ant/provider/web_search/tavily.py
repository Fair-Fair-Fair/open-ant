from typing import TYPE_CHECKING
import httpx

from .base import WebSearchProvider, SearchResult

if TYPE_CHECKING:
    from ant.utils.config import Config


class TavilySearchProvider(WebSearchProvider):
    """Web search provider using Tavily API"""

    BASE_URL = "https://api.tavily.com/search"

    def __init__(self, config: "Config"):
        self.api_key = config.websearch.api_key

    async def search(self, query: str) -> list[SearchResult]:
        """Search the web using Tavily API"""

        payload = {
            "api_key": self.api_key,
            "query": query,
            "search_depth": "basic",
            "max_results": 5,
            "include_answer": False,
            "include_raw_content": False,
        }

        timeout = httpx.Timeout(30.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                self.BASE_URL,
                json=payload,
            )

            response.raise_for_status()
            data = response.json()

        results = []

        for item in data.get("results", []):
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("content", ""),
                )
            )

        return results
