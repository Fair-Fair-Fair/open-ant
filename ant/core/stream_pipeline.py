"""Streaming pipeline with middleware-style async-generator stages.

each stage is an async generator, allowing
per-token events (token, tool_result, status, done, error) to flow
through the chain without buffering.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncGenerator, Awaitable, Callable

if TYPE_CHECKING:
    from litellm.types.completion import ChatCompletionMessageParam as Message

    from ant.core.agent import AgentSession
    from ant.core.tracer import Trace


@dataclass
class PipelineContext:
    """Carries state through the pipeline stages."""

    session: "AgentSession"
    user_message: str
    messages: list["Message"] = field(default_factory=list)
    tool_schemas: list[dict] = field(default_factory=list)
    response_content: str = ""
    tool_calls: list[Any] = field(default_factory=list)
    stop_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    iteration: int = 0  # 在 StreamToolExecutionStage 阶段，每当成功执行完一组tool_calls后，会执行 ctx.iteration += 1。  # noqa: E501
    max_iterations: int = 10  # Agent 最多可以连续进行 10 轮“AI思考→调用一组工具→执行完毕→AI继续思考”的循环。  # noqa: E501
    max_parallel_tools: int = 8  # 只读工具并行执行上限（asyncio.Semaphore 闸门）。llm-layer 从 config 注入。  # noqa: E501
    tool_timeout: float = 120.0  # 单个工具执行超时（秒）；超时返回 LLM 可感知的错误串。llm-layer 从 config 注入。  # noqa: E501
    parallel_writes: bool = False  # True 时写类工具（write/edit/bash）并入并行组。llm-layer 从 config 注入。  # noqa: E501
    usage_recorder: Callable[[dict], Awaitable[None]] | None = None  # llm-layer 注入的 usage 记账回调（消费 {"type":"usage"} 事件时调用）。  # noqa: E501
    aborted: bool = False  # 断流标志：客户端中途断开/任务被取消时置 True，供 pipeline 外层日志使用。  # noqa: E501
    start_time: float = field(default_factory=time.time)
    trace: "Trace | None" = None

logger = logging.getLogger(__name__)


class StreamPipelineStage(ABC):
    """Base class for streaming pipeline stages.

    Each stage is an async generator: it receives ``ctx`` and a ``next``
    callback that returns an async generator of downstream events.  Stages
    do pre-processing, then iterate over ``next(ctx)``, yielding every
    downstream event upward, and may add post-processing afterward.

    A stage that wants to short-circuit (e.g. validation failure) simply
    yields its own terminal event and returns without calling ``next``.
    """

    @abstractmethod
    async def execute(
        self,
        ctx: PipelineContext,
        next: Callable[[PipelineContext], AsyncGenerator[dict, None]],
    ) -> AsyncGenerator[dict, None]:
        ...


class StreamPipeline:
    """Ordered streaming pipeline with middleware chaining.

    Identical in spirit to ``Pipeline`` but every stage is an async
    generator so events stream through without buffering.  The outer
    ``run()`` loop handles tool-call iterations automatically — when
    ``ctx.stop_reason == "tool_calls"`` after a full chain execution the
    pipeline re-runs all stages so the LLM can continue with tool results
    in context.
    """

    def __init__(self) -> None:
        self._stages: list[StreamPipelineStage] = []

    def add_stage(self, stage: StreamPipelineStage) -> None:
        """Append a stage to the pipeline (executed in insertion order)."""
        self._stages.append(stage)

    async def run(
        self, ctx: PipelineContext
    ) -> AsyncGenerator[dict, None]:
        """Execute all stages; loop when tool calls require a follow-up turn.

        Yields every streaming event (token, status, tool_result, error,
        done) produced by the chain so the caller can forward them to the
        frontend.

        Stream-cut-off fallback: if the consumer closes the generator
        (``aclose()`` → GeneratorExit) or the consuming task is cancelled
        (CancelledError) before the terminal ``done`` event was delivered,
        the user's message is left unanswered — a placeholder assistant
        reply is persisted so the session can continue, then the original
        exception is re-raised.
        """
        terminal_emitted = False
        while True:
            try:
                async for event in self._execute_chain(0, ctx):
                    if event.get("type") == "done":
                        terminal_emitted = True
                    yield event
            except (GeneratorExit, asyncio.CancelledError):
                # GeneratorExit arrives at the yield point on aclose();
                # CancelledError when the consuming task is cancelled.
                ctx.aborted = True
                await self._persist_interrupted_placeholder(ctx, terminal_emitted)
                raise

            if ctx.stop_reason == "tool_calls":
                # ToolExecutionStage has already added tool results to
                # session state — re-run the full pipeline so the LLM
                # sees them in the next turn.
                continue

            # Any other stop reason (stop, length, content_filter, error)
            # means we are done.
            break

    async def _persist_interrupted_placeholder(
        self, ctx: PipelineContext, terminal_emitted: bool
    ) -> None:
        """Persist a placeholder assistant reply after a cut-off stream.

        Only when the conversation genuinely lacks an assistant reply:
        either the response had content but the terminal ``done`` event
        was never delivered, or the last persisted message is still the
        user's (no reply of any kind was recorded yet).
        """
        last_msg = (
            ctx.session.state.messages[-1]
            if ctx.session.state.messages
            else None
        )
        needs_placeholder = (
            (not terminal_emitted and ctx.response_content)
            or (last_msg is not None and last_msg.get("role") == "user")
        )
        if needs_placeholder:
            await ctx.session.state.add_message({
                "role": "assistant",
                "content": "⚠️ 响应中断：连接被断开，请重新发送消息继续。",
            })

    async def _execute_chain(
        self,
        index: int,
        ctx: PipelineContext,
    ) -> AsyncGenerator[dict, None]:
        """Recursively build and execute the middleware chain."""
        if index >= len(self._stages):
            return

        stage = self._stages[index]

        async def _next(c: PipelineContext) -> AsyncGenerator[dict, None]:
            async for event in self._execute_chain(index + 1, c):
                yield event

        async for event in stage.execute(ctx, _next):
            yield event
