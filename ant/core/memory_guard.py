"""Memory guard for extracting and filtering long-term memories.

Phase 3B: extraction now runs through ``ant.memory.extraction`` (tool-call
constrained output, low temperature, per-item fault isolation).  Each
candidate is resolved against the vector store (semantic dedup) and —
when the Neo4j memory graph is enabled — against the graph (entity-level
conflict detection + LLM arbitration + SUPERSEDES edges).
"""

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from litellm.types.completion import ChatCompletionMessageParam as Message

from ant.memory.extraction import extract_memories as _constrained_extract_memories

if TYPE_CHECKING:
    from ant.core.context import SharedContext

logger = logging.getLogger(__name__)

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

CONFLICT_RESOLVE_PROMPT = """
You maintain a long-term memory graph. A new fact may contradict or refine
existing facts that mention the same entities.

Existing facts (older):
{existing}

New fact (candidate):
{candidate}

Decide what to do with the new fact. Choose exactly one action:

- "keep_new": the new fact is correct and should be stored; the old facts
  are outdated or wrong and will be marked superseded.
- "keep_old": the new fact is a duplicate or less accurate; do not store it.
- "merge": the new fact is a refined version of the old ones; store it and
  mark the old facts superseded.

Output JSON only, one object:
{{"action": "keep_new" | "keep_old" | "merge", "reason": "short justification"}}
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
            where: Any | None = None,
    ) -> list[dict]:
        """Extract memorable facts from conversation messages.

        Extraction runs through ``ant.memory.extraction`` (tool-call
        constrained output; low temperature).  Each candidate is then
        resolved via ``_resolve_memory`` — vector-store semantic dedup plus,
        when the memory graph is enabled, entity-level conflict arbitration.
        A failed extraction call degrades to ``[]`` (never raises).

        ``where``（Phase 7）scopes the semantic-dedup query to a payload
        filter (per-tenant memory isolation; the LongMemEval eval uses it
        per benchmark instance).  ``None`` = no filter (production default).
        """

        try:
            candidates = await _constrained_extract_memories(
                self.llm, messages, self.context.config
            )
        except Exception as e:
            logger.warning("Memory extraction failed, returning []: %s", e)
            return []

        if not candidates:
            return []

        # Every resolved memory gets a stable id up front: graph conflict
        # arbitration needs it for mark_superseded(), and downstream
        # ingestion (vector store + graph) can share one id per memory.
        for candidate in candidates:
            candidate.setdefault("memory_id", uuid.uuid4().hex)

        resolved: list[dict] = []

        for candidate in candidates:
            result = await self._resolve_memory(candidate, where=where)
            if result is not None:
                resolved.append(result)

        if resolved:
            logger.info(
                "Resolved %d memories from %d extracted candidates",
                len(resolved),
                len(candidates),
            )

        return resolved

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
                logger.warning("Skipping malformed memory item (not a dict): %r", item)
                continue

            try:
                content = item.get("content", "").strip()

                if not content:
                    continue

                importance = self._safe_importance(item.get("importance", 5))

                if (
                        importance
                        < self.context.config.memory.min_importance
                ):
                    continue

                category = item.get("category", "fact")

                keywords = item.get("keywords", [])

                if not isinstance(keywords, list):
                    keywords = []
            except (AttributeError, TypeError, ValueError) as e:
                # 单条解析失败不影响同批其他条目：丢弃该条并告警
                logger.warning("Skipping invalid memory item %r: %s", item, e)
                continue

            valid.append(
                {
                    "content": content,
                    "category": category,
                    "importance": importance,
                    "keywords": keywords,
                }
            )

        return valid

    @staticmethod
    def _safe_importance(value: object, default: int = 5) -> int:
        """安全转换 importance：int() 裸转遇 "high"/5.5 等值会抛异常，失败时兜底默认值。"""
        try:
            return int(value)
        except (ValueError, TypeError):
            logger.warning(
                "Invalid importance value %r, defaulting to %d",
                value, default,
            )
            return default

    async def _resolve_memory(
            self,
            candidate: dict,
            where: Any | None = None,
    ) -> dict | None:
        """
        Resolve a candidate memory.

        Returns:
            None                -> ignore
            candidate           -> create
            candidate+_action   -> update
        """

        # ── Phase 3B: graph conflict arbitration (before vector dedup) ──
        # The graph object is read from the shared context with getattr so
        # this works unchanged when the graph feature is not wired (None).
        graph = getattr(self.context, "graph", None)
        if graph is not None:
            if await self._resolve_graph_conflict(candidate, graph) is None:
                return None

        retriever = self.context.memory_retriever
        assert retriever is not None

        # Dedup/merge decisions compare SEMANTIC similarity — pure vector
        # scores, not the hybrid fused score (keyword overlap shouldn't
        # make two different facts look like the same memory).
        similar = await retriever.retrieve_semantic(
            candidate["content"],
            top_k=self.context.config.memory.merge_top_k,
            where=where,
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

    async def _resolve_graph_conflict(
            self,
            candidate: dict,
            graph: object,
    ) -> str | None:
        """Arbitrate entity-level conflicts found in the memory graph.

        Returns ``"keep"`` (store the candidate) or ``None`` (drop it).

        The graph is an optional enhancement: any failure here (unreachable
        Aura, schema drift, …) degrades to keeping the candidate — it must
        never block memory storage (graceful degradation, warning + fallback).
        On ``keep_new`` / ``merge`` the conflicting old facts are marked
        superseded via ``mark_superseded``; that call is a safe no-op until
        the new node is actually ingested, so it is harmless even when the
        candidate is dropped downstream (e.g. by the vector dedup).
        """
        try:
            conflicts = await graph.detect_conflicts(candidate)
        except Exception as e:
            logger.warning(
                "Graph conflict detection failed, keeping candidate: %s", e
            )
            return "keep"

        if not conflicts:
            return "keep"

        new_id = candidate.get("memory_id") or uuid.uuid4().hex
        candidate["memory_id"] = new_id

        existing = "\n".join(
            f"- {c.get('memory_id')}: {c.get('content')} "
            f"(category={c.get('category')}, importance={c.get('importance')})"
            for c in conflicts
        )

        messages: list[Message] = [
            {
                "role": "user",
                "content": CONFLICT_RESOLVE_PROMPT.format(
                    existing=existing,
                    candidate=candidate["content"],
                ),
            }
        ]

        try:
            response, _, _ = await self.llm.chat(messages, [])
            decision = self._parse_json(response)
        except Exception as e:
            logger.warning(
                "Conflict arbitration failed, keeping candidate: %s", e
            )
            return "keep"

        action = decision.get("action")

        if action == "keep_old":
            logger.info(
                "Dropped memory (graph conflict, keep old): %s",
                candidate["content"],
            )
            return None

        if action in ("keep_new", "merge"):
            # 仲裁采纳新记忆: 旧事实全部标记为被取代
            for conflict in conflicts:
                try:
                    await graph.mark_superseded(conflict["memory_id"], new_id)
                except Exception as e:
                    logger.warning(
                        "mark_superseded(%s -> %s) failed: %s",
                        conflict["memory_id"],
                        new_id,
                        e,
                    )
            return "keep"

        logger.warning("Unknown conflict action %r, keeping candidate", action)
        return "keep"

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

