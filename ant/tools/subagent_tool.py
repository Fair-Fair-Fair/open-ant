"""Subagent dispatch tool facroty for creating dynamic dispatch tool"""

import asyncio
import json
import time
from typing import TYPE_CHECKING

from ant.core.events import AgentEventSource, DispatchEvent, DispatchResultEvent
from ant.tools.base import BaseTool, tool
from ant.utils.def_loader import DefNotFoundError

if TYPE_CHECKING:
    from ant.core.agent import AgentSession
    from ant.core.context import SharedContext

# 子代理 dispatch 结果等待超时（秒）——模块级默认值。
# 子代理启动异常 / worker 繁忙 / 事件丢失时，避免主 agent 永久挂死。
# LLM 可通过 timeout_seconds 参数（10~600）按任务复杂度覆盖。
SUBAGENT_DISPATCH_TIMEOUT_SECONDS = 180.0

# timeout_seconds 参数允许的最大值（秒），与 schema 的 maximum 一致。
# 只封顶、不下限：幻觉出的大数值不能击穿超时安全网；
# 小于 10 的值仅让等待更短（不会挂死），故直接放行。
SUBAGENT_DISPATCH_TIMEOUT_MAX_SECONDS = 600.0


def create_subagent_dispatch_tool(
        current_agent_id: str,
        context: "SharedContext"
) -> BaseTool | None:
    """Factory to create subagent dispatch tool with dynamic schema"""

    # Discover available agents, exclude current
    shared_context = context
    available_agents = shared_context.agent_loader.discover_agents()
    dispatchable_agents = [a for a in available_agents if a.id != current_agent_id]

    if not dispatchable_agents:
        return None

    # Build description listing avaiable agents
    agents_desc = "<available_agents>\n"
    for agent_def in dispatchable_agents:
        agents_desc += f'  <agent id="{agent_def.id}">{agent_def.description}</agent>\n'
    agents_desc += "</available_agents>"

    dispatchable_ids = [a.id for a in dispatchable_agents]

    @tool(
        name="subagent_dispatch",
        description=f"Dispatch a task to a specialized subagent.\n{agents_desc}",
        parameters={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "ID of the subagent to dispatch the task to",
                    "enum": dispatchable_ids
                },
                "task": {
                    "type": "string",
                    "description": "The task to dispatch to the subagent"
                },
                "context": {
                    "type": "string",
                    "description": "Optional context information for the subagent"
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": (
                        "Optional execution budget in seconds for the "
                        "subagent to complete (10-600, default 180). "
                        "Raise it for complex tasks."
                    ),
                    "minimum": 10,
                    "maximum": 600
                }
            },
            "required": ["agent_id", "task"]
        }
    )
    async def subagent_dispatch(
            agent_id: str,
            task: str,
            session: "AgentSession",
            context: str = "",
            timeout_seconds: int = SUBAGENT_DISPATCH_TIMEOUT_SECONDS,
    ) -> str:
        """Dispatch task to subagent, return result + session_id.

        Budget (timeout_seconds):
            - Optional per-call budget, 10-600s, default 180s
              (SUBAGENT_DISPATCH_TIMEOUT_SECONDS). Larger values for
              complex tasks.
            - Defensive coercion: non-numeric values fall back to the
              default; values above 600 are capped so a hallucinated
              budget cannot defeat the timeout safety net; values below
              10 pass through (they only shorten the wait).
            - On timeout, an explicit error string is returned and the
              local result future is cancelled.

        Cancellation propagation boundary (Phase 4 expansion point):
            - The combined wait task (result_future + wait_for) is
              registered on ``session._pending_tasks`` when that
              attribute exists, so a session-level cancellation can tear
              down the local wait promptly; the task is removed from the
              list once it finishes.
            - When this tool's own task is cancelled, the local wait is
              cancelled explicitly (after unsubscribe, before re-raise)
              so no zombie wait_task lingers until the default timeout.
              Note: a plain ``await wait_task`` does NOT propagate
              cancellation to the inner task, hence the explicit cancel.
            - There is NO worker-side cancellation yet: the subagent
              keeps running on its worker and a late DispatchResultEvent
              is simply ignored (the handler checks result_future.done()).
              Full end-to-end cancellation (a cancel notification event
              consumed by the subagent worker) is planned for Phase 4.
        """
        # varity agent exists and create session
        from ant.core.agent import Agent
        try:
            agent_def = shared_context.agent_loader.load(agent_id)
        except DefNotFoundError:
            raise ValueError(f"Agent '{agent_id}' not found")

        agent = Agent(agent_def, shared_context)
        agent_source = AgentEventSource(agent_id=current_agent_id)
        agent_session = await agent.new_session(agent_source)
        session_id = agent_session.session_id

        user_message = task
        if context:
            user_message = f"{task}\n\nContext:\n{context}"

        # Coerce the LLM-provided budget defensively, then cap it at the
        # schema maximum (the safety-relevant bound).
        try:
            timeout_seconds = float(timeout_seconds)
        except (TypeError, ValueError):
            timeout_seconds = SUBAGENT_DISPATCH_TIMEOUT_SECONDS
        if timeout_seconds > SUBAGENT_DISPATCH_TIMEOUT_MAX_SECONDS:
            timeout_seconds = SUBAGENT_DISPATCH_TIMEOUT_MAX_SECONDS

        loop = asyncio.get_running_loop()
        result_future: asyncio.Future[str] = loop.create_future()

        # Create temp handler that filters by session_id
        async def handle_result(event: DispatchResultEvent) -> None:
            if event.session_id == session_id:
                if not result_future.done():
                    if event.error:
                        result_future.set_exception(Exception(event.error))
                    else:
                        result_future.set_result(event.content)

        # Subscribe to DispatchResultEvent events
        shared_context.eventbus.subscribe(DispatchResultEvent, handle_result)

        wait_task: asyncio.Task | None = None
        try:
            # Publish DISPATCH event
            event = DispatchEvent(
                session_id=session_id,
                source=AgentEventSource(agent_id=current_agent_id),
                content=user_message,
                timestamp=time.time(),
                parent_session_id=session.session_id
            )
            await shared_context.eventbus.publish(event)

            # wait for result, with timeout so a stuck subagent
            # cannot hang the main agent's turn forever. The combined
            # task is registered on the session so a session-level
            # cancellation (Phase 4) can tear down this wait locally.
            wait_task = asyncio.create_task(
                asyncio.wait_for(result_future, timeout=timeout_seconds)
            )
            pending = getattr(session, "_pending_tasks", None)
            if pending is not None:
                pending.append(wait_task)

                def _drop_pending(_task: asyncio.Task) -> None:
                    try:
                        pending.remove(wait_task)
                    except ValueError:
                        pass

                wait_task.add_done_callback(_drop_pending)
            response = await wait_task
        except asyncio.TimeoutError:
            return (
                "Subagent dispatch timed out after "
                f"{timeout_seconds}s"
            )
        except asyncio.CancelledError:
            # Unsubscribe BEFORE re-raising so the temp handler never
            # leaks after the main turn is cancelled.
            shared_context.eventbus.unsubscribe(handle_result)
            # Tear down the local wait explicitly: a plain await does not
            # cancel the inner task, and a lingering wait_task would
            # otherwise stay pending until the default timeout. True
            # worker-side cancellation is Phase 4.
            if wait_task is not None and not wait_task.done():
                wait_task.cancel()
            raise
        finally:
            # ALways unsubscribe (idempotent — covers all paths)
            shared_context.eventbus.unsubscribe(handle_result)

        result = {
            "response": response,
            "session_id": session_id
        }
        return json.dumps(result)
    return subagent_dispatch
