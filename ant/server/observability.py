"""Phase 4B observability: Prometheus metrics, liveness/readiness probes,
optional structured JSON logging.

Design (workspace/plan.md, workspace/code.md):

* All metrics live on the global ``prometheus_client.REGISTRY`` so any
  exporter (the FastAPI ``/metrics`` route, a standalone uvicorn app, a
  pushgateway) can scrape them via ``generate_latest()``.
* Instrumentation helpers never raise and are the *single* wiring point for
  event/tool/LLM counters.  Phase 4B only wires the HTTP layer (see
  ``app.py``); the remaining call sites live in files outside this phase
  and are documented on each helper — one-line hooks for the reviewer.
* Health probes never raise and are time-boxed per component.  Two distinct
  states: ``not_configured`` (backend absent/disabled — HTTP 200) vs
  ``down`` (backend present but unreachable — HTTP 503).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from ant.bus.memory import InMemoryBus

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 3.0

# ── metrics (registered once, on the global REGISTRY) ────────────────────

EVENTS_TOTAL = Counter(
    "openant_events_total",
    "Events consumed by the runtime, labelled by event class and source namespace.",
    ["event_type", "source"],
)
QUEUE_DEPTH = Gauge(
    "openant_queue_depth",
    "Events waiting for delivery: in-process bus queue + pending outbound "
    "files, or unpublished MySQL outbox rows (unset when not measurable).",
)
TOOL_CALLS_TOTAL = Counter("openant_tool_calls_total", "Tool invocations.", ["tool"])
TOOL_DURATION_SECONDS = Histogram(
    "openant_tool_duration_seconds", "Tool execution time in seconds.", ["tool"]
)
LLM_REQUESTS_TOTAL = Counter(
    "openant_llm_requests_total", "LLM completion requests.", ["model"]
)
LLM_DURATION_SECONDS = Histogram(
    "openant_llm_duration_seconds", "LLM call duration in seconds.", ["model"]
)
TOKENS_TOTAL = Counter(
    "openant_tokens_total",
    "LLM tokens consumed, by model and direction (prompt|completion).",
    ["model", "direction"],
)
HTTP_REQUESTS_TOTAL = Counter(
    "openant_http_requests_total", "HTTP requests served.", ["method", "path"]
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "openant_http_request_duration_seconds",
    "HTTP request handling time in seconds.",
    ["method", "path"],
)

#: Content-Type for the Prometheus text exposition format.
METRICS_CONTENT_TYPE = CONTENT_TYPE_LATEST


def generate_metrics() -> bytes:
    """Render every registered metric in the Prometheus text format."""
    return generate_latest(REGISTRY)


# ── instrumentation helpers ─────────────────────────────────────────────

def record_event_consumed(event: Any) -> None:
    """Increment ``openant_events_total`` for one consumed event.

    Wiring point (Phase 4B boundary): call this as the first line of the
    event handlers in ``ant/server/agent_worker.py``, ``channel_worker.py``,
    ``delivery_worker.py`` and ``websocket_worker.py``.  Those files are
    outside this phase's scope, so until then the event counter is fed by
    the HTTP middleware only (request-level traffic).
    """
    source = getattr(getattr(event, "source", None), "_namespace", None)
    EVENTS_TOTAL.labels(
        event_type=type(event).__name__,
        source=source if source else "unknown",
    ).inc()


def observe_tool(name: str, seconds: float) -> None:
    """Record one tool call and its duration.

    Wiring point (Phase 4B boundary): ``ant/tools/registry.py`` — time the
    invocation and call this in a ``finally`` block.  One line at the call
    site, no behavioural change.
    """
    TOOL_CALLS_TOTAL.labels(tool=name).inc()
    TOOL_DURATION_SECONDS.labels(tool=name).observe(seconds)


def observe_llm(model: str, seconds: float, prompt_tokens: int, completion_tokens: int) -> None:
    """Record one LLM completion (requests, duration, tokens).

    Wiring point (Phase 4B boundary): ``ant/provider/llm/base.py`` — call
    this after the provider returns, with the model actually used.
    """
    LLM_REQUESTS_TOTAL.labels(model=model).inc()
    LLM_DURATION_SECONDS.labels(model=model).observe(seconds)
    if prompt_tokens:
        TOKENS_TOTAL.labels(model=model, direction="prompt").inc(prompt_tokens)
    if completion_tokens:
        TOKENS_TOTAL.labels(model=model, direction="completion").inc(completion_tokens)


def observe_http_request(method: str, path: str, seconds: float) -> None:
    """Record one HTTP request (count + latency histogram).

    Called from the FastAPI middleware in ``app.py``.  ``path`` is the raw
    request path; swap in route templates if label cardinality becomes a
    concern on paths with many distinct ids.
    """
    HTTP_REQUESTS_TOTAL.labels(method=method, path=path).inc()
    HTTP_REQUEST_DURATION_SECONDS.labels(method=method, path=path).observe(seconds)


# ── queue depth ─────────────────────────────────────────────────────────

def update_queue_depth(context: Any) -> None:
    """Best-effort, synchronous ``openant_queue_depth`` update.

    Measured when the durable bus is an :class:`InMemoryBus` (queue size +
    pending outbound files).  RabbitMqBus depth lives on the broker (needs
    an RPC) and is not measured here; the MySQL outbox is handled by
    ``probe_outbox_depth``.  When nothing is measurable the gauge is left
    untouched (a scrape shows the last known value).
    """
    durable = getattr(context, "_durable_bus", None)
    if not isinstance(durable, InMemoryBus):
        return
    depth = durable._queue.qsize()
    try:
        depth += sum(1 for _ in durable.pending_dir.glob("*.json"))
    except OSError:
        pass
    QUEUE_DEPTH.set(depth)


async def probe_outbox_depth(context: Any) -> int | None:
    """``openant_queue_depth`` from the MySQL outbox; None when not applicable.

    Counts ``outbox_events`` rows that have not been published yet
    (``published_at IS NULL``).  Called from the ``/metrics`` handler on
    each scrape (async-capable route).  Failures are logged, never raised.
    """
    factory = getattr(context, "_session_factory", None)
    if factory is None:
        return None
    try:
        from sqlalchemy import text

        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            async with factory() as session:
                row = await session.execute(
                    text("SELECT COUNT(*) FROM outbox_events WHERE published_at IS NULL")
                )
        depth = int(row.scalar_one())
        QUEUE_DEPTH.set(depth)
        return depth
    except Exception as exc:
        logger.warning("outbox depth probe failed (%s)", type(exc).__name__)
        return None


# ── liveness / readiness ────────────────────────────────────────────────


def check_liveness() -> dict[str, str]:
    """Liveness probe: the process is up (always ``{"status": "ok"}``)."""
    return {"status": "ok"}


def _probe_detail(exc: BaseException) -> str:
    """Short probe failure summary (failure class + message, no secrets)."""
    return f"{type(exc).__name__}: {exc}"


async def _check_mysql(context: Any) -> dict[str, Any]:
    """Real probe: ``SELECT 1`` through the MySQL session factory."""
    factory = getattr(context, "_session_factory", None)
    if factory is None:
        return {
            "name": "mysql",
            "status": "not_configured",
            "detail": "no MySQL session factory (JSONL storage or missing .env credentials)",
        }
    try:
        from sqlalchemy import text

        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            async with factory() as session:
                await session.execute(text("SELECT 1"))
        return {"name": "mysql", "status": "ok", "detail": None}
    except Exception as exc:
        return {"name": "mysql", "status": "down", "detail": _probe_detail(exc)}


async def _check_rabbitmq(context: Any) -> dict[str, Any]:
    """Probe the durable bus: only a real RabbitMqBus is a RabbitMQ check."""
    durable = getattr(context, "_durable_bus", None)
    if durable is None:
        return {"name": "rabbitmq", "status": "not_configured", "detail": "no durable bus"}
    from ant.bus.rabbitmq import RabbitMqBus

    if not isinstance(durable, RabbitMqBus):
        return {
            "name": "rabbitmq",
            "status": "not_configured",
            "detail": (
                f"durable bus is {type(durable).__name__} "
                "(memory fallback — RabbitMQ not in use)"
            ),
        }
    connection = getattr(durable, "_connection", None)
    if connection is None:
        return {
            "name": "rabbitmq",
            "status": "down",
            "detail": "bus never connected (start() did not complete)",
        }
    if getattr(connection, "is_closed", False):
        return {"name": "rabbitmq", "status": "down", "detail": "connection is closed"}
    return {"name": "rabbitmq", "status": "ok", "detail": None}


async def _check_qdrant(context: Any) -> dict[str, Any]:
    """Probe the vector store: force lazy client bootstrap, then a no-op get."""
    store = getattr(context, "vector_store", None)
    if store is None or not callable(getattr(store, "get", None)):
        return {
            "name": "qdrant",
            "status": "not_configured",
            "detail": "no vector store (memory disabled)",
        }
    try:
        bootstrap = getattr(store, "_client_async", None)
        if callable(bootstrap):
            # Lazy client bootstrap checks credentials + collection — a
            # real connectivity check, not just attribute presence.
            await asyncio.wait_for(bootstrap(), PROBE_TIMEOUT_SECONDS)
        await asyncio.wait_for(store.get([]), PROBE_TIMEOUT_SECONDS)
        return {"name": "qdrant", "status": "ok", "detail": None}
    except Exception as exc:
        return {"name": "qdrant", "status": "down", "detail": _probe_detail(exc)}


async def _check_neo4j(context: Any) -> dict[str, Any]:
    """Probe the memory graph via the driver's ``verify_connectivity``."""
    graph = getattr(context, "graph", None)
    if graph is None:
        return {
            "name": "neo4j",
            "status": "not_configured",
            "detail": "memory graph disabled or credentials missing",
        }
    driver = getattr(graph, "_driver", None)
    if driver is None or not callable(getattr(driver, "verify_connectivity", None)):
        return {
            "name": "neo4j",
            "status": "down",
            "detail": "graph object exists but driver is unavailable",
        }
    try:
        await asyncio.wait_for(driver.verify_connectivity(), PROBE_TIMEOUT_SECONDS)
        return {"name": "neo4j", "status": "ok", "detail": None}
    except Exception as exc:
        return {"name": "neo4j", "status": "down", "detail": _probe_detail(exc)}


async def check_readiness(context: Any) -> tuple[list[dict[str, Any]], bool]:
    """Probe every optional backend in parallel.

    Returns ``(component statuses, ready)`` — ``ready`` is False only when
    a configured backend is actually down ("not_configured" is fine).
    Never raises.
    """
    results = await asyncio.gather(
        _check_mysql(context),
        _check_rabbitmq(context),
        _check_qdrant(context),
        _check_neo4j(context),
    )
    ready = all(r["status"] != "down" for r in results)
    return list(results), ready


def render_readiness(statuses: list[dict[str, Any]]) -> dict[str, Any]:
    """Shape the ``/readyz`` JSON body from probe results."""
    return {
        "status": "ready" if all(s["status"] != "down" for s in statuses) else "degraded",
        "components": {
            s["name"]: {"status": s["status"], "detail": s["detail"]} for s in statuses
        },
    }


# ── structured logging (opt-in) ────────────────────────────────────────


def configure_structlog(config: Any) -> bool:
    """Enable structlog JSON logging when ``config.observability.json_logs`` is true.

    Phase 4A (auth) owns the ``observability`` config section; it is read
    with ``getattr`` so this works before and without that field.  Default
    is a no-op — the standard-library logging pipeline stays untouched.
    Returns True when structured logging is now active.
    """
    obs = getattr(config, "observability", None)
    if obs is None or not getattr(obs, "json_logs", False):
        return False
    try:
        import structlog
    except ImportError:
        logger.warning(
            "config.observability.json_logs=true but structlog is not "
            "installed — falling back to stdlib logging"
        )
        return False
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    logger.info("structured JSON logging enabled (structlog)")
    return True
