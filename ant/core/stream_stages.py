"""Streaming pipeline stages for harness-mode streaming chat.

Each stage is an async-generator so per-token events flow without buffering.
Stages integrate with SessionFSM (lifecycle) and ExecutionTracer (observability).
"""

from __future__ import annotations

import logging
import time

from ant.core.session_fsm import SessionPhase
from ant.core.stream_pipeline import StreamPipelineStage

logger = logging.getLogger(__name__)


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
            if span:
                span.add_event("max_iterations_reached", {"iteration": ctx.iteration})
            _finish_span(span, "ok")
            yield {"type": "done", "finish_reason": "stop"}
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
    """Build the message list from session state (system prompt + history)."""

    async def execute(self, ctx, next):
        span = _start_span(ctx, "ContextBuildStage")

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
    """

    async def execute(self, ctx, next):
        span = _start_span(ctx, "LLMCallStage")

        # Reset accumulated content for this LLM call
        ctx.response_content = ""
        ctx.tool_calls = []
        ctx.stop_reason = ""

        async for chunk in ctx.session.agent.llm.stream_chat(
            ctx.messages, ctx.tool_schemas
        ):
            event_type = chunk.get("type")

            if event_type == "token":
                ctx.response_content += chunk["data"]
                yield chunk  # forward token to frontend

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

            elif event_type == "error":
                ctx.stop_reason = "error"
                if span:
                    span.add_event("llm_error", {"error": chunk.get("data")})
                _finish_span(span, "error")
                yield chunk  # forward error to frontend
                return       # don't chain downstream on error

        _finish_span(span, "ok")

        # Chain to downstream stages (ToolExecution → Terminal)
        async for event in next(ctx):
            yield event


class StreamToolExecutionStage(StreamPipelineStage):
    """Execute tool calls when the LLM requests them.

    Yields ``status`` and ``tool_result`` events to the frontend, then
    adds the assistant message + tool-result messages to session state so
    the next pipeline iteration sees the updated conversation.
    """

    async def execute(self, ctx, next):
        if ctx.stop_reason == "tool_calls" and ctx.tool_calls:
            span = _start_span(ctx, "ToolExecutionStage")

            tool_results: list[str] = []
            for tc in ctx.tool_calls:
                tool_span = _start_span(ctx, f"ToolExecution:{tc.name}")

                yield {
                    "type": "status",
                    "data": f"⏳ 执行中: {tc.name}…",
                }

                result = await ctx.session._execute_tool_call(tc)
                tool_results.append(result)

                if tool_span:
                    tool_span.add_event("tool_result_length", {
                        "length": len(result),
                    })
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
            ctx.session.state.add_message(assistant_msg)

            # Record each tool result
            for tc, result in zip(ctx.tool_calls, tool_results):
                ctx.session.state.add_message({
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
                ctx.session.state.add_message({
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
