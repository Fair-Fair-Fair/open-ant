"""Constrained-LLM memory extraction (Phase 3B).

Replaces free-form JSON prompting with **tool-call constrained output**: the
LLM must call the ``extract_memories`` tool (strict JSON Schema) once per
fact, and we parse only ``tool_calls[].arguments`` — never free text.

Design principles carried over (workspace/code.md):
  * #3 单个坏数据不连坐整批 — one malformed tool call is dropped with a
    warning; the rest of the batch still lands.
  * #13 防御性钳制 LLM 输入 — importance is int()-converted and clamped to
    1..10; keywords/entities are coerced to safe shapes.

Failure contract:
  * LLM call failure → the exception propagates (the caller,
    ``ant.core.memory_guard``, catches it and falls back).
  * Unparseable arguments JSON → warning + that call contributes nothing
    (never a batch-wide crash); an all-bad batch returns ``[]``.
"""

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ant.provider.llm.base import LLMProvider

logger = logging.getLogger(__name__)

EXTRACT_TOOL_NAME = "extract_memories"

# ── Strict JSON Schema: the model can only produce well-formed memories ──
EXTRACT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": EXTRACT_TOOL_NAME,
            "description": (
                "Extract one fact worth remembering long-term from the "
                "conversation. Call this tool once per fact."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The fact: a concise, self-contained sentence",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["user_pref", "personal", "project", "decision", "fact"],
                    },
                    "importance": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "description": "1-10; only extract items >= 5",
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Relevant keywords for retrieval",
                    },
                    "entities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "type": {
                                    "type": "string",
                                    "description": (
                                        "e.g. person / project / "
                                        "language / company / place"
                                    ),
                                },
                            },
                            "required": ["name", "type"],
                            "additionalProperties": False,
                        },
                        "description": "Named entities mentioned in the fact",
                    },
                },
                "required": ["content", "category", "importance", "keywords", "entities"],
                "additionalProperties": False,
            },
        },
    }
]

EXTRACTION_PROMPT = (
    "You are a memory extraction system. Analyze the conversation below and "
    "extract facts worth remembering long-term.\n\n"
    "**CRITICAL**: Only extract information from the **USER's** messages. "
    "Ignore all assistant (AI) responses, as they often contain information "
    "already stored in documents or general knowledge.\n\n"
    "Only extract information that has lasting value:\n"
    "- User preferences, habits, and opinions\n"
    "- Personal information (name, job, location, etc.)\n"
    "- Project details and tech stack (when stated by user)\n"
    "- Important decisions and conclusions made by user\n"
    "- Corrections the user made about your behavior\n"
    "\n"
    "Do NOT extract:\n"
    "- Transient conversation details (greetings, simple Q&A)\n"
    "- Information already covered by tool results\n"
    "- Trivial or context-dependent details\n"
    "- Any facts that appear to be from assistant responses\n"
    "\n"
    "For every fact, call the `extract_memories` tool exactly once with:\n"
    '- "content": the fact to remember (concise, self-contained sentence)\n'
    '- "category": one of "user_pref", "personal", "project", "decision", "fact"\n'
    '- "importance": integer 1-10 (only include items >= 5)\n'
    '- "keywords": list of relevant keywords for retrieval\n'
    '- "entities": list of named entities in the fact, each an object with '
    '"name" and "type" (e.g. person / project / language / company / place)\n'
    "\n"
    "If nothing is worth remembering, do NOT call the tool at all.\n"
    "\n"
    "Conversation:\n"
    "{conversation}"
)


def _serialize_messages(messages: list[dict]) -> str:
    """Serialize messages to plain text for extraction (system/tool skipped)."""
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role in ("system", "tool"):
            continue
        lines.append(f"{role.upper()}: {content}")
    return "\n".join(lines)


def _min_importance(config: Any) -> int:
    """min_importance from the config; accepts full Config or a bare object."""
    memory_cfg = getattr(config, "memory", None)
    if memory_cfg is not None:
        value = getattr(memory_cfg, "min_importance", 5)
    else:
        value = getattr(config, "min_importance", 5)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 5


def _safe_importance(value: Any, default: int = 5) -> int:
    """int() conversion with fallback + 1..10 clamp (设计原则 13)."""
    try:
        importance = int(value)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid importance value %r, defaulting to %d",
            value, default,
        )
        return default
    return max(1, min(10, importance))


def normalize_entities(entities: Any) -> list[dict]:
    """Coerce raw entity items to ``{"name": str, "type": str}`` dicts.

    Accepts ``{"name": ..., "type": ...}`` dicts and bare strings (type
    defaults to "fact").  Invalid items are dropped.
    """
    out: list[dict] = []
    if not isinstance(entities, list):
        return out
    for ent in entities:
        if isinstance(ent, dict):
            name = str(ent.get("name") or "").strip()
            if not name:
                continue
            etype = ent.get("type")
            out.append({"name": name, "type": str(etype or "fact").strip()})
        elif isinstance(ent, str) and ent.strip():
            out.append({"name": ent.strip(), "type": "fact"})
    return out


def _validate_item(item: Any, min_importance: int) -> dict | None:
    """Validate one parsed memory object; None = dropped with a warning."""
    if not isinstance(item, dict):
        logger.warning("Skipping malformed memory item (not a dict): %r", item)
        return None

    try:
        content = str(item.get("content") or "").strip()
        if not content:
            return None

        importance = _safe_importance(item.get("importance", 5))
        if importance < min_importance:
            return None

        category = item.get("category") or "fact"
        if not isinstance(category, str):
            category = "fact"

        keywords = item.get("keywords", [])
        if not isinstance(keywords, list):
            keywords = []
        keywords = [k for k in keywords if isinstance(k, str)]

        entities = item.get("entities", [])
    except Exception as exc:
        logger.warning("Skipping invalid memory item %r: %s", item, exc)
        return None

    return {
        "content": content,
        "category": category,
        "importance": importance,
        "keywords": keywords,
        "entities": normalize_entities(entities),
    }


async def extract_memories(
    llm: "LLMProvider",
    messages: list[dict],
    config: Any,
) -> list[dict]:
    """Extract memorable facts via constrained tool-call output.

    Args:
        llm: the configured LLMProvider (litellm Router).
        messages: conversation messages (only user content is a source).
        config: full Config (or duck-typed object with
            ``.memory.min_importance``).

    Returns:
        Validated memory dicts:
        ``{content, category, importance, keywords, entities}``.
    """
    conversation_text = _serialize_messages(messages)
    extraction_messages = [
        {
            "role": "user",
            "content": EXTRACTION_PROMPT.format(
                conversation=conversation_text
            ),
        }
    ]

    # Constrained output + low temperature for deterministic schema output.
    # LLM failures intentionally propagate — memory_guard owns the fallback.
    _, tool_calls, _ = await llm.chat(
        extraction_messages,
        EXTRACT_TOOLS,
        temperature=0.2,
    )

    memories: list[dict] = []
    min_importance = _min_importance(config)

    for call in tool_calls:
        if getattr(call, "name", None) != EXTRACT_TOOL_NAME:
            logger.debug(
                "Ignoring unexpected tool call %r",
                getattr(call, "name", None),
            )
            continue
        try:
            parsed = json.loads(call.arguments or "null")
        except json.JSONDecodeError as exc:
            # 设计原则 3: 单条坏数据不连坐整批 — drop this call, keep the rest
            logger.warning(
                "Failed to parse extract_memories arguments (dropping): %s",
                exc,
            )
            continue

        items = parsed if isinstance(parsed, list) else [parsed]
        for item in items:
            memory = _validate_item(item, min_importance)
            if memory is not None:
                memories.append(memory)

    if not memories:
        logger.warning("Memory extraction produced no valid memories")
    return memories
