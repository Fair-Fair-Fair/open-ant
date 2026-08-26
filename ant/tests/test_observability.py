"""Phase 4B observability tests (no real network).

Covers:
  * ``/healthz`` — always 200.
  * ``/readyz`` — unconfigured backends → 200 with every component
    "not_configured"; a backend that raises → 503 with that component
    marked "down".
  * ``/metrics`` — Prometheus exposition contains the ``openant_*``
    families and stays 200 even when a probe backend is broken.
  * HTTP middleware — request counter increases per request.
  * Phase 4A auth mount — create_app survives a missing auth module and
    mounts it when present (hermetic fakes, no dependency on auth.py).
  * Instrumentation helpers and the opt-in structlog switch.

All metric assertions use before/after deltas: the metrics live on the
global prometheus REGISTRY and accumulate across tests in one process.
"""
import logging
import sys
import types

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from ant.bus.memory import InMemoryBus
from ant.core.events import AgentEventSource, OutboundEvent
from ant.server import observability
from ant.server.app import create_app


class _FakeApi:
    host = "127.0.0.1"
    port = 8000


class _FakeConfig:
    api = _FakeApi()
    default_agent = "pickle"


class _FakeContext:
    """Minimal context for create_app/readyz — no backends configured."""

    def __init__(self) -> None:
        self.config = _FakeConfig()
        self._durable_bus = None
        self._session_factory = None
        self.vector_store = None
        self.graph = None
        self.websocket_worker = None


class _BrokenVectorStore:
    async def get(self, ids):
        raise RuntimeError("vector store unreachable")


class _BrokenSessionFactory:
    """Fake MySQL session factory whose session raises on enter."""

    def __call__(self):
        return self

    async def __aenter__(self):
        raise RuntimeError("mysql unreachable")

    async def __aexit__(self, *exc):
        return None


def _make_event() -> OutboundEvent:
    return OutboundEvent(
        session_id="s1",
        source=AgentEventSource(agent_id="a1"),
        content="hi",
    )


def _sample(name, labels):
    """Global metric sample value, 0.0 when never observed."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture()
def client():
    with TestClient(create_app(_FakeContext())) as test_client:
        yield test_client


# ── /healthz ───────────────────────────────────────────────────────────


def test_healthz_always_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ── /readyz ────────────────────────────────────────────────────────────


def test_readyz_not_configured_is_200(client):
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    for name in ("mysql", "rabbitmq", "qdrant", "neo4j"):
        assert body["components"][name]["status"] == "not_configured"


def test_readyz_vector_store_down_returns_503():
    ctx = _FakeContext()
    ctx.vector_store = _BrokenVectorStore()
    with TestClient(create_app(ctx)) as test_client:
        resp = test_client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["components"]["qdrant"]["status"] == "down"
    assert body["components"]["mysql"]["status"] == "not_configured"


def test_readyz_mysql_down_returns_503():
    ctx = _FakeContext()
    ctx._session_factory = _BrokenSessionFactory()
    with TestClient(create_app(ctx)) as test_client:
        resp = test_client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["components"]["mysql"]["status"] == "down"
    assert "mysql unreachable" in body["components"]["mysql"]["detail"]


# ── /metrics ───────────────────────────────────────────────────────────


def test_metrics_expose_openant_families(client):
    # Seed samples so every family appears in the exposition (prometheus
    # omits collectors that have no sample at all).
    observability.record_event_consumed(_make_event())
    observability.observe_tool("read_file", 0.5)
    observability.observe_llm("gpt-4o", 1.2, 10, 20)
    observability.update_queue_depth(types.SimpleNamespace(_durable_bus=InMemoryBus()))

    resp = client.get("/metrics")
    assert resp.status_code == 200
    text = resp.text
    for family in (
        "openant_events_total",
        "openant_queue_depth",
        "openant_tool_calls_total",
        "openant_tool_duration_seconds",
        "openant_llm_requests_total",
        "openant_llm_duration_seconds",
        "openant_tokens_total",
        "openant_http_requests_total",
    ):
        assert family in text


def test_metrics_tolerates_broken_outbox_probe():
    ctx = _FakeContext()
    ctx._session_factory = _BrokenSessionFactory()
    with TestClient(create_app(ctx)) as test_client:
        resp = test_client.get("/metrics")
    assert resp.status_code == 200
    assert "openant_http_requests_total" in resp.text


# ── HTTP middleware ────────────────────────────────────────────────────


def test_http_middleware_counts_requests(client):
    labels = {"method": "GET", "path": "/healthz"}
    before = _sample("openant_http_requests_total", labels)
    client.get("/healthz")
    client.get("/healthz")
    assert _sample("openant_http_requests_total", labels) - before == 2.0


# ── instrumentation helpers ────────────────────────────────────────────


def test_record_event_consumed_labels():
    labels = {"event_type": "OutboundEvent", "source": "agent"}
    before = _sample("openant_events_total", labels)
    observability.record_event_consumed(_make_event())
    assert _sample("openant_events_total", labels) - before == 1.0


def test_observe_tool_and_llm_helpers():
    tool_labels = {"tool": "write_file"}
    tool_before = _sample("openant_tool_calls_total", tool_labels)
    prompt_labels = {"model": "gpt-4o", "direction": "prompt"}
    completion_labels = {"model": "gpt-4o", "direction": "completion"}
    prompt_before = _sample("openant_tokens_total", prompt_labels)
    completion_before = _sample("openant_tokens_total", completion_labels)

    observability.observe_tool("write_file", 0.3)
    observability.observe_llm("gpt-4o", 2.0, 5, 7)

    assert _sample("openant_tool_calls_total", tool_labels) - tool_before == 1.0
    assert _sample("openant_tokens_total", prompt_labels) - prompt_before == 5.0
    assert _sample("openant_tokens_total", completion_labels) - completion_before == 7.0


# ── queue depth gauge ──────────────────────────────────────────────────


def test_queue_depth_measures_inmemory_bus(tmp_path):
    bus = InMemoryBus(pending_dir=tmp_path / "pending")
    ctx = types.SimpleNamespace(_durable_bus=bus)
    observability.update_queue_depth(ctx)
    assert REGISTRY.get_sample_value("openant_queue_depth") == 0.0

    pending = tmp_path / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (pending / "e1.json").write_text("{}", encoding="utf-8")
    (pending / "e2.json").write_text("{}", encoding="utf-8")
    observability.update_queue_depth(ctx)
    assert REGISTRY.get_sample_value("openant_queue_depth") == 2.0


# ── Phase 4A auth mount ────────────────────────────────────────────────


def test_create_app_without_auth_module_warns(monkeypatch, caplog):
    # Force "ant.server.auth" to appear unimportable even if the parallel
    # Phase 4A agent has already written the real module.
    monkeypatch.setitem(sys.modules, "ant.server.auth", None)
    with caplog.at_level(logging.WARNING, logger="ant.server.app"):
        app = create_app(_FakeContext())
    assert app is not None
    assert any("auth" in record.message for record in caplog.records)


def test_create_app_mounts_auth_when_present(monkeypatch):
    fake_auth = types.ModuleType("ant.server.auth")
    mounted = []

    def mount_auth(app, context):
        mounted.append((app, context))

    fake_auth.mount_auth = mount_auth
    monkeypatch.setitem(sys.modules, "ant.server.auth", fake_auth)

    app = create_app(_FakeContext())
    assert len(mounted) == 1
    assert mounted[0][0] is app


# ── structured logging (opt-in) ────────────────────────────────────────


def test_configure_structlog_defaults_off():
    # No ``observability`` config section (Phase 4A has not landed) →
    # the stdlib logging pipeline stays untouched.
    assert observability.configure_structlog(_FakeConfig()) is False


def test_configure_structlog_opt_in():
    cfg = types.SimpleNamespace(observability=types.SimpleNamespace(json_logs=True))
    result = observability.configure_structlog(cfg)
    # structlog is an optional dependency — enabled when installed,
    # gracefully skipped otherwise.
    assert isinstance(result, bool)
    if result:
        import structlog

        assert any(
            isinstance(p, structlog.processors.JSONRenderer)
            for p in structlog.get_config()["processors"]
        )
