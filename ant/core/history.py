"""Legacy JSONL conversation history backend (Phase 0).

Phase 1: the pydantic schemas moved to ``ant.storage.schemas``; this
module keeps them re-exported for backward compatibility and retains
the synchronous ``HistoryStore`` JSONL implementation, which is adapted
to the async ``HistoryRepository`` protocol by
``ant.storage.repository.JsonlHistoryRepository``.
"""

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ant.storage.schemas import (
    MAX_PERSISTED_TOOL_CHARS,  # noqa: F401  (re-exported for compat)
    HistoryMessage,  # noqa: F401  (re-exported for compat)
    HistorySession,  # noqa: F401  (re-exported for compat)
)

if TYPE_CHECKING:
    from ant.utils.config import Config

__all__ = ["HistoryStore", "HistoryMessage", "HistorySession", "MAX_PERSISTED_TOOL_CHARS"]


def _now_iso() -> str:
    """Return current datetime as ISO format string."""
    return datetime.now().isoformat()


class HistoryStore:
    """JSONL file-based history storage.

    Legacy Phase 0 implementation — synchronous, file based.  New code
    should go through ``HistoryRepository`` (see
    ``ant.storage.repository``) instead of calling this directly.
    """

    @staticmethod
    def from_config(config: "Config") -> "HistoryStore":
        return HistoryStore(config.history_path)

    def __init__(self, base_path: Path):
        self.base_path = Path(base_path)
        self.sessions_path = self.base_path / "sessions"
        self.index_path = self.base_path / "index.jsonl"

        self.base_path.mkdir(parents=True, exist_ok=True)
        self.sessions_path.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str) -> Path:
        """Get the file path for a session."""
        return self.sessions_path / f"{session_id}.jsonl"

    def _read_index(self) -> list[HistorySession]:
        """Read all session entries from index.jsonl."""
        if not self.index_path.exists():
            return []

        sessions = []
        with open(self.index_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        sessions.append(HistorySession.model_validate_json(line))
                    except Exception:
                        continue
        return sessions

    def _write_index(self, sessions: list[HistorySession]) -> None:
        """Write all session entries to index.jsonl."""
        with open(self.index_path, "w") as f:
            for session in sessions:
                f.write(session.model_dump_json() + "\n")

    def _find_session_index(
        self, sessions: list[HistorySession], session_id: str
    ) -> int:
        """Find the index of a session in the list."""
        for i, s in enumerate(sessions):
            if s.id == session_id:
                return i
        return -1

    def create_session(self, agent_id: str, session_id: str,
                       source: "Any") -> dict[str, Any]:
        """Create a new conversation session."""
        now = _now_iso()
        session = HistorySession(
            id=session_id,
            agent_id=agent_id,
            source=source,
            title=None,
            message_count=0,
            created_at=now,
            updated_at=now,
        )

        # Append to index
        with open(self.index_path, "a") as f:
            f.write(session.model_dump_json() + "\n")

        # Create session file
        self._session_path(session_id).touch()

        return session.model_dump()

    def save_message(self, session_id: str, message: HistoryMessage) -> None:
        """Save a message to history."""
        sessions = self._read_index()
        idx = self._find_session_index(sessions, session_id)
        if idx < 0:
            raise ValueError(f"Session not found: {session_id}")

        session = sessions[idx]

        # Append message to session file
        session_file = self._session_path(session_id)
        with open(session_file, "a") as f:
            f.write(message.model_dump_json() + "\n")

        # Update index
        session.message_count += 1
        session.updated_at = _now_iso()

        # Auto-generate title from first user message
        if session.title is None and message.role == "user":
            title = message.content[:50]
            if len(message.content) > 50:
                title += "..."
            session.title = title

        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        self._write_index(sessions)

    def list_sessions(self) -> list[HistorySession]:
        """List all sessions, most recently updated first."""
        sessions = self._read_index()
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions

    def get_messages(self, session_id: str) -> list[HistoryMessage]:
        """Get all messages for a session."""
        session_file = self._session_path(session_id)
        if not session_file.exists():
            return []

        messages: list[HistoryMessage] = []
        with open(session_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        messages.append(HistoryMessage.model_validate_json(line))
                    except Exception:
                        continue

        return messages

    def get_session_info(self, session_id: str) -> HistorySession | None:
        """Get session metadata without loading messages."""
        sessions = self._read_index()
        for session in sessions:
            if session.id == session_id:
                return session
        return None
