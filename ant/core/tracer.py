# 新增: src/ant/core/tracer.py
from __future__ import annotations
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Span:
    """A single execution span."""
    span_id: str
    trace_id: str
    name: str
    start_time: float
    end_time: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    status: str = "ok"

    @property
    def duration_ms(self) -> float:
        end = self.end_time or time.time()
        return (end - self.start_time) * 1000

    def finish(self, status: str = "ok") -> None:
        self.end_time = time.time()
        self.status = status

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self.events.append({
            "name": name,
            "attributes": attributes or {},
            "timestamp": time.time(),
        })


@dataclass
class Trace:
    """A complete execution trace containing multiple spans."""
    trace_id: str
    session_id: str
    spans: list[Span] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)

    def start_span(self, name: str) -> Span:
        span = Span(
            span_id=str(uuid.uuid4())[:8],
            trace_id=self.trace_id,
            name=name,
            start_time=time.time(),
        )
        self.spans.append(span)
        return span

    def summary(self) -> dict[str, Any]:
        return {
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


class ExecutionTracer:
    """Creates and manages execution traces for agent sessions."""

    def __init__(self) -> None:
        self._traces: dict[str, Trace] = {}

    def start_trace(self, session_id: str) -> Trace:
        trace = Trace(
            trace_id=str(uuid.uuid4()),
            session_id=session_id,
        )
        self._traces[trace.trace_id] = trace
        logger.info("Trace started: %s for session %s", trace.trace_id, session_id)
        return trace

    def finish_trace(self, trace: Trace) -> dict[str, Any]:
        summary = trace.summary()
        logger.info("Trace completed: %s", summary)
        return summary
