"""Unit tests for ``ant.memory.graph.MemoryGraph`` (no real network).

The neo4j async driver is replaced by a scripted fake (monkeypatched onto
``ant.memory.graph.AsyncGraphDatabase``); fake sessions record every
``run()`` call so tests assert on Cypher fragments and parameters instead
of talking to Aura.

The final module-level test is a real-Aura smoke test that is *skipped*
(never failed) when credentials are missing or the instance is
unreachable — the skip reason names only the failure class.
"""

import uuid
from pathlib import Path

import pytest

import ant.memory.graph as graph_module
from ant.memory.graph import MemoryGraph, MemoryGraphError

# ---------------------------------------------------------------------------
# Scripted fake driver
# ---------------------------------------------------------------------------

class FakeAsyncResult:
    """Mimics the subset of ``neo4j.AsyncResult`` used by MemoryGraph."""

    def __init__(self, records=()):
        self._records = list(records)

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        for record in self._records:
            yield record

    async def single(self):
        return self._records[0] if self._records else None


class FakeAsyncSession:
    """Records every run() call; returns scripted records from the driver."""

    def __init__(self, driver):
        self.driver = driver
        self.calls = []  # list of (query, params)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def run(self, query, **params):
        self.calls.append((query, params))
        if self.driver.run_error is not None:
            raise self.driver.run_error
        if self.driver.results:
            return FakeAsyncResult(self.driver.results.pop(0))
        return FakeAsyncResult()


class FakeAsyncDriver:
    """Replaces the real driver; ``session()`` yields FakeAsyncSession."""

    def __init__(self, results=(), run_error=None):
        self.results = [list(r) for r in results]
        self.run_error = run_error
        self.sessions = []
        self.close_count = 0

    def session(self, database=None):
        session = FakeAsyncSession(self)
        session.database = database
        self.sessions.append(session)
        return session

    async def close(self):
        self.close_count += 1


class FakeAsyncGraphDatabase:
    """Replaces ``neo4j.AsyncGraphDatabase``; ``driver()`` returns a fake."""

    driver_instance = None

    @classmethod
    def driver(cls, uri, auth=None, **kwargs):
        driver = cls.driver_instance or FakeAsyncDriver()
        driver.uri = uri
        driver.auth = auth
        driver.kwargs = kwargs
        cls.driver_instance = driver
        return driver


def make_graph(monkeypatch, results=(), run_error=None):
    """Build a MemoryGraph whose driver is the scripted fake."""
    FakeAsyncGraphDatabase.driver_instance = FakeAsyncDriver(
        results=results, run_error=run_error
    )
    monkeypatch.setattr(graph_module, "AsyncGraphDatabase", FakeAsyncGraphDatabase)
    graph = MemoryGraph("bolt://fake:7687", "user", "pass", database="neo4j")
    return graph, FakeAsyncGraphDatabase.driver_instance


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_constructor_passes_uri_and_auth(monkeypatch):
    graph, driver = make_graph(monkeypatch)
    assert driver.uri == "bolt://fake:7687"
    assert driver.auth == ("user", "pass")
    assert graph._database == "neo4j"


def test_missing_driver_raises_clear_error(monkeypatch):
    monkeypatch.setattr(graph_module, "AsyncGraphDatabase", None)
    with pytest.raises(MemoryGraphError, match="pip install neo4j"):
        MemoryGraph("bolt://fake:7687", "u", "p")


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

async def test_ingest_merges_memory_entities_and_edges(monkeypatch):
    graph, driver = make_graph(monkeypatch, results=[[{"memory_id": "mem-1"}]])
    returned = await graph.ingest(
        {
            "memory_id": "mem-1",
            "content": "Alice prefers dark mode",
            "category": "user_pref",
            "importance": 8,
            "keywords": ["alice", "theme"],
            "entities": [{"name": "Alice", "type": "person"}],
            "created_at": "2026-08-01T10:00:00",
            "updated_at": "2026-08-01T10:00:00",
            "source": "platform-cli",
            "session_id": "sess-1",
        }
    )
    assert returned == "mem-1"
    assert len(driver.sessions) == 1
    query, params = driver.sessions[0].calls[0]
    assert "MERGE (m:Memory {memory_id: $memory_id})" in query
    assert "MERGE (e:Entity {name: ent.name})" in query
    assert "MERGE (m)-[:MENTIONED_IN]->(e)" in query
    assert "m.archived = false" in query  # re-ingest un-archives
    assert params["memory_id"] == "mem-1"
    assert params["content"] == "Alice prefers dark mode"
    assert params["category"] == "user_pref"
    assert params["importance"] == 8
    assert params["entities"] == [{"name": "Alice", "type": "person"}]
    assert params["session_id"] == "sess-1"


async def test_ingest_generates_memory_id_when_missing(monkeypatch):
    graph, driver = make_graph(monkeypatch)  # no records -> fallback return
    returned = await graph.ingest(
        {
            "content": "fact",
            "importance": 5,
            "entities": [],  # empty UNWIND yields no rows: still MERGEs node
        }
    )
    assert returned
    query, params = driver.sessions[0].calls[0]
    assert params["memory_id"] == returned
    assert params["category"] == "fact"
    assert params["importance"] == 5
    assert params["entities"] == []


async def test_ingest_normalizes_entities(monkeypatch):
    graph, driver = make_graph(monkeypatch)
    await graph.ingest(
        {
            "content": "f",
            "importance": 5,
            "entities": [
                "Alice",
                {"name": "py", "type": "language"},
                {"type": "x"},  # no name -> dropped
            ],
        }
    )
    _, params = driver.sessions[0].calls[0]
    assert params["entities"] == [
        {"name": "Alice", "type": "fact"},
        {"name": "py", "type": "language"},
    ]


# ---------------------------------------------------------------------------
# detect_conflicts
# ---------------------------------------------------------------------------

async def test_detect_conflicts_returns_older_same_category_memories(monkeypatch):
    records = [
        {
            "memory_id": "old-1",
            "content": "Alice prefers light mode",
            "category": "user_pref",
            "importance": 6,
            "updated_at": "2026-07-01T00:00:00",
        }
    ]
    graph, driver = make_graph(monkeypatch, results=[records])
    conflicts = await graph.detect_conflicts(
        {
            "content": "Alice prefers dark mode",
            "category": "user_pref",
            "importance": 7,
            "entities": [{"name": "Alice", "type": "person"}],
            "updated_at": "2026-08-01T00:00:00",
        }
    )
    assert conflicts == records
    query, params = driver.sessions[0].calls[0]
    assert "WHERE e.name IN $entity_names" in query
    assert "m.category = $category" in query
    assert "m.updated_at < $candidate_time" in query
    assert "ORDER BY m.updated_at DESC" in query
    assert "LIMIT $limit" in query
    assert params["entity_names"] == ["Alice"]
    assert params["category"] == "user_pref"
    assert params["candidate_time"] == "2026-08-01T00:00:00"
    assert params["limit"] == 3


async def test_detect_conflicts_without_entities_returns_empty(monkeypatch):
    graph, driver = make_graph(monkeypatch)
    conflicts = await graph.detect_conflicts(
        {"content": "fact", "category": "fact", "entities": []}
    )
    assert conflicts == []
    assert driver.sessions == []  # no query executed at all


async def test_connection_failure_raises_wrapped_error(monkeypatch):
    graph, driver = make_graph(monkeypatch, run_error=RuntimeError("boom"))
    with pytest.raises(MemoryGraphError) as excinfo:
        await graph.detect_conflicts(
            {"entities": [{"name": "Alice", "type": "person"}]}
        )
    message = str(excinfo.value)
    assert "RuntimeError" in message  # failure CLASS is fine to surface
    # 凭据纪律: 失败消息绝不含 URI 或认证信息
    assert "fake" not in message
    assert "user" not in message
    assert "pass" not in message


# ---------------------------------------------------------------------------
# expand
# ---------------------------------------------------------------------------

async def test_expand_returns_entities_peers_and_supersedes(monkeypatch):
    entity_records = [
        {"kind": "ENTITY", "rel_type": "MENTIONED_IN", "name": "Alice", "type": "person"},
        {"kind": "ENTITY", "rel_type": "MENTIONED_IN", "name": "Alice", "type": "person"},
    ]  # duplicate row: UNION dedupes in real Neo4j, seen-set dedupes here
    peer_records = [
        {
            "kind": "MEMORY", "rel_type": "SHARES_ENTITY", "name": "Alice",
            "type": None, "memory_id": "mem-2", "content": "Alice works at Acme",
            "category": "fact", "importance": 6, "updated_at": "2026-07-01T00:00:00",
        }
    ]
    supersede_records = [
        {
            "kind": "MEMORY", "rel_type": "SUPERSEDES", "name": None, "type": None,
            "memory_id": "mem-3", "content": "Alice prefers dark mode",
            "category": "user_pref", "importance": 8, "updated_at": "2026-08-01T00:00:00",
        }
    ]
    # One run call returns ONE result set: the UNION query's combined rows.
    graph, driver = make_graph(
        monkeypatch,
        results=[entity_records + peer_records + supersede_records],
    )
    items = await graph.expand(["mem-1"])

    assert [i["rel_type"] for i in items] == ["MENTIONED_IN", "SHARES_ENTITY", "SUPERSEDES"]
    assert items[0]["name"] == "Alice"
    assert items[0]["type"] == "person"
    assert items[0]["memory_id"] is None  # entities carry no memory fields
    assert items[1]["memory_id"] == "mem-2"
    assert items[1]["content"] == "Alice works at Acme"
    assert items[2]["memory_id"] == "mem-3"
    assert items[2]["rel_type"] == "SUPERSEDES"

    assert len(driver.sessions) == 1
    assert len(driver.sessions[0].calls) == 1
    query, params = driver.sessions[0].calls[0]
    assert "-[:MENTIONED_IN]->(e:Entity)" in query
    assert "'SHARES_ENTITY'" in query
    assert "peer.memory_id <> m.memory_id" in query
    assert "-[:SUPERSEDES]->(newer:Memory)" in query
    assert params == {"ids": ["mem-1"]}


async def test_expand_empty_ids_returns_empty(monkeypatch):
    graph, driver = make_graph(monkeypatch)
    assert await graph.expand([]) == []
    assert driver.sessions == []


# ---------------------------------------------------------------------------
# mark_superseded / archive_stale / close
# ---------------------------------------------------------------------------

async def test_mark_superseded_creates_edge(monkeypatch):
    graph, driver = make_graph(monkeypatch)
    await graph.mark_superseded("old-1", "new-1")
    query, params = driver.sessions[0].calls[0]
    assert "MERGE (old)-[:SUPERSEDES]->(new)" in query
    assert params == {"old_id": "old-1", "new_id": "new-1"}


async def test_archive_stale_soft_archives_and_counts(monkeypatch):
    graph, driver = make_graph(monkeypatch, results=[[{"archived_count": 3}]])
    count = await graph.archive_stale(min_importance=3, days=30)
    assert count == 3
    query, params = driver.sessions[0].calls[0]
    assert "SET m.archived = true" in query
    assert "RETURN count(m) AS archived_count" in query
    assert params["min_importance"] == 3
    assert params["cutoff"].startswith("20")  # ISO timestamp


async def test_close_is_idempotent(monkeypatch):
    graph, driver = make_graph(monkeypatch)
    await graph.close()
    await graph.close()
    assert driver.close_count == 1


async def test_operations_after_close_raise_clear_error(monkeypatch):
    graph, driver = make_graph(monkeypatch)
    await graph.close()
    with pytest.raises(MemoryGraphError, match="closed"):
        await graph.detect_conflicts(
            {"entities": [{"name": "A", "type": "person"}]}
        )


# ---------------------------------------------------------------------------
# Real-Aura smoke test (skips when unconfigured or unreachable)
# ---------------------------------------------------------------------------

def _read_env_file() -> dict:
    """Parse NEO4J_* values from the repo-root .env; values never embedded."""
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if not env_path.is_file():
        return {}
    keys = {"NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD", "NEO4J_DATABASE"}
    values = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() in keys:
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _neo4j_credentials() -> dict:
    """Read NEO4J_* from InfraSettings (if a sibling already wired it) or
    directly from the .env file.  Never embeds values in this module."""
    creds = {}
    try:
        from ant.utils.settings import InfraSettings

        infra = InfraSettings()
        for name, attr in (
            ("uri", "neo4j_uri"),
            ("username", "neo4j_username"),
            ("password", "neo4j_password"),
            ("database", "neo4j_database"),
        ):
            value = getattr(infra, attr, None)
            if value:
                creds[name] = value
    except Exception:
        pass
    if not all(creds.get(k) for k in ("uri", "username", "password")):
        env = _read_env_file()
        creds = {
            "uri": env.get("NEO4J_URI"),
            "username": env.get("NEO4J_USERNAME"),
            "password": env.get("NEO4J_PASSWORD"),
            "database": env.get("NEO4J_DATABASE"),
        }
    return creds


@pytest.fixture(scope="module")
async def aura_graph():
    """Real-Aura MemoryGraph; module skipped when unconfigured/unreachable."""
    creds = _neo4j_credentials()
    if not all(creds.get(k) for k in ("uri", "username", "password")):
        pytest.skip("Neo4j credentials not configured in .env")
    graph = None
    try:
        graph = MemoryGraph(
            creds["uri"],
            creds["username"],
            creds["password"],
            database=creds.get("database"),
        )
        # Connectivity probe: a read-only query hits the network.
        await graph.detect_conflicts(
            {
                "content": "smoke probe",
                "category": "fact",
                "importance": 1,
                "entities": [{"name": "open-ant-smoke-probe", "type": "probe"}],
            }
        )
    except Exception as exc:
        if graph is not None:
            await graph.close()
        # 凭据纪律: skip 理由只写失败类别，绝不写 URI/密码
        pytest.skip(f"Neo4j unreachable ({type(exc).__name__})")
    yield graph
    await graph.close()


async def test_aura_smoke_ingest_expand(aura_graph):
    """End-to-end round trip on the real Aura instance.

    Writes unique smoke nodes and cleans them up (DETACH DELETE) afterwards.
    """
    memory_id = f"smoke-{uuid.uuid4().hex[:12]}"
    try:
        await aura_graph.ingest(
            {
                "memory_id": memory_id,
                "content": "smoke fact for open-ant integration test",
                "category": "fact",
                "importance": 1,
                "keywords": ["smoke"],
                "entities": [{"name": "open-ant-smoke-entity", "type": "probe"}],
                "source": "smoke-test",
                "session_id": "smoke",
            }
        )
        items = await aura_graph.expand([memory_id])
        assert any(
            i["rel_type"] == "MENTIONED_IN"
            and i["name"] == "open-ant-smoke-entity"
            for i in items
        )
    finally:
        async with aura_graph._driver.session(
            database=aura_graph._database
        ) as session:
            await session.run(
                "MATCH (m:Memory {memory_id: $id}) DETACH DELETE m",
                id=memory_id,
            )
