"""Streaming pipeline with middleware-style async-generator stages.

each stage is an async generator, allowing
per-token events (token, tool_result, status, done, error) to flow
through the chain without buffering.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable

if TYPE_CHECKING:
    from ant.core.agent import AgentSession
    from ant.core.tracer import Trace
    from litellm.types.completion import ChatCompletionMessageParam as Message


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
    iteration: int = 0
    max_iterations: int = 10
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
        """
        while True:
            async for event in self._execute_chain(0, ctx):
                yield event

            if ctx.stop_reason == "tool_calls":
                # ToolExecutionStage has already added tool results to
                # session state — re-run the full pipeline so the LLM
                # sees them in the next turn.
                continue

            # Any other stop reason (stop, length, content_filter, error)
            # means we are done.
            break

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
