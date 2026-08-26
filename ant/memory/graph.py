"""Neo4j memory graph (Phase 3B).

Facts are stored as ``:Memory`` nodes connected to the ``:Entity`` nodes
they mention via ``:MENTIONED_IN`` edges.  The graph layer provides:

* ``ingest``           — MERGE a memory + its entities + edges (idempotent)
* ``detect_conflicts`` — find older facts sharing entities + category
* ``expand``           — one-hop subgraph expansion for retrieval
* ``mark_superseded``  — arbitration outcome: new fact replaces an old one
* ``archive_stale``    — soft-archive (``archived=true``) old low-importance facts

Uses the neo4j **async** driver (5.x API).  The driver import is lazy so
importing this module never hard-fails when the driver is absent.

Credentials discipline: the URI/password go straight to the driver and are
NEVER logged.  Failures are wrapped in ``MemoryGraphError`` whose message
carries only the failure *class name* — never the URI, never the
credentials, never the driver's own error text (which can embed the URI).

Failure contract: every method raises a clear exception on connection
failure; the caller (``ant.core.memory_guard``) falls back to graph-free
operation.
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from ant.memory.extraction import normalize_entities

try:
    from neo4j import AsyncGraphDatabase
except ImportError as exc:  # pragma: no cover - exercised when neo4j absent
    AsyncGraphDatabase = None  # type: ignore[assignment]
    _NEO4J_IMPORT_ERROR = exc
else:
    _NEO4J_IMPORT_ERROR = None

logger = logging.getLogger(__name__)

# Top-N older memories returned by detect_conflicts.
CONFLICT_LOOKBACK = 3


class MemoryGraphError(RuntimeError):
    """Raised when a graph operation fails (message never contains secrets)."""


def _iso_now() -> str:
    return datetime.now().isoformat()


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class MemoryGraph:
    """Async Neo4j-backed memory graph (5.x async driver)."""

    def __init__(
        self,
        uri: str,
        username: str,
        password: str,
        database: str | None = None,
        **driver_kwargs: Any,
    ):
        """Open the driver.  ``database=None`` = server default database.

        Extra keyword arguments (e.g. connection-pool sizing) are passed
        straight to ``AsyncGraphDatabase.driver``.
        """
        if AsyncGraphDatabase is None:
            raise MemoryGraphError(
                "neo4j driver is not installed — run `pip install neo4j`"
            ) from _NEO4J_IMPORT_ERROR
        self._database = database
        self._closed = False
        self._driver = AsyncGraphDatabase.driver(
            uri, auth=(username, password), **driver_kwargs
        )
        logger.info("MemoryGraph driver created (database=%r)", database)

    async def close(self) -> None:
        """Idempotent driver shutdown (safe to call multiple times)."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._driver.close()
        except Exception as exc:  # pragma: no cover - real driver only
            logger.warning("MemoryGraph close failed (%s)", type(exc).__name__)

    async def _run(self, query: str, params: dict[str, Any]):
        """Execute a Cypher query; wrap failures without leaking secrets."""
        if self._closed:
            raise MemoryGraphError("MemoryGraph is closed")
        try:
            async with self._driver.session(database=self._database) as session:
                result = await session.run(query, **params)
                # Result 绑定 session 生命周期：必须在 async with 内消费完，
                # 返回记录列表。旧实现退出 session 后才迭代 Result，
                # 真驱动抛 ResultConsumedError（假 session 单测测不出）。
                return [record async for record in result]
        except MemoryGraphError:
            raise
        except Exception as exc:
            # 凭据纪律: 只带失败类别，绝不带 driver 错误原文（可能含 URI）
            raise MemoryGraphError(
                f"MemoryGraph query failed ({type(exc).__name__}); "
                "graph access disabled for this call"
            ) from exc

    async def ingest(self, memory: dict) -> str:
        """MERGE a :Memory node + its :Entity nodes + :MENTIONED_IN edges.

        Idempotent: re-ingesting the same ``memory_id`` updates the node,
        re-asserts the edges and un-archives a previously archived node.
        Conflict detection/arbitration is NOT part of ingest — see
        ``detect_conflicts`` (read-only) and the guard-side arbitration
        (which materializes its outcome via ``mark_superseded``).

        Returns the ``memory_id`` (generated as a uuid4 hex string when the
        input dict does not carry one).
        """
        memory_id = memory.get("memory_id") or uuid.uuid4().hex
        now = _iso_now()
        query = """
        MERGE (m:Memory {memory_id: $memory_id})
        SET m.content = $content,
            m.category = $category,
            m.importance = $importance,
            m.created_at = $created_at,
            m.updated_at = $updated_at,
            m.source = $source,
            m.session_id = $session_id,
            m.archived = false
        WITH m
        UNWIND $entities AS ent
        MERGE (e:Entity {name: ent.name})
        SET e.type = ent.type
        MERGE (m)-[:MENTIONED_IN]->(e)
        RETURN m.memory_id AS memory_id
        """
        params = {
            "memory_id": memory_id,
            "content": str(memory.get("content") or ""),
            "category": memory.get("category") or "fact",
            "importance": _safe_int(memory.get("importance", 5), 5),
            "created_at": memory.get("created_at") or now,
            "updated_at": memory.get("updated_at") or now,
            "source": memory.get("source") or "",
            "session_id": memory.get("session_id") or "",
            "entities": normalize_entities(memory.get("entities", [])),
        }
        records = await self._run(query, params)
        for record in records:
            return record.get("memory_id") or memory_id
        return memory_id

    async def detect_conflicts(self, candidate: dict) -> list[dict]:
        """Find older facts that may conflict with the candidate.

        A conflict candidate must share an entity, have the same category,
        and be strictly older (``updated_at``) than the candidate.  Returns
        the top ``CONFLICT_LOOKBACK`` most recent ones, each as
        ``{memory_id, content, category, importance, updated_at}``.

        Read-only — no edges are created here; LLM arbitration lives in
        ``ant.core.memory_guard``.
        """
        entities = normalize_entities(candidate.get("entities", []))
        entity_names = [e["name"] for e in entities]
        if not entity_names:
            return []

        query = """
        MATCH (e:Entity)
        WHERE e.name IN $entity_names
        MATCH (m:Memory)-[:MENTIONED_IN]->(e)
        WHERE m.category = $category
          AND m.updated_at < $candidate_time
          AND (m.archived IS NULL OR m.archived = false)
        RETURN DISTINCT m.memory_id AS memory_id,
               m.content AS content,
               m.category AS category,
               m.importance AS importance,
               m.updated_at AS updated_at
        ORDER BY m.updated_at DESC
        LIMIT $limit
        """
        params = {
            "entity_names": entity_names,
            "category": candidate.get("category") or "fact",
            "candidate_time": candidate.get("updated_at") or _iso_now(),
            "limit": CONFLICT_LOOKBACK,
        }
        records = await self._run(query, params)

        conflicts: list[dict] = []
        for record in records:
            conflicts.append(
                {
                    "memory_id": record.get("memory_id"),
                    "content": record.get("content"),
                    "category": record.get("category"),
                    "importance": record.get("importance"),
                    "updated_at": record.get("updated_at"),
                }
            )
        return conflicts

    async def expand(self, memory_ids: list[str]) -> list[dict]:
        """One-hop subgraph expansion around the given memory ids.

        Returns three kinds of items (archived memories excluded); every
        item carries ``memory_id`` / ``content`` / ``category`` /
        ``importance`` / ``updated_at`` (``None`` where not applicable) and
        ``rel_type``:

        * related :Entity nodes                  -> rel_type "MENTIONED_IN"
        * other memories sharing an entity       -> rel_type "SHARES_ENTITY"
        * newer memories on the SUPERSEDES chain -> rel_type "SUPERSEDES"
        """
        ids = [str(i) for i in memory_ids]
        if not ids:
            return []

        query = """
        MATCH (m:Memory)-[:MENTIONED_IN]->(e:Entity)
        WHERE m.memory_id IN $ids AND (m.archived IS NULL OR m.archived = false)
        RETURN e.name AS name, e.type AS type,
               null AS memory_id, null AS content,
               null AS category, null AS importance, null AS updated_at,
               'ENTITY' AS kind, 'MENTIONED_IN' AS rel_type
        UNION
        MATCH (m:Memory)-[:MENTIONED_IN]->(e:Entity)<-[:MENTIONED_IN]-(peer:Memory)
        WHERE m.memory_id IN $ids
          AND peer.memory_id <> m.memory_id
          AND (m.archived IS NULL OR m.archived = false)
          AND (peer.archived IS NULL OR peer.archived = false)
        RETURN e.name AS name, null AS type,
               peer.memory_id AS memory_id, peer.content AS content,
               peer.category AS category, peer.importance AS importance,
               peer.updated_at AS updated_at,
               'MEMORY' AS kind, 'SHARES_ENTITY' AS rel_type
        UNION
        MATCH (m:Memory)-[:SUPERSEDES]->(newer:Memory)
        WHERE m.memory_id IN $ids
          AND (m.archived IS NULL OR m.archived = false)
          AND (newer.archived IS NULL OR newer.archived = false)
        RETURN null AS name, null AS type,
               newer.memory_id AS memory_id, newer.content AS content,
               newer.category AS category, newer.importance AS importance,
               newer.updated_at AS updated_at,
               'MEMORY' AS kind, 'SUPERSEDES' AS rel_type
        """
        records = await self._run(query, {"ids": ids})

        items: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for record in records:
            kind = record.get("kind")
            rel_type = record.get("rel_type")
            if kind == "ENTITY":
                item = {
                    "name": record.get("name"),
                    "type": record.get("type"),
                    "memory_id": None,
                    "content": None,
                    "category": None,
                    "importance": None,
                    "updated_at": None,
                    "rel_type": rel_type,
                }
            else:
                item = {
                    "name": record.get("name"),
                    "type": None,
                    "memory_id": record.get("memory_id"),
                    "content": record.get("content"),
                    "category": record.get("category"),
                    "importance": record.get("importance"),
                    "updated_at": record.get("updated_at"),
                    "rel_type": rel_type,
                }
            key = (kind, item["memory_id"] or item.get("name") or "")
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
        return items

    async def mark_superseded(self, old_id: str, new_id: str) -> None:
        """Create a :Memory-[:SUPERSEDES]->:Memory edge (arbitration outcome).

        Safe no-op when either node does not exist yet (MATCH finds
        nothing) — exactly the guard's calling pattern: arbitration marks
        superseded BEFORE the new memory is ingested, and the edge only
        materializes once the new node exists.
        """
        query = """
        MATCH (old:Memory {memory_id: $old_id}), (new:Memory {memory_id: $new_id})
        MERGE (old)-[:SUPERSEDES]->(new)
        """
        await self._run(query, {"old_id": old_id, "new_id": new_id})

    async def archive_stale(self, min_importance: int, days: int) -> int:
        """Soft-archive stale facts and return how many were archived.

        A memory is stale when ``importance < min_importance`` AND
        ``updated_at`` is older than ``days`` days; it gets
        ``SET m.archived = true`` (soft archive only — never a physical
        delete).  ``updated_at`` must be an ISO-8601 string (as written by
        ``ingest``) so the lexicographic comparison matches chronological
        order.
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        query = """
        MATCH (m:Memory)
        WHERE m.importance < $min_importance
          AND m.updated_at < $cutoff
          AND (m.archived IS NULL OR m.archived = false)
        SET m.archived = true
        RETURN count(m) AS archived_count
        """
        records = await self._run(
            query,
            {
                "min_importance": _safe_int(min_importance, 1),
                "cutoff": cutoff,
            },
        )
        for record in records:
            return int(record.get("archived_count") or 0)
        return 0
