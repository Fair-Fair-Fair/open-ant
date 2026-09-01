"""OpenTelemetry tracing for the agent runtime (Phase 6, trace.md §5/§8/§9).

一个用户请求 = 一个 Trace；LLM 调用 / 工具调用 / 管线阶段 / 事件发布与消费
都是 Span。跨 MainAgent → EventBus → SubAgent 的链路通过 **W3C traceparent
随事件载荷传播**（trace.md 面试预案的落地）：同步路径用 contextvars，异步
EventBus 路径把 traceparent 写进 event.traceparent 字段，消费端 extract 后
以 consume span 为父重建链路——跨线程、跨进程都不断。

初始化（幂等，SharedContext 构造时调用）：
  * observability.tracing_enabled=False → 无 provider（零开销 no-op）
  * 设置了 OTEL_EXPORTER_OTLP_ENDPOINT 环境变量 → OTLP HTTP 导出
    （对接 Jaeger/Tempo/OTel Collector）
  * observability.trace_to_file=True → JSON Lines 落 <logging_path>/traces.jsonl
    （无外部依赖的本地查询/演示）
  * observability.trace_console=True → ConsoleSpanExporter（开发调试）
  * 全部未配置 → 保持 no-op（默认不刷控制台噪音）

脱敏纪律：span 属性只放 metadata（长度/计数/模型名/token 数/状态），
**绝不放 prompt / 参数 / 结果内容**（trace.md §6）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context as OtelContext
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import SpanContext, StatusCode, get_current_span, set_span_in_context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

logger = logging.getLogger(__name__)

_TRACER_NAME = "open-ant"
_provider: TracerProvider | None = None
_initialized = False
_file_exporter: "FileSpanExporter | None" = None


class FileSpanExporter(SpanExporter):
    """JSON Lines span exporter — 无 Jaeger 时的本地查询/演示路径。

    每行一个 span（trace_id/parent_id/name/attributes/status/时间），
    可用 jq/grep 直接查"某个 trace 的整条链"。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, spans: list[ReadableSpan]) -> SpanExportResult:
        with open(self._path, "a", encoding="utf-8") as f:
            for span in spans:
                ctx: SpanContext | None = span.get_span_context()
                if ctx is None or not ctx.is_valid:
                    continue
                f.write(
                    json.dumps(
                        {
                            "trace_id": format(ctx.trace_id, "032x"),
                            "span_id": format(ctx.span_id, "016x"),
                            "parent_id": (
                                format(span.parent.span_id, "016x")
                                if span.parent is not None
                                else None
                            ),
                            "name": span.name,
                            "attributes": dict(span.attributes or {}),
                            "status": span.status.status_code.name,
                            "start_ms": span.start_time // 1_000_000,
                            "end_ms": span.end_time // 1_000_000 if span.end_time else None,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


def init_tracing(config: Any) -> None:
    """Idempotent provider init from workspace config. 禁用时零开销 no-op。"""
    global _provider, _initialized, _file_exporter
    if _initialized:
        return
    _initialized = True

    obs_cfg = getattr(config, "observability", None)
    if obs_cfg is None or not getattr(obs_cfg, "tracing_enabled", True):
        return  # 无 provider → get_tracer 返回 no-op 代理

    exporter: SpanExporter
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        # 对接 Jaeger / Tempo / OTel Collector（trace.md §5 的标准架构）
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter()
        processor: SpanProcessor = BatchSpanProcessor(exporter)
        logger.info("OTel tracing: OTLP exporter (endpoint 来自环境变量)")
    elif getattr(obs_cfg, "trace_to_file", False):
        logging_path = Path(getattr(config, "logging_path", Path(".logs")))
        _file_exporter = FileSpanExporter(logging_path / "traces.jsonl")
        exporter = _file_exporter
        processor = SimpleSpanProcessor(exporter)
        logger.info("OTel tracing: 文件导出 %s", logging_path / "traces.jsonl")
    elif getattr(obs_cfg, "trace_console", False):
        exporter = ConsoleSpanExporter()
        processor = SimpleSpanProcessor(exporter)
        logger.info("OTel tracing: 控制台导出（开发调试）")
    else:
        # 启用但未配置任何导出目标：保持 no-op（不刷控制台噪音）
        logger.info("OTel tracing: 未配置导出目标，保持 no-op "
                    "（设置 OTEL_EXPORTER_OTLP_ENDPOINT 或 trace_to_file/trace_console）")
        return

    _provider = TracerProvider(
        resource=Resource.create({"service.name": "open-ant"})
    )
    _provider.add_span_processor(processor)
    trace.set_tracer_provider(_provider)


def get_tracer() -> trace.Tracer:
    return trace.get_tracer(_TRACER_NAME)


def is_enabled() -> bool:
    """Provider 是否真实注册（禁用时返回 False，调用方可跳过额外开销）。"""
    return _provider is not None


# ── W3C traceparent 传播（trace.md §3/§9：异步消息里显式携带 context） ────


def inject_current_traceparent() -> str | None:
    """当前活动 span 的 W3C traceparent；无活动 span 时返回 None。

    Producer 在发布事件时调用，把链路上下文注入事件载荷。
    """
    span = get_current_span()
    ctx: SpanContext | None = span.get_span_context()
    if ctx is None or not ctx.is_valid:
        return None
    return f"00-{format(ctx.trace_id, '032x')}-{format(ctx.span_id, '016x')}-01"


def extract_traceparent(traceparent: str | None) -> OtelContext:
    """从事件载荷里的 traceparent 重建 OtelContext（Consumer 侧）。"""
    if not traceparent:
        return OtelContext()
    carrier = {"traceparent": traceparent}
    return TraceContextTextMapPropagator().extract(carrier)


def start_consume_span(
    event_type: str,
    traceparent: str | None,
    bus_label: str,
) -> trace.Span | None:
    """消费端：从事件携带的 context 重建父链，创建 agent.event.consume span。

    返回 span（调用方用 trace.use_span(span, end_on_exit=True) 包裹 handler），
    使后续 Agent/LLM/Tool span 全部挂在 consume span 之下。
    """
    parent_ctx = extract_traceparent(traceparent)
    span = get_tracer().start_span(
        "agent.event.consume",
        context=parent_ctx,
        attributes={
            "event.type": event_type,
            "bus": bus_label,
        },
    )
    return span


def current_span_context() -> OtelContext | None:
    """当前 span 的 context（用于把新根 span 挂到消费链下）。"""
    span = get_current_span()
    ctx: SpanContext | None = span.get_span_context()
    if ctx is None or not ctx.is_valid:
        return None
    return set_span_in_context(span)


def trace_use_span(span: trace.Span, end_on_exit: bool = True):
    """opentelemetry.trace.use_span 的便捷再导出（end_on_exit 默认 True）。"""
    return trace.use_span(span, end_on_exit=end_on_exit)


__all__ = [
    "FileSpanExporter",
    "init_tracing",
    "get_tracer",
    "is_enabled",
    "inject_current_traceparent",
    "extract_traceparent",
    "start_consume_span",
    "current_span_context",
    "trace_use_span",
    "StatusCode",
]
