# 新增: src/ant/core/tracer.py —— Phase 6: OTel 桥接实现
"""Execution tracer — Phase 6 起为 OpenTelemetry 桥接。

一个用户 turn = 根 span ``agent.run``；管线阶段 / LLM / 工具为子 span。
原内存 Span/Trace 接口保留（stream_stages 的 ``_start_span`` 调用点不变），
内部改为 OTel span 的适配层：``add_event`` → span.add_event（带属性）、
``finish`` → set_status + end。tracing 禁用时是零开销 no-op 适配器。

链路语义（trace.md §3）：子 span 的父 = 当前 contextvars 里的活动 span
——事件消费端已用 ``trace.use_span(consume_span)`` 包裹 handler，
所以 MainAgent → publish → consume → SubAgent 自动串成一条 Trace。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ant.observability import tracing

logger = logging.getLogger(__name__)


@dataclass
class Span:
    """OTel span 适配层（保留旧接口：add_event / finish）。"""

    span_id: str  # 仅用于日志
    trace_id: str
    name: str
    start_time: float
    _otel_span: Any = None  # opentelemetry.trace.Span（no-op 时为 None）
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    end_time: float | None = None
    status: str = "ok"

    @property
    def duration_ms(self) -> float:
        end = self.end_time or time.time()
        return (end - self.start_time) * 1000

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self.events.append(
            {"name": name, "attributes": attributes or {}, "timestamp": time.time()}
        )
        if self._otel_span is not None:
            self._otel_span.add_event(name, attributes or {})

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value
        if self._otel_span is not None:
            self._otel_span.set_attribute(key, value)

    def finish(self, status: str = "ok") -> None:
        self.end_time = time.time()
        self.status = status
        if self._otel_span is not None:
            if status != "ok":
                from opentelemetry.trace import Status, StatusCode

                self._otel_span.set_status(Status(StatusCode.ERROR, status))
            self._otel_span.end()


@dataclass
class Trace:
    """一次用户 turn 的根 span 容器（session 级）。"""

    trace_id: str
    session_id: str
    spans: list[Span] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)
    _root: Span | None = None

    def start_span(self, name: str) -> Span:
        otel_span = None
        # 父链：当前 contextvars 活动 span（消费端 use_span 包裹的结果）
        parent_ctx = tracing.current_span_context()
        if tracing.is_enabled():
            kwargs: dict[str, Any] = {"attributes": {}}
            if parent_ctx is not None:
                kwargs["context"] = parent_ctx
            otel_span = tracing.get_tracer().start_span(name, **kwargs)

        span = Span(
            span_id=str(uuid.uuid4())[:8],
            trace_id=self.trace_id,
            name=name,
            start_time=time.time(),
            _otel_span=otel_span,
        )
        self.spans.append(span)
        return span

    def summary(self) -> dict[str, Any]:
        ttft_ms = None
        for s in self.spans:
            for evt in s.events:
                if evt.get("name") == "first_token":
                    ttft_ms = evt.get("attributes", {}).get("ttft_ms")
        result = {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "total_spans": len(self.spans),
            "total_duration_ms": (time.time() - self.start_time) * 1000,
            "spans": [
                {
                    "name": s.name,
                    "duration_ms": s.duration_ms,
                    "status": s.status,
                }
                for s in self.spans
            ],
        }
        if ttft_ms is not None:
            result["ttft_ms"] = ttft_ms
        return result


class ExecutionTracer:
    """Creates and manages execution traces for agent sessions."""

    def __init__(self) -> None:
        self._traces: dict[str, Trace] = {}

    def start_trace(self, session_id: str) -> Trace:
        trace = Trace(
            trace_id=str(uuid.uuid4()),
            session_id=session_id,
        )
        # 根 span：agent.run（每个用户 turn 一条）
        root = trace.start_span("agent.run")
        root.set_attribute("session.id", session_id)
        trace._root = root
        self._traces[trace.trace_id] = trace
        logger.info("Trace started: %s for session %s", trace.trace_id, session_id)
        return trace

    def finish_trace(self, trace: Trace) -> dict[str, Any]:
        if trace._root is not None:
            trace._root.finish("ok")
        summary = trace.summary()
        logger.info("Trace completed: %s", summary)
        self._traces.pop(trace.trace_id, None)
        return summary
