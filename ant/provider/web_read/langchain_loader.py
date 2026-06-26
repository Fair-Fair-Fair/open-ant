"""Web page reading provider using LangChain's WebBaseLoader."""

import asyncio
from langchain_community.document_loaders import WebBaseLoader

from .base import WebReadProvider, ReadResult


class LangChainWebReadProvider(WebReadProvider):
    """Web read provider using LangChain WebBaseLoader."""

    READ_TIMEOUT = 30.0

    def __init__(self):
        pass

    async def read(self, url: str) -> ReadResult:
        """Read a web page using LangChain WebBaseLoader."""
        try:
            loader = WebBaseLoader(url)
            docs = await asyncio.wait_for(
                asyncio.to_thread(loader.load),
                timeout=self.READ_TIMEOUT,
            )

            content = "\n\n".join(doc.page_content for doc in docs).strip()
            if not content:
                raise Exception("No content extracted from page")

            title = docs[0].metadata.get("title", "") if docs else ""

            return ReadResult(
                url=url,
                title=title,
                content=content,
                error=None,
            )
        except asyncio.TimeoutError:
            return ReadResult(
                url=url,
                title="",
                content="",
                error=f"Timeout: reading {url} exceeded {self.READ_TIMEOUT}s",
            )
        except Exception as e:
            return ReadResult(
                url=url,
                title="",
                content="",
                error=str(e),
            )
