"""Memory retriever for RAG prompt injection."""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ant.core.context import SharedContext
    from ant.provider.memory.base import MemoryDocument

logger = logging.getLogger(__name__)


class MemoryRetriever:
    """Retrieves relevant memories and formats them for prompt injection."""

    def __init__(self, context: "SharedContext"):
        self.context = context
        # ── Hit-rate tracking ──
        self._total_queries: int = 0
        self._hits: int = 0  # queries that returned >= 1 result

    def get_stats(self) -> dict:
        """Return memory retrieval statistics for benchmarking."""
        return {
            "total_queries": self._total_queries,
            "hits": self._hits,
            "hit_rate": (self._hits / self._total_queries * 100)
            if self._total_queries > 0
            else 0.0,
        }

    async def retrieve(
        self, query: str, top_k: int | None = None
    ) -> list["MemoryDocument"]:
        """Retrieve top-k most relevant memories for a query."""
        if top_k is None:
            top_k = self.context.config.memory.top_k

        self._total_queries += 1
        vector_store = self.context.vector_store
        results = await vector_store.query(query, top_k=top_k)
        if results:
            self._hits += 1
        return results

    def format_for_prompt(self, memories: list["MemoryDocument"]) -> str:
        """Format retrieved memories into a Markdown block for system prompt.

        Distinguishes document snippets from conversational memories.
        """
        if not memories:
            return ""

        lines = ["## Relevant Memories", ""]
        for mem in memories:
            meta = mem.metadata
            # 判断类型
            if meta.get("type") == "document":
                # 文档片段
                filename = meta.get("filename", "unknown")
                lines.append(f"- [文档] {mem.content} (来源: {filename})")
            else:
                # 对话记忆
                category = meta.get("category", "general")
                lines.append(f"- [记忆:{category}] {mem.content}")
        return "\n".join(lines)
