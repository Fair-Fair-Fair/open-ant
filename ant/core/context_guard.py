"""Context guard for proactive context window management."""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from litellm import token_counter
from litellm.types.completion import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionToolMessageParam,
)
from litellm.types.completion import (
    ChatCompletionMessageParam as Message,
)

from ant.core.session_state import SessionState
from ant.provider.llm import LLMProvider

if TYPE_CHECKING:
    from ant.core.context import SharedContext

logger = logging.getLogger(__name__)


# Default max size for tool result content before truncation
MAX_TOOL_RESULT_CHARS = 1000

COMPACT_PROMPT = (
    "Your task is to create a detailed summary of the conversation so far, capturing the "
    "user's requests, your actions, and any important context needed to continue without "
    "losing information.\n\n"
    "Your summary should include the following sections:\n\n"
    "1. Primary Request and Intent: What did the user explicitly ask for? Capture the "
    "full scope of their request.\n\n"
    "2. Key Facts and User Preferences: Important information exchanged, decisions made, "
    "and user preferences or constraints discovered during the conversation.\n\n"
    "3. User Messages: List ALL user messages that are not tool results. These are "
    "critical for understanding the user's feedback and changing intent.\n\n"
    "4. Errors and Corrections: Any mistakes made, how they were fixed, and especially "
    "any corrections or feedback from the user about doing things differently.\n\n"
    "5. Current Work and Pending Tasks: What was being worked on immediately before this "
    "summary, and what tasks remain unfinished.\n\n"
    "Here is the conversation to summarize:\n\n"
    "{conversation}\n\n"
    "Please provide your summary following this structure. Be precise and thorough — the "
    "next response will only have access to this summary, not the original messages."
)


@dataclass
class ContextGuard:
    """Manages context window size with proactive compaction."""

    shared_context: "SharedContext"
    token_threshold: int = 160000  # 80% of 200k context
    max_tool_result_chars: int = MAX_TOOL_RESULT_CHARS

    def estimate_tokens(self, state: "SessionState") -> int:
        """Estimate token count for session state."""
        if not state.messages:
            return 0
        return token_counter(
            model=state.agent.agent_def.llm.model, messages=state.build_messages()
        )

    async def check_and_compact(
        self,
        state: "SessionState",
    ) -> "SessionState":
        """Check token count, compact if needed."""
        token_count = self.estimate_tokens(state)

        if token_count < self.token_threshold:
            return state

        # First try truncating large tool results
        state.messages = self._truncate_large_tool_results(state.messages)
        token_count = self.estimate_tokens(state)

        if token_count < self.token_threshold:
            return state

        # If still over threshold, compact via summarization
        return await self._compact_messages(state)

    def _compress_message_count(self, state: "SessionState") -> int:
        """Calculate how many messages to compress."""
        keep_count = max(4, int(len(state.messages) * 0.2))
        compress_count = max(2, int(len(state.messages) * 0.5))
        return min(compress_count, len(state.messages) - keep_count)

    def _truncate_large_tool_results(self, messages: list[Message]) -> list[Message]:
        """Truncate oversized tool results to reduce context size."""
        result: list[Message] = []
        for msg in messages:
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if (
                    isinstance(content, str)
                    and len(content) > self.max_tool_result_chars
                ):
                    original_size = len(content)
                    truncated = content[: self.max_tool_result_chars]
                    truncated_content = (
                        f"{truncated}\n\n"
                        f"[Truncated - original size: {original_size} chars]"
                    )

                    msg = cast(
                        ChatCompletionToolMessageParam,
                        {**msg, "content": truncated_content},
                    )

            result.append(msg)
        return result

    def _serialize_messages_for_summary(self, messages: list[Message]) -> str:
        """Serialize messages to plain text for summarization."""
        lines = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            # Handle tool calls in assistant messages
            if role == "assistant" and msg.get("tool_calls"):
                tool_names = [
                    tc.get("function", {}).get("name", "unknown")
                    for tc in (cast(ChatCompletionAssistantMessageParam, msg)).get(
                        "tool_calls", []
                    )
                ]
                lines.append(
                    f"ASSISTANT: [used tools: {', '.join(tool_names)}] {content}"
                )
            else:
                lines.append(f"{role.upper()}: {content}")
        return "\n".join(lines)

    async def _compact_messages(
        self,
        state: "SessionState",
    ) -> "SessionState":
        """Compact history by summarizing older messages.

        Summarization runs on a dedicated small model when
        ``config.llm.summarize_model`` is set, otherwise on the session's
        main LLM.  Timeout/retry are already handled inside LLMProvider
        (litellm Router ``timeout``/``num_retries``), so no extra
        ``asyncio.wait_for`` is needed here.

        If summarization fails for any reason the guard degrades to a
        *hard truncation*: keep only the newest ``keep_count`` messages and
        drop the rest without a summary.  A slightly lossy context is
        better than a crashed turn (errors never propagate upward).
        """
        compress_count = self._compress_message_count(state)
        keep_count = max(4, int(len(state.messages) * 0.2))

        old_messages = state.messages[:compress_count]
        old_text = self._serialize_messages_for_summary(old_messages)

        summary_prompt = COMPACT_PROMPT.format(conversation=old_text)

        llm = self._summary_llm(state)
        try:
            response, _, _ = await llm.chat(
                [{"role": "user", "content": summary_prompt}],
                [],  # No tools needed
            )
        except Exception as exc:
            logger.warning(
                "Context compaction summarization failed (%s); degrading to "
                "hard truncation: keeping the newest %d messages without a "
                "summary",
                exc,
                keep_count,
            )
            # Hard truncation fallback — drop the oldest messages, no
            # summary text (state updated in place, same as the success path).
            state.messages = state.messages[-keep_count:] if keep_count else []
            return state

        # Build compacted message list
        messages: list[Message] = []
        messages.append(
            {
                "role": "user",
                "content": f"[Previous conversation summary]\n{response}",
            }
        )
        messages.append(
            {
                "role": "assistant",
                "content": "I've reviewed the conversation summary. Ready to continue.",
            }
        )
        messages.extend(state.messages[compress_count:])

        # Update state in place
        state.messages = messages
        return state

    def _summary_llm(self, state: "SessionState") -> "LLMProvider":
        """LLM used for compaction summarization.

        ``config.llm.summarize_model`` (when set) is used as a dedicated
        small model for the compaction job; otherwise the session's main
        LLM is reused.
        """
        llm_config = self.shared_context.config.llm
        if llm_config and llm_config.summarize_model:
            return LLMProvider.from_config(
                llm_config.model_copy(update={"model": llm_config.summarize_model})
            )
        return state.agent.llm
