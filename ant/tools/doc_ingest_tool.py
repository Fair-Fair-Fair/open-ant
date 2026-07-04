"""Agent tool for ingesting documents into the RAG memory store."""

from pathlib import Path
from typing import TYPE_CHECKING

from ant.tools.base import tool

if TYPE_CHECKING:
    from ant.core.agent import AgentSession


@tool(
    name="ingest_document",
    description="Load a file or directory into the long-term memory knowledge base. "
                "⚠️ ONLY use this tool when the user EXPLICITLY asks to import/load/ingest documents into the knowledge base. "
                "Do NOT use this tool to answer questions or search for information. "
                "If the user asks a question about stored knowledge, use the search_knowledge tool instead.",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the file or directory to ingest"
            }
        },
        "required": ["path"]
    }
)
async def ingest_document(path: str, session: "AgentSession") -> str:
    """Ingest a file or directory into the vector store."""
    ctx = session.shared_context
    ingester = getattr(ctx, "doc_ingester", None)

    if ingester is None:
        return "Error: Document ingestion is not available. Memory system may be disabled."

    target = Path(path)
    if not target.exists():
        return f"Error: Path does not exist: {path}"

    try:
        if target.is_file():
            count = await ingester.ingest_file(path)
            return f"Successfully ingested {target.name}: {count} chunks stored."
        elif target.is_dir():
            count = await ingester.ingest_directory(path)
            return f"Successfully ingested directory {target.name}: {count} chunks stored."
        else:
            return f"Error: Unsupported path type: {path}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error ingesting document: {e}"
