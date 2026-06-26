"""Crawl4AI provider for web page reading."""

import asyncio
from crawl4ai import AsyncWebCrawler

from .base import WebReadProvider, ReadResult


class Crawl4AIProvider(WebReadProvider):
    """Web read provider using Crawl4AI."""

    CRAWL_TIMEOUT = 30.0

    def __init__(self):
        """Initialize Crawl4AI provider."""
        pass

    async def read(self, url: str) -> ReadResult:
        """Read a web page using Crawl4AI."""
        try:
            async with AsyncWebCrawler(verbose=False) as crawler:
                result = await asyncio.wait_for(
                    crawler.arun(url=url),
                    timeout=self.CRAWL_TIMEOUT,
                )

                if not result.success:
                    raise Exception(result.error_message or "Failed to crawl page")

                return ReadResult(
                    url=url,
                    title=(result.metadata.get("title", "") if result.metadata else ""),
                    content=result.markdown or "",
                    error=None,
                )
        except asyncio.TimeoutError:
            return ReadResult(
                url=url,
                title="",
                content="",
                error=f"Timeout: reading {url} exceeded {self.CRAWL_TIMEOUT}s",
            )
        except Exception as e:
            return ReadResult(
                url=url,
                title="",
                content="",
                error=str(e),
            )