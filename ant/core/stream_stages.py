"""Streaming pipeline stages for harness-mode streaming chat.

Each stage is an async-generator so per-token events flow without buffering.
Stages integrate with SessionFSM (lifecycle) and ExecutionTracer (observability).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ant.core.guardrails import _INJECTION_BLOCKED_MSG, StreamRedactor
from ant.core.session_fsm import SessionPhase
from ant.core.stream_pipeline import StreamPipelineStage

logger = logging.getLogger(__name__)

# 写类工具：并发执行可能产生同文件写竞态，默认保持串行（config.parallel_writes=True 除外）。
_WRITE_TOOLS = {"write", "edit", "bash"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _start_span(ctx, name: str):
    """Start a tracer span on *ctx* if a trace is attached, no-op otherwise."""
    if ctx.trace is None:
        return None
    span = ctx.trace.start_span(name)
    span.add_event("begin", {"iteration": ctx.iteration})
    return span


def _finish_span(span, status: str = "ok") -> None:
    """Finish *span* if it exists."""
    if span is not None:
        span.finish(status)


def _try_transition(fsm, phase: SessionPhase) -> None:
    """Transition *fsm* to *phase*, logging skipped transitions."""
    try:
        fsm.transition_to(phase)
    except ValueError as exc:
        logger.debug("FSM transition skipped: %s", exc)


def _build_stream_redactor(shared_context) -> "StreamRedactor | None":
    """Build the streaming secret redactor for one LLM call, or None (直通).

    None when: guardrails disabled, output guard missing, secret redaction
    disabled on the output guard, or ``redact_buffer_chars`` == 0 (config
    passthrough mode — tokens flow unchanged).  ``redact_buffer_chars`` is
    read from ``config.guardrails.output`` via getattr (the field is added
    by the config/auth workstream in parallel; 128 is the fallback default).
    """
    if shared_context is None:
        return None
    guardrails = getattr(shared_context, "guardrails", None)
    output = getattr(guardrails, "output", None) if guardrails is not None else None
    if output is None:
        return None
    if not getattr(output, "_enabled", True):
        return None
    if not getattr(output, "_redact_secrets", True):
        return None

    config = getattr(shared_context, "config", None)
    output_cfg = (
        getattr(getattr(config, "guardrails", None), "output", None)
        if config is not None
        else None
    )
    buffer_size = (
        getattr(output_cfg, "redact_buffer_chars", 128)
        if output_cfg is not None
        else 128
    )
    if buffer_size is None:
        buffer_size = 128
    try:
        buffer_size = int(buffer_size)
    except (TypeError, ValueError):
        buffer_size = 128
    if buffer_size <= 0:
        return None  # 直通：不建 redactor，token 原样转发
    return StreamRedactor.from_output_guard(output, buffer_size=buffer_size)


def _resolve_judge_llm(ctx) -> Any:
    """Shared LLM for the injection judge — ``summarize_model`` preferred.

    优先用配置里的轻量模型（``config.llm.summarize_model``，getattr 兜底），
    未配置时回落 session 主模型（``session.agent.llm``）。任何解析失败都
    回退主模型（原则 11：降级不炸链）。
    """
    session = ctx.session
    agent = getattr(session, "agent", None)
    fallback_llm = getattr(agent, "llm", None) if agent is not None else None

    shared_context = getattr(session, "shared_context", None)
    config = getattr(shared_context, "config", None) if shared_context is not None else None
    llm_config = getattr(config, "llm", None) if config is not None else None
    small_model = (
        getattr(llm_config, "summarize_model", None) if llm_config is not None else None
    )
    if not small_model:
        return fallback_llm
    try:
        from ant.provider.llm import LLMProvider

        return LLMProvider.from_config(
            llm_config.model_copy(update={"model": small_model})
        )
    except Exception:  # noqa: BLE001 — fall back to the main model
        logger.debug(
            "Judge LLM resolution failed — falling back to the main model",
            exc_info=True,
        )
        return fallback_llm


# ---------------------------------------------------------------------------
# Pre-LLM stages
# ---------------------------------------------------------------------------

class StreamValidationStage(StreamPipelineStage):
    """Short-circuit on empty input or exhausted iteration budget."""

    async def execute(self, ctx, next):
        span = _start_span(ctx, "ValidationStage")

        if not ctx.user_message.strip():
            _finish_span(span, "ok")
            yield {"type": "done", "finish_reason": "stop"}
            return

        if ctx.iteration >= ctx.max_iterations:
            _try_transition(ctx.session.fsm, SessionPhase.EXHAUSTED)
            # 必须终止外层 run() 循环：不置 stop_reason 的话，上一轮
            # ToolExecutionStage 留下的 "tool_calls" 会让 StreamPipeline.run()
            # 再次进入循环 → 无限 yield error 死循环（Phase 2 验收发现）。
            ctx.stop_reason = "exhausted"
            if span:
                span.add_event("max_iterations_reached", {"iteration": ctx.iteration})
            _finish_span(span, "ok")
            yield {
                "type": "error",
                "data": (
                    f"Agent reached the maximum tool-call iteration limit "
                    f"({ctx.max_iterations}). Your request may be too complex, "
                    f"or the agent got stuck trying to work around a restriction. "
                    f"Try a more specific request or check the sandbox settings."
                ),
            }
            return

        _finish_span(span, "ok")
        async for event in next(ctx):
            yield event


class StreamInputGuardStage(StreamPipelineStage):
    """Validate and sanitize user input before it reaches the LLM context.

    Layers (in order):
      1. Sanitize control characters
      2. Check message length
      3. Detect prompt injection patterns (regex)
      4. Optional LLM-judge re-check (config.guardrails.input.judge_enabled)
         — only when the regex layer found nothing; UNSAFE verdicts are
         handled with the same block_injection semantics as regex hits.

    Short-circuits with an error event on violation — the pipeline never
    reaches the LLM for a malicious or malformed message.
    """

    async def execute(self, ctx, next):
        span = _start_span(ctx, "InputGuardStage")
        guardrails = ctx.session.shared_context.guardrails

        if guardrails and guardrails.input:
            input_guard = guardrails.input

            # 1. Sanitize control characters
            original = ctx.user_message
            ctx.user_message = input_guard.sanitize(original)
            if span and original != ctx.user_message:
                span.add_event("control_chars_stripped", {
                    "original_len": len(original),
                    "sanitized_len": len(ctx.user_message),
                })

            # 2. Check message length
            ok, msg = input_guard.check_length(ctx.user_message)
            if not ok:
                if span:
                    span.add_event("length_blocked", {"message": msg})
                _finish_span(span, "ok")
                yield {"type": "error", "data": msg}
                return

            # 3. Detect prompt injection (regex 层)
            clean, pattern, msg = input_guard.detect_injection(ctx.user_message)

            # ── Phase 4C: LLM-judge 复核层（注入检测第三层） ──
            # regex 未命中时由 judge 复核语义级注入（regex 命中直接拦，
            # judge 只查 regex 漏网的）。InputGuard.detect_injection 是同步
            # 的，judge 是 async —— 所以在这里（stage）await 调用。
            if clean:
                judge = getattr(guardrails, "judge", None)
                if judge is not None and ctx.user_message.strip():
                    llm = _resolve_judge_llm(ctx)
                    judged_safe = await judge.check(ctx.user_message, llm)
                    if not judged_safe:
                        # 与 regex 命中同权处理：block_injection 决定拦/放
                        logger.warning("LLM judge flagged user message as unsafe")
                        if span:
                            span.add_event("injection_blocked", {"pattern": "llm_judge"})
                        _finish_span(span, "ok")
                        if getattr(input_guard, "_block_injection", True):
                            yield {"type": "error", "data": _INJECTION_BLOCKED_MSG}
                            return
                        # Audit mode — log but don't block
                        logger.info(
                            "Judge-flagged message allowed through "
                            "(block_injection=False)"
                        )

            if not clean:
                if span:
                    span.add_event("injection_blocked", {"pattern": pattern})
                _finish_span(span, "ok")
                yield {"type": "error", "data": msg}
                return

        _finish_span(span, "ok")
        async for event in next(ctx):
            yield event


class StreamObservabilityStage(StreamPipelineStage):
    """Time the full downstream chain and log per-iteration metrics."""

    async def execute(self, ctx, next):
        span = _start_span(ctx, "ObservabilityStage")
        start = time.time()

        async for event in next(ctx):
            yield event

        elapsed = time.time() - start
        ctx.metadata["elapsed_seconds"] = elapsed

        if span:
            span.add_event("iteration_complete", {
                "elapsed_s": round(elapsed, 3),
                "stop_reason": ctx.stop_reason,
            })

        logger.info(
            "Pipeline iteration: elapsed=%.3fs iteration=%d stop_reason=%s",
            elapsed,
            ctx.iteration,
            ctx.stop_reason,
        )
        _finish_span(span, "ok")


class StreamContextBuildStage(StreamPipelineStage):
    """Build the message list from session state (system prompt + history).

    Also persists the user message to session history — at this point the
    input guardrail (StreamInputGuardStage) has already passed, so we know
    the message is safe to store.
    """

    async def execute(self, ctx, next):
        span = _start_span(ctx, "ContextBuildStage")

        # Persist the user message NOW — after InputGuard has cleared it.
        # Only add on the first iteration; subsequent iterations are tool-call
        # continuations within the same turn.
        if ctx.iteration == 0:
            user_msg: dict = {"role": "user", "content": ctx.user_message}
            await ctx.session.state.add_message(user_msg)

        ctx.messages = ctx.session.state.build_messages()

        if span:
            span.add_event("messages_built", {"message_count": len(ctx.messages)})

        _finish_span(span, "ok")
        async for event in next(ctx):
            yield event


class StreamContextGuardStage(StreamPipelineStage):
    """Compact context window if token budget is exceeded."""

    async def execute(self, ctx, next):
        span = _start_span(ctx, "ContextGuardStage")

        # FSM: enter compacting (briefly)
        _try_transition(ctx.session.fsm, SessionPhase.COMPACTING)

        ctx.session.state = await ctx.session.context_guard.check_and_compact(
            ctx.session.state
        )

        # FSM: back to active
        _try_transition(ctx.session.fsm, SessionPhase.ACTIVE)

        if span:
            span.add_event("compaction_checked", {
                "message_count": len(ctx.session.state.messages),
            })

        _finish_span(span, "ok")
        async for event in next(ctx):
            yield event


# ---------------------------------------------------------------------------
# LLM + Tool stages  (the heart of the streaming harness)
# ---------------------------------------------------------------------------

class StreamLLMCallStage(StreamPipelineStage):
    """Invoke the LLM in streaming mode.

    Yields *token* events upward as they arrive.  When the stream ends,
    this stage records ``tool_calls`` / ``stop_reason`` on *ctx* and
    chains to the downstream stages (ToolExecution → Terminal) so they
    can emit status, tool_result, and the final done event.

    Phase 4C streaming redaction (安全 P0 #12): every token passes through
    a :class:`~ant.core.guardrails.StreamRedactor` (one per LLM call) —
    ``data`` is fed first, only the non-empty safe part is yielded, and the
    remaining buffer is flushed (redacted) before the error/done events
    (先 flush 补尾，后 done).  ``redact_buffer_chars`` == 0 disables the
    redactor entirely (直通).  ``ctx.response_content`` accumulates the
    *redacted* text so persisted history matches what the user saw.
    """

    async def execute(self, ctx, next):
        span = _start_span(ctx, "LLMCallStage")

        # Reset accumulated content for this LLM call
        ctx.response_content = ""
        ctx.tool_calls = []
        ctx.stop_reason = ""
        _first_token = False  # track TTFT within this call
        _call_start = time.time()

        # ── Phase 4C 流式脱敏（安全 P0 #12：token 先出后审 → 延迟换覆盖） ──
        # 每个 LLM 调用建一个 redactor：token 先 feed 再 yield 非空部分，
        # done/error 前 flush 剩余缓冲并 yield 补尾。redact_buffer_chars=0
        # 时直通（不建 redactor，token 原样转发）。
        redactor = _build_stream_redactor(ctx.session.shared_context)

        async for chunk in ctx.session.agent.llm.stream_chat(
            ctx.messages, ctx.tool_schemas
        ):
            event_type = chunk.get("type")

            if event_type == "token":
                if not _first_token:
                    _first_token = True
                    ttft_ms = (time.time() - _call_start) * 1000
                    ctx.metadata["ttft_ms"] = ttft_ms
                    if span:
                        span.add_event("first_token", {"ttft_ms": round(ttft_ms, 1)})

                token_data = chunk["data"]
                safe = (
                    redactor.feed(token_data)
                    if redactor is not None
                    else token_data
                )
                ctx.response_content += safe
                if safe:
                    # 空返回 = 整段 token 仍被缓冲（未过 buffer_size 或
                    # 跨 chunk 密钥被压住），等 flush 时再补尾。
                    yield {"type": "token", "data": safe}

            elif event_type == "tool_calls":
                ctx.tool_calls = chunk["data"]
                ctx.stop_reason = "tool_calls"
                tool_names = [tc.name for tc in ctx.tool_calls]

                # FSM: waiting for tool execution
                _try_transition(ctx.session.fsm, SessionPhase.WAITING_TOOL)

                if span:
                    span.add_event("tool_calls_requested", {
                        "tools": tool_names,
                        "count": len(ctx.tool_calls),
                    })

                yield {
                    "type": "status",
                    "data": f"⏳ 调用工具: {', '.join(tool_names)}",
                }

            elif event_type == "done":
                ctx.stop_reason = chunk.get("finish_reason", "stop")
                if span:
                    span.add_event("llm_done", {
                        "finish_reason": ctx.stop_reason,
                        "response_length": len(ctx.response_content),
                    })

            elif event_type == "usage":
                # Token/cost accounting event (llm-layer 协议)：不转发给前端，
                # 交给 ctx.usage_recorder（若已注入）。记账失败只 debug 日志，
                # 绝不能打断流式输出。
                if ctx.usage_recorder is not None:
                    try:
                        await ctx.usage_recorder(chunk["data"])
                    except Exception:
                        logger.debug("usage recording failed", exc_info=True)

            elif event_type == "error":
                ctx.stop_reason = "error"
                if span:
                    span.add_event("llm_error", {"error": chunk.get("data")})
                # 先 flush 剩余缓冲（已脱敏）并 yield 补尾，再转发 error。
                if redactor is not None:
                    tail = redactor.flush()
                    if tail:
                        ctx.response_content += tail
                        yield {"type": "token", "data": tail}
                _finish_span(span, "error")
                yield chunk  # forward error to frontend
                return       # don't chain downstream on error

        # 流结束：flush 剩余缓冲并 yield 补尾，之后才链到下游
        # （done 事件由 TerminalStage 发出 —— 先 flush 后 done）。
        if redactor is not None:
            tail = redactor.flush()
            if tail:
                ctx.response_content += tail
                yield {"type": "token", "data": tail}

        _finish_span(span, "ok")

        # Chain to downstream stages (ToolExecution → Terminal)
        async for event in next(ctx):
            yield event


class StreamToolExecutionStage(StreamPipelineStage):
    """Execute tool calls when the LLM requests them.

    Yields ``status`` and ``tool_result`` events to the frontend, then
    adds the assistant message + tool-result messages to session state so
    the next pipeline iteration sees the updated conversation.

    Execution plan（简单两步法 + 两道闸门）：
      1. ``require_confirmation`` 策略命中的工具逐个串行执行，每个都等待
         用户批准——确认审批保持逐工具生效，被确认工具永远不进入并行组；
      2. 写类工具（write/edit/bash）按 LLM 返回顺序逐个串行执行——
         确定性优先，避免同文件写竞态；
      3. 只读工具用 ``asyncio.gather`` 并行执行，并发上限
         ``ctx.max_parallel_tools``（asyncio.Semaphore 闸门）。

    当 ``ctx.parallel_writes`` 为 True 时，写类工具并入并行组（跳过第 2 步）。
    每个工具的执行都受 ``ctx.tool_timeout`` 约束：超时返回 LLM 可感知的
    错误串，单个工具失败/超时不会中断其他工具。结果按原 ``tool_calls``
    顺序组装（结尾 zip 逻辑不变，并行执行但顺序还原）。
    """

    async def execute(self, ctx, next):
        if ctx.stop_reason == "tool_calls" and ctx.tool_calls:
            span = _start_span(ctx, "ToolExecutionStage")

            tool_results: list[str] = [""] * len(ctx.tool_calls)

            # ── Human-in-the-Loop: require confirmation for high-privilege tools ──
            policy = None
            if ctx.session.tools and ctx.session.tools._governance:
                policy = ctx.session.tools._governance.policy

            def _needs_confirmation(tc) -> bool:
                return bool(policy and tc.name in policy.require_confirmation)

            confirmed: list[tuple[int, Any]] = []
            pending: list[tuple[int, Any]] = []
            for idx, tc in enumerate(ctx.tool_calls):
                (confirmed if _needs_confirmation(tc) else pending).append((idx, tc))

            # 剩余工具再分写类（串行）与只读（并行）。parallel_writes=True 时
            # 写类也并入并行组；被确认工具始终先串行处理完，再并行其余。
            if ctx.parallel_writes:
                write_tools: list[tuple[int, Any]] = []
                read_tools: list[tuple[int, Any]] = pending
            else:
                write_tools = [(i, tc) for i, tc in pending if tc.name in _WRITE_TOOLS]
                read_tools = [(i, tc) for i, tc in pending if tc.name not in _WRITE_TOOLS]

            async def _run_tool(tc) -> str:
                """执行单个工具，受 ctx.tool_timeout 硬超时约束。

                超时转错误串（LLM 可感知）；_execute_tool_call 已兜底工具
                内部异常，半途失败不会中断其他工具。
                """
                try:
                    result = await asyncio.wait_for(
                        ctx.session._execute_tool_call(tc), timeout=ctx.tool_timeout
                    )
                except asyncio.TimeoutError:
                    result = f"Tool {tc.name} timed out after {ctx.tool_timeout:g}s"

                # ── Tool result injection scan ──
                guardrails = ctx.session.shared_context.guardrails
                if guardrails and guardrails.output:
                    result = guardrails.output.scan_tool_result(result)
                # ──────────────────────────────────
                return result

            # ── 1) 被确认工具：逐个串行 + 逐工具审批流（行为不变） ──
            for idx, tc in confirmed:
                tool_span = _start_span(ctx, f"ToolExecution:{tc.name}")

                yield {"type": "status", "data": f"⏳ 执行中: {tc.name}…"}
                yield {"type": "status", "data": f"⏳ 等待批准: {tc.name}…"}

                broker = ctx.session.shared_context.confirmation_broker
                approved = await broker.request_approval(
                    session_id=ctx.session.session_id,
                    tool_name=tc.name,
                    tool_args=tc.arguments,
                    context=ctx.session.shared_context,
                    agent_id=(
                        ctx.session.agent.agent_def.id if ctx.session.agent else ""
                    ),
                )

                if not approved:
                    result = (
                        f"Tool call denied: the user did not approve "
                        f"the execution of '{tc.name}'."
                    )
                    tool_results[idx] = result
                    if tool_span:
                        tool_span.add_event("confirmation_denied", {"tool": tc.name})
                        _finish_span(tool_span, "ok")
                    continue

                result = await _run_tool(tc)
                tool_results[idx] = result
                if tool_span:
                    tool_span.add_event("tool_result_length", {"length": len(result)})
                    _finish_span(tool_span, "ok")

                # Truncate long results for display
                brief = result[:200] + "…" if len(result) > 200 else result
                yield {"type": "tool_result", "data": {"name": tc.name, "result": brief}}

            # ── 2) 写类工具：逐个串行（顺序与 LLM 返回一致，保证确定性） ──
            for idx, tc in write_tools:
                tool_span = _start_span(ctx, f"ToolExecution:{tc.name}")

                yield {"type": "status", "data": f"⏳ 执行中: {tc.name}…"}

                result = await _run_tool(tc)
                tool_results[idx] = result
                if tool_span:
                    tool_span.add_event("tool_result_length", {"length": len(result)})
                    _finish_span(tool_span, "ok")

                # Truncate long results for display
                brief = result[:200] + "…" if len(result) > 200 else result
                yield {"type": "tool_result", "data": {"name": tc.name, "result": brief}}

            # ── 3) 只读工具：gather 并行，Semaphore 限流 max_parallel_tools ──
            if read_tools:
                semaphore = asyncio.Semaphore(max(1, ctx.max_parallel_tools))
                tool_spans = {
                    idx: _start_span(ctx, f"ToolExecution:{tc.name}")
                    for idx, tc in read_tools
                }
                for _, tc in read_tools:
                    yield {"type": "status", "data": f"⏳ 执行中: {tc.name}…"}

                async def _gated(tc) -> str:
                    async with semaphore:
                        return await _run_tool(tc)

                # return_exceptions=True：并行组里单个异常转错误串，不中断其他工具。
                outcomes = await asyncio.gather(
                    *(_gated(tc) for _, tc in read_tools),
                    return_exceptions=True,
                )

                for (idx, tc), outcome in zip(read_tools, outcomes):
                    if isinstance(outcome, Exception):
                        result = f"Tool {tc.name} failed: {outcome}"
                    else:
                        result = outcome
                    tool_results[idx] = result
                    tool_span = tool_spans[idx]
                    if tool_span:
                        tool_span.add_event("tool_result_length", {"length": len(result)})
                        _finish_span(tool_span, "ok")

                    # Truncate long results for display
                    brief = result[:200] + "…" if len(result) > 200 else result
                    yield {
                        "type": "tool_result",
                        "data": {"name": tc.name, "result": brief},
                    }

            # Record the assistant turn (with tool_calls) in session state
            assistant_msg: dict = {
                "role": "assistant",
                "content": ctx.response_content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": tc.arguments,
                        },
                    }
                    for tc in ctx.tool_calls
                ],
            }
            await ctx.session.state.add_message(assistant_msg)

            # Record each tool result (zip 还原 LLM 原始顺序，与执行顺序无关)
            for tc, result in zip(ctx.tool_calls, tool_results):
                await ctx.session.state.add_message({
                    "role": "tool",
                    "content": result,
                    "tool_call_id": tc.id,
                })

            ctx.iteration += 1

            # FSM: tools done, back to active for next LLM round
            _try_transition(ctx.session.fsm, SessionPhase.ACTIVE)

            if span:
                span.add_event("tools_executed", {
                    "tool_count": len(ctx.tool_calls),
                })
                _finish_span(span, "ok")

        # Chain to terminal stage
        async for event in next(ctx):
            yield event


class StreamOutputGuardStage(StreamPipelineStage):
    """Sanitize agent output before it is persisted to session history.

    Operates on ``ctx.response_content`` after the LLM and any tool calls
    have completed, but **before** TerminalStage persists the response.

    Three layers:
      1. Redact secrets (API keys, tokens, private keys)
      2. Check output length (truncate if needed)
      3. Check content policy (replace blocked content)
    """

    async def execute(self, ctx, next):
        span = _start_span(ctx, "OutputGuardStage")
        guardrails = ctx.session.shared_context.guardrails

        # Pre-work: sanitize ctx.response_content before TerminalStage persists it
        if (
            guardrails
            and guardrails.output
            and ctx.stop_reason != "tool_calls"
            and ctx.response_content.strip()
        ):
            output_guard = guardrails.output

            # 1. Redact secrets
            original_len = len(ctx.response_content)
            ctx.response_content = output_guard.redact_secrets(ctx.response_content)
            if span and len(ctx.response_content) != original_len:
                span.add_event("secrets_redacted", {})

            # 2. Check / enforce output length
            ok, msg = output_guard.check_length(ctx.response_content)
            if not ok:
                max_len = output_guard._max_length
                ctx.response_content = (
                    ctx.response_content[:max_len]
                    + f"\n\n[Truncated at {max_len:,} chars]"
                )
                if span:
                    span.add_event("length_truncated", {"max_length": max_len})

            # 3. Check content policy
            clean, pattern, msg = output_guard.check_policy(ctx.response_content)
            if not clean:
                ctx.response_content = (
                    f"[Response blocked by content policy: {msg}]"
                )
                if span:
                    span.add_event("content_blocked", {"pattern": pattern})

        _finish_span(span, "ok")

        # Chain to TerminalStage (which persists the sanitized content)
        async for event in next(ctx):
            yield event


class StreamTerminalStage(StreamPipelineStage):
    """Emit the final ``done`` event (only when we are truly finished).

    During a tool-call iteration this stage stays silent so the outer
    ``StreamPipeline.run()`` loop can re-run the chain without the
    frontend seeing a spurious done event.
    """

    async def execute(self, ctx, next):
        span = _start_span(ctx, "TerminalStage")

        if ctx.stop_reason != "tool_calls":
            # Persist the final assistant response to session history.
            # (Tool-call assistant messages are saved in StreamToolExecutionStage;
            # this catches the final text-only response.)
            if ctx.response_content.strip():
                await ctx.session.state.add_message({
                    "role": "assistant",
                    "content": ctx.response_content,
                })
            if span:
                span.add_event("final_done", {"finish_reason": ctx.stop_reason})
            _finish_span(span, "ok")
            yield {"type": "done", "finish_reason": ctx.stop_reason}
        else:
            _finish_span(span, "ok")
        # When stop_reason IS "tool_calls", remain silent — the
        # StreamPipeline outer loop will re-run the full chain.
