"""Phase 6 — OpenTelemetry tracing tests.

覆盖（对齐 src/trace.md 面试预案的三大核心）：
  * span 树与父子链（agent.run → 阶段/LLM/工具子 span）；
  * **跨 EventBus 的 Trace Context 传播**：publish 注入 W3C traceparent，
    消费端 extract 后 handler 运行在 consume span 之下（contextvars 链不断）；
  * 脱敏纪律：span 属性只含 metadata（长度/计数/模型名），绝不含内容；
  * 禁用/未配置时零开销 no-op。
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ant.observability import tracing


@pytest.fixture(autouse=True)
def _reset_tracing(monkeypatch):
    """每个测试独立初始化：重置模块全局并清掉全局 provider。

    OTel 的全局 TracerProvider 只能设置一次（"Overriding ... is not
    allowed"），测试需重置内部标志——OTel 测试套件的标准做法。
    """
    from opentelemetry import trace as otel_trace
    from opentelemetry.util._once import Once

    # _TRACER_PROVIDER_SET_ONCE 是 Once 对象（非布尔），重置为全新实例
    monkeypatch.setattr(otel_trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(otel_trace, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(tracing, "_initialized", False)
    monkeypatch.setattr(tracing, "_provider", None)
    yield


def _config(**overrides) -> SimpleNamespace:
    logging_path = overrides.pop("logging_path", ".logs")
    defaults = dict(
        tracing_enabled=True, trace_to_file=False, trace_console=False,
        metrics_enabled=True, json_logs=True,
    )
    defaults.update(overrides)
    obs = SimpleNamespace(**defaults)
    return SimpleNamespace(
        observability=obs,
        logging_path=Path(logging_path),
    )


# ── 1. span 树与父子链 + 文件导出 ─────────────────────────────────────────


def test_span_tree_with_file_exporter(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")  # 清掉外部端点
    tracing.init_tracing(_config(trace_to_file=True, logging_path=str(tmp_path)))
    assert tracing.is_enabled()

    tracer = tracing.get_tracer()
    root = tracer.start_span("agent.run")
    root.set_attribute("session.id", "s1")
    with tracing.trace_use_span(root):
        child = tracer.start_span("agent.llm")
        child.set_attribute("llm.model", "deepseek/test")
        child.set_attribute("llm.prompt_tokens", 100)
        child.end()
        tool = tracer.start_span("agent.tool.bash")
        tool.set_attribute("tool.result_length", 42)
        tool.end()
    root.end()

    lines = (tmp_path / "traces.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    spans = [json.loads(line) for line in lines]
    by_name = {s["name"]: s for s in spans}
    # 父子链：child/tool 的 parent 是 root 的 span_id
    assert by_name["agent.llm"]["parent_id"] == by_name["agent.run"]["span_id"]
    assert by_name["agent.tool.bash"]["parent_id"] == by_name["agent.run"]["span_id"]
    # 语义属性落盘（脱敏纪律：只有 metadata）
    assert by_name["agent.llm"]["attributes"]["llm.model"] == "deepseek/test"
    assert by_name["agent.llm"]["attributes"]["llm.prompt_tokens"] == 100
    assert by_name["agent.tool.bash"]["attributes"]["tool.result_length"] == 42
    assert by_name["agent.run"]["attributes"]["session.id"] == "s1"


def test_no_content_leaks_into_span_attributes(tmp_path, monkeypatch):
    """trace.md §6：prompt/参数/结果内容绝不进 span 属性。"""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    tracing.init_tracing(_config(trace_to_file=True, logging_path=str(tmp_path)))
    tracer = tracing.get_tracer()
    span = tracer.start_span("agent.llm")
    span.set_attribute("llm.model", "m")
    span.end()

    line = json.loads((tmp_path / "traces.jsonl").read_text(encoding="utf-8"))
    attrs = json.dumps(line["attributes"], ensure_ascii=False)
    assert "prompt" not in attrs and "secret" not in attrs and "content" not in attrs


# ── 2. 跨 EventBus 的 Context 传播（trace.md §3/§8/§9） ───────────────────


def test_traceparent_injected_and_w3c_formatted(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    tracing.init_tracing(_config(trace_console=True))
    tracer = tracing.get_tracer()
    with tracing.trace_use_span(tracer.start_span("agent.run")):
        tp = tracing.inject_current_traceparent()
    # W3C traceparent: 00-<32hex>-<16hex>-01
    assert tp is not None
    parts = tp.split("-")
    assert parts[0] == "00" and parts[3] == "01"
    assert len(parts[1]) == 32 and len(parts[2]) == 16


def test_consume_span_rebuilds_parent_chain(monkeypatch):
    """extract 后创建的 span 挂在消费链下（跨进程重放同一 trace）。"""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    tracing.init_tracing(_config(trace_to_file=True))
    tp = "00-11111111111111111111111111111111-2222222222222222-01"
    span = tracing.start_consume_span("DispatchEvent", tp, "rabbitmq")
    assert span is not None
    # 消费 span 的 trace_id 与上游一致（同一条 Trace 的延续）
    ctx = span.get_span_context()
    assert format(ctx.trace_id, "032x") == "11111111111111111111111111111111"
    span.end()


def test_event_traceparent_serialization_roundtrip():
    """traceparent 随事件 to_dict/from_dict 往返（RabbitMQ 载荷即事件 JSON）。"""
    from ant.core.events import InboundEvent, deserialize_event

    event = InboundEvent(
        session_id="s1",
        source="platform-cli:cli-user",  # 构造时是 str，roundtrip 后校验字段存在
        content="hi",
        traceparent="00-11111111111111111111111111111111-2222222222222222-01",
    )
    restored = deserialize_event(event.to_dict())
    assert restored.traceparent == event.traceparent


async def test_composite_bus_injects_traceparent_and_handler_runs_under_consume(
    monkeypatch, tmp_path
):
    """publish 注入 → memory 总线消费时 handler 运行在 consume span 之下。"""
    from ant.bus.composite import CompositeBus
    from ant.bus.memory import InMemoryBus
    from ant.core.events import DispatchEvent

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    tracing.init_tracing(_config(trace_to_file=True, logging_path=str(tmp_path)))

    bus = CompositeBus(InMemoryBus(tmp_path / "pending"))
    await bus.start()

    seen: dict = {}
    published_event = DispatchEvent(
        session_id="sub",
        source="agent:main",
        content="task",
        parent_session_id="main",
    )

    async def handler(event):
        # handler 内启动的 span 应挂在 consume span 之下（contextvars 传播）
        seen["had_parent"] = tracing.current_span_context() is not None
        child = tracing.get_tracer().start_span("agent.subagent.run")
        seen["child_parent_valid"] = child.parent is not None
        child.end()

    bus.subscribe(DispatchEvent, handler)

    # publish 前在活动 span 内发布，traceparent 应被注入事件
    tracer = tracing.get_tracer()
    with tracing.trace_use_span(tracer.start_span("agent.run")):
        await bus.publish(published_event)

    # 等待内存总线消费（flush 语义）
    await asyncio_sleep_short()
    assert published_event.traceparent is not None
    assert seen.get("had_parent") is True  # consume span 包裹了 handler
    assert seen.get("child_parent_valid") is True
    await bus.stop()


async def asyncio_sleep_short():
    import asyncio

    await asyncio.sleep(0.05)


# ── 3. 禁用/未配置：零开销 no-op ──────────────────────────────────────────


def test_tracing_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    tracing.init_tracing(_config(tracing_enabled=False))
    assert not tracing.is_enabled()
    assert tracing.inject_current_traceparent() is None
    span = tracing.start_consume_span("InboundEvent", None, "memory")
    span.end()  # NonRecordingSpan 上 end 安全


def test_tracing_enabled_but_unconfigured_stays_noop(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    tracing.init_tracing(_config())  # 三开关全关
    assert not tracing.is_enabled()


def test_execution_tracer_works_when_tracing_disabled(monkeypatch):
    """tracing 关闭时 ExecutionTracer 适配层不抛、零开销。"""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    tracing.init_tracing(_config(tracing_enabled=False))
    from ant.core.tracer import ExecutionTracer

    tracer = ExecutionTracer()
    trace = tracer.start_trace("s1")
    span = trace.start_span("ValidationStage")
    span.add_event("begin", {"iteration": 0})
    span.finish("ok")
    summary = tracer.finish_trace(trace)
    assert summary["total_spans"] == 2  # agent.run + ValidationStage
    assert summary["spans"][0]["name"] == "agent.run"
