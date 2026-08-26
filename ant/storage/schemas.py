"""Pydantic schemas for persisted conversation history.

Moved from ``ant.core.history`` (Phase 1) so both the JSONL and the MySQL
repositories share one canonical schema.  ``ant.core.history`` re-exports
these names for backward compatibility.

The JSONL file format is unchanged: each ``HistoryMessage`` is one JSON
line; session metadata is one JSON line in ``index.jsonl``.
"""

from datetime import datetime
from typing import Any, Literal

from litellm.types.completion import ChatCompletionMessageParam as Message
from pydantic import BaseModel, Field, field_validator

from ant.core.events import EventSource

__all__ = [
    "MAX_PERSISTED_TOOL_CHARS",
    "HistorySession",
    "HistoryMessage",
]

# Tool results are truncated before persistence to prevent context bloat.
# The LLM uses the full result in the current turn; future turns only need
# a brief reference (title + URL, which the first ~500 chars capture).
MAX_PERSISTED_TOOL_CHARS = 500


def _now_iso() -> str:
    """Return current datetime as ISO format string."""
    return datetime.now().isoformat()


class HistorySession(BaseModel):
    """Session metadata - stored in index.jsonl (JSONL backend)."""

    id: str
    agent_id: str
    source: str  # Serialized EventSource (e.g., "platform-telegram:123:456")
    title: str | None = None
    message_count: int = 0
    created_at: str
    updated_at: str

    @field_validator("source", mode="before")
    @classmethod
    def parse_source(cls, v: Any) -> str:
        if hasattr(v, "__str__"):
            return str(v)
        return v

    def get_source(self) -> EventSource:
        """Get the session's Eventsource"""
        return EventSource.from_string(self.source)


class HistoryMessage(BaseModel):
    """Single message - stored in session.jsonl (JSONL backend)."""

    timestamp: str = Field(default_factory=_now_iso)
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None

    @classmethod
    def from_message(cls, message: Message) -> "HistoryMessage":
        """Create HistoryMessage from litellm Message format.

        Tool results are truncated to ``MAX_PERSISTED_TOOL_CHARS`` to prevent
        storage bloat and context pollution on session resume.  The full
        content is used by the LLM in the current turn; future turns only
        need a brief reference.
        """
        tool_calls = None
        if message.get("tool_calls"):
            tool_calls = [
                {
                    "id": tc.get("id"),
                    "type": tc.get("type", "function"),
                    "function": tc.get("function", {}),
                }
                for tc in message["tool_calls"]
            ]

        tool_call_id = message.get("tool_call_id")

        content = str(message.get("content", ""))
        if message.get("role") == "tool" and len(content) > MAX_PERSISTED_TOOL_CHARS:
            content = content[:MAX_PERSISTED_TOOL_CHARS] + "…"

        return cls(
            role=message["role"],
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
        )

    def to_message(self) -> Message:
        """Convert HistoryMessage to litellm Message format."""
        base: dict[str, Any] = {
            "role": self.role,
            "content": self.content,
        }

        if self.role == "assistant" and self.tool_calls:
            return {
                "role": "assistant",
                "content": self.content,
                "tool_calls": self.tool_calls,
            }

        if self.role == "tool" and self.tool_call_id:
            base["tool_call_id"] = self.tool_call_id
            return base

        return base
