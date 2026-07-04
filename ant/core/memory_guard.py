"""Memory guard for extracting and filtering long-term memories."""

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from litellm.types.completion import ChatCompletionMessageParam as Message

if TYPE_CHECKING:
    from ant.core.context import SharedContext

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """You are a memory extraction system. Analyze the conversation below and extract facts worth remembering long-term.

**CRITICAL**: Only extract information from the **USER's** messages. Ignore all assistant (AI) responses, as they often contain information already stored in documents or general knowledge.

Only extract information that has lasting value:
- User preferences, habits, and opinions
- Personal information (name, job, location, etc.)
- Project details and tech stack (when stated by user)
- Important decisions and conclusions made by user
- Corrections the user made about your behavior

Do NOT extract:
- Transient conversation details (greetings, simple Q&A)
- Information already covered by tool results
- Trivial or context-dependent details
- Any facts that appear to be from assistant responses

Conversation:
{conversation}

Return a JSON array of objects with these fields:
- "content": the fact to remember (concise, self-contained sentence)
- "category": one of "user_pref", "personal", "project", "decision", "fact"
- "importance": integer 1-10 (only include items >= 5)
- "keywords": list of relevant keywords for retrieval

If nothing is worth remembering, return an empty array: []
Return ONLY the JSON array, no other text."""

RESOLVE_PROMPT = """
You maintain a long-term memory database.

Existing memories:

{existing}

Candidate memory:

{candidate}

Choose exactly one action.

1.
{{
  "action":"ignore"
}}

2.
{{
  "action":"create"
}}

3.
{{
  "action":"update",
  "target":"memory-id"
}}

Rules:

- Ignore if duplicate.

- Update if candidate is newer,
  more precise,
  or contradicts the old one.

- Create if it is a different fact.

Output JSON only.
"""


class MemoryGuard:
    """Extracts and filters long-term memories from conversations."""

    def __init__(self, context: "SharedContext"):
        self.context = context
        from ant.provider.llm.base import LLMProvider
        self.llm = LLMProvider.from_config(self.context.config.llm)

    async def extract_memories(
            self,
            messages: list[Message],
    ) -> list[dict]:
        """Extract memorable facts from conversation messages."""

        conversation_text = self._serialize_messages(messages)

        extraction_messages: list[Message] = [
            {
                "role": "user",
                "content": EXTRACTION_PROMPT.format(
                    conversation=conversation_text
                ),
            }
        ]

        response, _, _ = await self.llm.chat(extraction_messages, [])

        candidates = self._parse_response(response)

        if not candidates:
            return []

        resolved: list[dict] = []

        for candidate in candidates:
            result = await self._resolve_memory(candidate)
            if result is not None:
                resolved.append(result)

        if resolved:
            logger.info(
                "Resolved %d memories from %d extracted candidates",
                len(resolved),
                len(candidates),
            )

        return resolved

    def _serialize_messages(self, messages: list[Message]) -> str:
        """Serialize messages to plain text for extraction."""
        lines = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if role in ("system", "tool"):
                continue
            lines.append(f"{role.upper()}: {content}")
        return "\n".join(lines)

    def _parse_response(self, response: str) -> list[dict]:
        """
        Parse extraction response returned by the LLM.

        Supports:

        - [...]
        - ```json [...] ```
        - ``` [...] ```
        """

        response = response.strip()

        if response.startswith("```"):
            lines = response.splitlines()

            if lines and lines[0].startswith("```"):
                lines = lines[1:]

            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]

            response = "\n".join(lines).strip()

        try:
            data = json.loads(response)
        except json.JSONDecodeError as e:
            logger.warning(
                "Failed to parse memory extraction response: %s",
                e,
            )
            return []

        if not isinstance(data, list):
            logger.warning(
                "Memory extraction response is not a list."
            )
            return []

        valid: list[dict] = []

        for item in data:

            if not isinstance(item, dict):
                continue

            content = item.get("content", "").strip()

            if not content:
                continue

            importance = int(
                item.get("importance", 5)
            )

            if (
                    importance
                    < self.context.config.memory.min_importance
            ):
                continue

            category = item.get("category", "fact")

            keywords = item.get("keywords", [])

            if not isinstance(keywords, list):
                keywords = []

            valid.append(
                {
                    "content": content,
                    "category": category,
                    "importance": importance,
                    "keywords": keywords,
                }
            )

        return valid

    async def _resolve_memory(
            self,
            candidate: dict,
    ) -> dict | None:
        """
        Resolve a candidate memory.

        Returns:
            None                -> ignore
            candidate           -> create
            candidate+_action   -> update
        """

        retriever = self.context.memory_retriever
        assert retriever is not None

        similar = await retriever.retrieve(
            candidate["content"],
            top_k=self.context.config.memory.merge_top_k,
        )

        # 检查是否与文档片段重复
        doc_threshold = getattr(self.context.config.memory, "doc_similarity_threshold", 0.75)
        for doc in similar:
            if doc.metadata.get("type") == "document" and doc.score >= doc_threshold:
                logger.info(f"⏭️  Ignored memory (already in documents): {candidate['content']}")
                return None

        # 数据库为空，直接新增
        if not similar:
            return candidate

        # 相似度不足，认为是新记忆
        if (
                similar[0].score
                < self.context.config.memory.merge_similarity
        ):
            return candidate

        existing = "\n".join(
            f"{m.id}: {m.content}"
            for m in similar
        )

        messages: list[Message] = [
            {
                "role": "user",
                "content": RESOLVE_PROMPT.format(
                    existing=existing,
                    candidate=candidate["content"],
                ),
            }
        ]

        response, _, _ = await self.llm.chat(messages, [])

        try:
            decision = self._parse_json(response)
        except Exception as e:
            logger.warning(
                "Failed to parse resolve response: %s",
                e,
            )
            return candidate

        action = decision.get("action")

        if action == "ignore":
            logger.info(f"⚠️ Ignored duplicate memory: {candidate['content']}")
            return None

        if action == "create":
            return candidate

        if action == "update":
            target = decision.get("target")

            if target is None:
                logger.warning(
                    "Resolve returned update without target. Ignoring candidate."
                )
                return None  # 避免无 target 时错误创建

            return {
                **candidate,
                "_action": "update",
                "_target": target,
            }

        logger.warning(
            "Unknown resolve action: %s",
            action,
        )

        return candidate

    def _parse_json(self, response: str) -> dict:
        """
        Parse JSON returned by the LLM.

        Supports:
        - {...}
        - ```json ... ```
        - ``` ... ```
        """

        response = response.strip()

        if response.startswith("```"):
            lines = response.splitlines()

            if lines and lines[0].startswith("```"):
                lines = lines[1:]

            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]

            response = "\n".join(lines).strip()

        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            logger.warning(
                "Invalid JSON response from LLM:\n%s",
                response,
            )
            raise

        if not isinstance(data, dict):
            raise TypeError(
                f"Expected dict but got {type(data).__name__}"
            )

        return data

