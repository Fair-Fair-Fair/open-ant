"""Agent worker for executing agent jobs
Agent 工作模块 —— 负责接收入站事件并将其分发给对应的 Agent 会话执行器。
核心职责：
  1. 监听事件总线上的 InboundEvent（用户消息等入站事件）
  2. 根据事件中的 session_id 查找对应的 Agent 定义
  3. 创建/恢复 Agent 会话，执行聊天或斜杠命令
  4. 将结果通过 OutboundEvent 发布回事件总线
  5. 对异常会话进行有限次数的自动重试
"""
import asyncio
import logging
from dataclasses import replace  # 用于不可变 dataclass 的字段替换（如重试时递增 retry_count）
from typing import Union, TYPE_CHECKING

from .worker import SubscribeWorker  # 基类：提供事件订阅型 Worker 的基础能力
from ant.core.agent import Agent  # Agent 核心类，封装 LLM 对话逻辑
from ant.core.events import (
    InboundEvent, OutboundEvent, AgentEventSource,
    DispatchEvent, DispatchResultEvent, StreamChunkEvent,
)

from ant.utils.def_loader import DefNotFoundError  # Agent/Skill 定义加载失败的异常

if TYPE_CHECKING:
    from ant.core.context import SharedContext
    from ant.core.agent import AgentDef

# 会话失败后的最大重试次数；超过此次数将直接返回错误给用户
MAX_RETRIES = 3

logger = logging.getLogger(__name__)

ProcessableEvent = Union[InboundEvent, DispatchEvent]


class AgentWorker(SubscribeWorker):
    """Dispatches events to session executors
    事件分发器：订阅 InboundEvent，为每个事件创建异步任务来执行 Agent 会话。
    继承自 SubscribeWorker，具备 Worker 的生命周期管理能力。
    """

    def __init__(self, context: "SharedContext") -> None:
        """初始化 AgentWorker
        Args:
            context: 全局上下文对象，包含 eventbus、history_store、agent_loader、command_registry 等共享组件
        """
        super().__init__(context)

        self._semaphores: dict[str, asyncio.Semaphore] = {}

        # 在事件总线中订阅 InboundEvent 类型的事件，回调函数为 dispatch_event
        # 每当有用户消息等入站事件时，都会触发 dispatch_event
        self.context.eventbus.subscribe(InboundEvent, self.dispatch_event)
        self.context.eventbus.subscribe(DispatchEvent, self.dispatch_event)
        self.logger.info("AgentWorker subscribed to InboundEvent and DispatchEvent events")

    async def dispatch_event(self, event: InboundEvent) -> None:
        """Create executor task for typed event.
        事件分发入口：根据事件中的 session_id 查找会话信息，加载对应 Agent 定义，
        然后以异步任务的方式启动会话执行（不阻塞当前事件循环）。

        流程：
          1. 通过 session_id 从 history_store 获取会话元信息（包含 agent_id）
          2. 通过 agent_id 从 agent_loader 加载 Agent 定义（prompt、工具列表等）
          3. 创建异步任务 exec_session 来执行实际的对话逻辑

        Args:
            event: 入站事件，包含 session_id、content（用户消息）、retry_count 等字段
        """
        # 从历史记录存储中获取会话信息，session_info 中包含 agent_id（标识使用哪个 Agent）
        session_info = self.context.history_store.get_session_info(event.session_id)

        if session_info:
            agent_id = session_info.agent_id
        else:
            logger.warning(f"Session not found: {event.session_id}, falling back to routing")
            agent_id = self.context.routing_table.resolve(str(event.source))

        try:
            # 加载 Agent 定义（包含 system prompt、可用工具、模型配置等）
            agent_def = self.context.agent_loader.load(agent_id)
        except DefNotFoundError as e:
            # Agent 定义不存在时，向用户返回错误信息并终止处理
            logger.error(f"Agent not found: {agent_id}: {e}")
            return await self._emit_response(
                event,
                "",
                agent_def.id,
                str(e)
            )

        # 以异步任务方式启动会话执行
        # 使用 create_task 而非 await，使事件分发不被阻塞，可以立即处理下一个事件
        # 注意这里是 create_task 而非 await，这意味着 dispatch_event 会瞬间返回，不会阻塞 EventBus 的事件分发循环。
        asyncio.create_task(self.exec_session(event, agent_def))

    async def exec_session(self, event: ProcessableEvent, agent_def: "AgentDef") -> None:
        """执行一次完整的 Agent 会话
        核心执行逻辑：
          1. 创建 Agent 实例并恢复/新建会话
          2. 判断是否为斜杠命令，如果是则走命令分发流程
          3. 否则调用 session.stream_chat() 执行流式 LLM 对话
          4. 将结果通过 StreamChunkEvent + OutboundEvent 发布到事件总线
          5. 异常时进行重试（最多 MAX_RETRIES 次），超限后返回错误

        Args:
            event: 入站事件
            agent_def: Agent 定义对象，包含 prompt、工具、模型等配置
        """
        sem = self._get_or_create_semaphore(agent_def)
        session_id = event.session_id

        async with sem:
            try:
                # 使用 Agent 定义和全局上下文创建 Agent 实例
                agent = Agent(agent_def, self.context)

                if session_id:
                    try:
                        # 尝试恢复已有会话（加载历史消息上下文）
                        session = agent.resume_session(session_id)
                    except ValueError:
                        # 会话不存在（可能是首次对话或历史被清理），创建新会话并绑定原 session_id
                        logger.warning(f"Session {session_id} not found, creating new")
                        session = agent.new_session(session_id=session_id)
                else:
                    # 没有 session_id 时创建全新会话，由 Agent 自动生成 session_id
                    session = agent.new_session()
                    session_id = session.session_id

                # ── 斜杠命令优先处理 ──
                # 如果用户消息以 "/" 开头（如 /help、/reset），优先尝试命令分发
                if event.content.startswith("/"):
                    result = await self.context.command_registry.dispatch(
                        event.content, session
                    )
                    if result:
                        # 命令匹配并执行成功，直接返回命令结果，跳过 Agent 对话流程
                        await self._emit_response(event, result, agent_def.id)
                        logger.info(f"Command completed: {session_id}")
                        return
                    # 如果命令未匹配（result 为空），则继续走 Agent 对话流程

                collected_content = ""
                async for chunk in session.stream_chat(event.content):
                    chunk_type = chunk.get("type")

                    if chunk_type == "token":
                        token = chunk["data"]
                        collected_content += token
                        await self._emit_stream_chunk(
                            event, token, agent_def.id
                        )

                    elif chunk_type == "status":
                        logger.info(f"Stream status: {chunk['data']}")

                    elif chunk_type == "tool_result":
                        logger.debug(f"Tool result: {chunk['data'].get('name')}")

                    elif chunk_type == "done":
                        break

                    elif chunk_type == "error":
                        await self._emit_response(
                            event, "", agent_def.id, str(chunk.get("data", "Unknown error"))
                        )
                        return

                logger.info(f"Session completed: {session_id}")

                await self._emit_response(event, collected_content, agent_def.id)
            except Exception as e:
                # ── 异常处理与重试机制 ──
                logger.error(f"Session failed: {e}")

                if event.retry_count < MAX_RETRIES:
                    # 重试次数未耗尽：创建一个重试事件重新投入事件总线
                    # 使用 dataclasses.replace() 创建新事件（dataclass 不可变，不能直接修改字段）
                    retry_event = replace(
                        event,
                        retry_count=event.retry_count + 1,
                        content=".",
                    )
                    # 将重试事件发布回事件总线，AgentWorker 会再次收到并处理
                    await self.context.eventbus.publish(retry_event)
                else:
                    # 重试次数已耗尽，向用户返回错误信息
                    await self._emit_response(event, "", agent_def.id, str(e))

        self._maybe_cleanup_semaphores(agent_def)

    async def _emit_stream_chunk(
        self,
        event: ProcessableEvent,
        content: str,
        agent_id: str,
    ) -> None:
        """Emit a streaming chunk event."""
        stream_event = StreamChunkEvent(
            session_id=event.session_id,
            source=AgentEventSource(agent_id),
            content=content,
        )
        await self.context.eventbus.publish(stream_event)

    async def _emit_response(self,
                             event: ProcessableEvent,
                             content: str,
                             agent_id: str,
                             error: str | None = None) -> None:
        """Emit response event with content
        发送响应事件的辅助方法：将内容或错误信息封装为 OutboundEvent 并发布。

        使用场景：
          - Agent 定义不存在时返回错误
          - 斜杠命令执行完毕返回结果
          - 重试耗尽后返回最终错误

        Args:
            event: 原始入站事件，用于获取 session_id 以关联响应
            content: 响应正文内容（正常结果或空字符串）
            error: 可选的错误信息，非 None 时会转为字符串写入出站事件的 error 字段
        """
        if isinstance(event, DispatchEvent):
            result_event: OutboundEvent | DispatchResultEvent = DispatchResultEvent(
                session_id=event.session_id,
                source=AgentEventSource(agent_id),
                content=content,
                error=str(error) if error else None,  # 确保 error 为字符串类型或 None
            )
        else:
            result_event = OutboundEvent(
                session_id=event.session_id,
                source=AgentEventSource(agent_id),
                content=content,
                error=str(error) if error else None,  # 确保 error 为字符串类型或 None
            )
        await self.context.eventbus.publish(result_event)

    def _get_or_create_semaphore(self, agent_def: "AgentDef") -> asyncio.Semaphore:
        """Get existing or create new semaphore for agent"""
        if agent_def.id not in self._semaphores:
            self._semaphores[agent_def.id] = asyncio.Semaphore(
                agent_def.max_concurrency
            )
        logger.debug(
            f"Created semaphore for {agent_def.id} with value {agent_def.max_concurrency}"
        )
        return self._semaphores[agent_def.id]

    def _maybe_cleanup_semaphores(self, agent_def: "AgentDef") -> None:
        """Remove semaphores for certain agents"""
        if agent_def.id not in self._semaphores:
            return
        if not self._semaphores[agent_def.id]._waiters:
            del self._semaphores[agent_def.id]
