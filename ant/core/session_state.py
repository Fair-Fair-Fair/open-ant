"""Session state container with persistence helpers."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from litellm.types.completion import ChatCompletionMessageParam as Message

from ant.core.history import HistoryMessage

if TYPE_CHECKING:
    from ant.core.agent import Agent
    from ant.core.context import SharedContext
    from ant.core.events import EventSource


@dataclass
class SessionState:
    """Pure conversation state + persistence."""

    session_id: str
    agent: "Agent"
    messages: list[Message]
    source: "EventSource"
    shared_context: "SharedContext"

    def add_message(self, message: Message) -> None:
        """Add message to in-memory list + persist."""
        self.messages.append(message)
        history_msg = HistoryMessage.from_message(message)
        self.shared_context.history_store.save_message(self.session_id, history_msg)

    def build_messages(self) -> list[Message]:
        """Build messages list with system prompt."""
        system_prompt = self.agent.agent_def.agent_md
        messages: list[Message] = [{"role": "system", "content": system_prompt}]
        messages.extend(self.messages)
        return messages
