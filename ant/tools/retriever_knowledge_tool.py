"""Agent tool for searching the RAG knowledge base (read-only)."""

import logging
from typing import TYPE_CHECKING

from ant.tools.base import tool

if TYPE_CHECKING:
    from ant.core.agent import AgentSession

logger = logging.getLogger(__name__)


@tool(
    name="retriever_knowledge",
    description="Search the long-term knowledge base (RAG memory store) for relevant information. "
                "Use this tool when the user asks about stored knowledge, documents, or memories. "
                "This is a READ-ONLY search tool — it does NOT modify or add to the knowledge base. "
                "Do NOT use ingest_document to answer questions; use this tool instead.",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to find relevant knowledge from the memory store"
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results to return (default: 5, max: 10)"
            }
        },
        "required": ["query"]
    }
)
async def retriever_knowledge(query: str, session: "AgentSession", top_k: int = 5) -> str:
    """Search the vector store and return matching documents."""
    ctx = session.shared_context
    vector_store = getattr(ctx, "vector_store", None)

    if vector_store is None:
        return "Error: Knowledge base is not available. Memory system may be disabled."

    top_k = min(max(top_k, 1), 10)

    try:
        results = await vector_store.query(query, top_k=top_k)
        if not results:
            return "No relevant knowledge found in the store."

        lines = [f"Found {len(results)} relevant knowledge entries:\n"]
        for i, doc in enumerate(results, 1):
            meta = doc.metadata
            source_type = "文档" if meta.get("type") == "document" else "记忆"
            filename = meta.get("filename", "")
            category = meta.get("category", "")
            score = doc.score if doc.score is not None else 0.0

            header = f"[{i}] ({source_type})"
            if filename:
                header += f" 来源: {filename}"
            if category:
                header += f" 分类: {category}"
            header += f" (相关度: {score:.2f})"

            lines.append(header)
            lines.append(f"    {doc.content}\n")

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Knowledge search failed: {e}")
        return f"Error searching knowledge base: {e}"
