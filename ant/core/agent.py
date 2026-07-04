import uuid
import json
import logging
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from ant.core.context_guard import ContextGuard
from ant.core.session_state import SessionState
from ant.core.events import EventSource
from ant.provider.llm import LLMProvider
from ant.tools.registry import ToolRegistry
from ant.tools.skill_tool import create_skill_tool
from ant.tools.websearch_tool import create_websearch_tool
from ant.tools.webread_tool import create_webread_tool

# 14 post-message tool
from ant.tools.post_message_tool import create_post_message_tool
# 15 agent-dispatch
from ant.tools.subagent_tool import create_subagent_dispatch_tool
# 17 document ingestion
from ant.tools.doc_ingest_tool import ingest_document
# 18 knowledge search
from ant.tools.retriever_knowledge_tool import retriever_knowledge
from litellm.types.completion import (
    ChatCompletionMessageParam as Message,
    ChatCompletionMessageToolCallParam,
)
# stream output support
from typing import AsyncGenerator, Dict, Any, Union

if TYPE_CHECKING:
    from ant.core.context import SharedContext
    from ant.core.agent_loader import AgentDef
    from ant.provider.llm import LLMToolCall


class Agent:
    """A configured agent that creates and manages conversation sessions."""

    def __init__(self, agent_def: "AgentDef", context: "SharedContext") -> None:
        self.agent_def = agent_def
        self.context = context
        self.llm = LLMProvider.from_config(agent_def.llm)

    def _build_tools(self, include_post_message: bool) -> ToolRegistry:
        """Build a ToolRegistry with tools appropriate for the session."""
        registry = ToolRegistry.with_builtins()

        # Register skill tool if allowed
        if self.agent_def.allow_skills:
            skill_tool = create_skill_tool(self.context.skill_loader)
            if skill_tool:
                registry.register(skill_tool)

        websearch_tool = create_websearch_tool(self.context.config)
        if websearch_tool:
            registry.register(websearch_tool)

        webread_tool = create_webread_tool(self.context.config)
        if webread_tool:
            registry.register(webread_tool)

        # Register document ingest tool if memory is enabled
        if self.context.doc_ingester is not None:
            registry.register(ingest_document)
            registry.register(retriever_knowledge)

        if include_post_message:
            post_message_tool = create_post_message_tool(self.context)
            if post_message_tool:
                registry.register(post_message_tool)

        # Register subagent dispatch tool
        subagent_tool = create_subagent_dispatch_tool(
            self.agent_def.id, self.context
        )
        if subagent_tool:
            registry.register(subagent_tool)

        return registry

    def _get_token_threshold(self) -> int:
        """Get token threshold based on model's context window."""
        # Default to 80% of 200k context
        return 160000

    def new_session(
        self,
        source: EventSource,
        session_id: str | None = None,
    ) -> "AgentSession":
        """Create a new conversation session."""
        session_id = session_id or str(uuid.uuid4())

        include_post_message = source.is_cron
        tools = self._build_tools(include_post_message)

        # Create context guard for this session
        context_guard = ContextGuard(
            shared_context=self.context,
            token_threshold=self._get_token_threshold(),
        )

        state = SessionState(
            session_id=session_id,
            agent=self,
            messages=[],
            source=source,
            shared_context=self.context,
        )

        session = AgentSession(
            agent=self,
            state=state,
            context_guard=context_guard,
            tools=tools,
        )

        self.context.history_store.create_session(
            self.agent_def.id, session_id, source
        )
        return session

    def resume_session(self, session_id: str) -> "AgentSession":
        """Load an existing conversation session."""
        session_query = [
            session
            for session in self.context.history_store.list_sessions()
            if session.id == session_id
        ]
        if not session_query:
            raise ValueError(f"Session not found: {session_id}")

        session_info = session_query[0]
        source = session_info.get_source()

        # Get all messages (no max_history limit)
        history_messages = self.context.history_store.get_messages(session_id)

        # Convert HistoryMessage to litellm Message format
        messages: list[Message] = [msg.to_message() for msg in history_messages]

        include_post_message = source.is_cron
        # Build tools for resumed session
        tools = self._build_tools(include_post_message)

        # Create context guard
        context_guard = ContextGuard(
            shared_context=self.context,
            token_threshold=self._get_token_threshold(),
        )

        # Create SessionState with loaded messages
        state = SessionState(
            session_id=session_info.id,
            agent=self,
            messages=messages,
            source=source,
            shared_context=self.context,
        )

        return AgentSession(
            agent=self,
            state=state,
            context_guard=context_guard,
            tools=tools,
        )


@dataclass
class AgentSession:
    """Chat orchestrator - operates on swappable SessionState."""

    agent: Agent
    state: SessionState
    context_guard: ContextGuard
    tools: ToolRegistry
    started_at: datetime = field(default_factory=datetime.now)

    @property
    def session_id(self) -> str:
        """Delegate to state."""
        return self.state.session_id

    @property
    def source(self) -> "EventSource":
        """Delegate to state."""
        return self.state.source

    @property
    def shared_context(self) -> "SharedContext":
        """Delegate to state."""
        return self.state.shared_context

    async def chat(self, message: str) -> str:
        """Send a message to the LLM and get a response."""
        user_msg: Message = {"role": "user", "content": message}
        self.state.add_message(user_msg)

        # RAG: retrieve relevant memories before building prompt
        await self._retrieve_memories()

        tool_schemas = self.tools.get_tool_schemas()
        logger = logging.getLogger(__name__)

        while True:
            messages = self.state.build_messages()
            self.state = await self.context_guard.check_and_compact(self.state)
            content, tool_calls, stop_reason = await self.agent.llm.chat(messages, tool_schemas)

            tool_call_dicts: list[ChatCompletionMessageToolCallParam] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in tool_calls
            ]
            assistant_msg: Message = {
                "role": "assistant",
                "content": content,
            }
            if tool_call_dicts:
                assistant_msg["tool_calls"] = tool_call_dicts

            self.state.add_message(assistant_msg)

            if stop_reason == "tool_calls":
                await self._handle_tool_calls(tool_calls)
                continue

            if stop_reason == "length":
                logger.warning(
                    "LLM response truncated (max_tokens reached), "
                    "returning partial response"
                )

            if stop_reason == "content_filter":
                logger.warning("LLM response filtered by content filter")
                return content if content else "I'm unable to respond to that request."

            break

        # RAG: asynchronously extract memories from this conversation
        task = asyncio.create_task(self._maybe_extract_memories())
        task.add_done_callback(self._on_memory_extraction_done)

        return content

    def _on_memory_extraction_done(self, task: asyncio.Task) -> None:
        """Callback to log any unhandled exceptions from memory extraction."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger = logging.getLogger(__name__)
            logger.warning(f"Memory extraction task failed: {exc}")

    async def _retrieve_memories(self) -> None:
        """Retrieve relevant memories and inject into session state."""
        retriever = self.shared_context.memory_retriever
        if not retriever:
            return

        query = self._build_retrieval_query()
        if not query:
            return

        logger = logging.getLogger(__name__)
        try:
            memories = await retriever.retrieve(query)
            if memories:
                self.state.memory_context = retriever.format_for_prompt(memories)
                logger.debug(f"Retrieved {len(memories)} memories for session {self.session_id}")
        except Exception as e:
            logger.debug(f"Memory retrieval failed: {e}")

    def _build_retrieval_query(self) -> str:
        """Build a context-aware retrieval query from recent conversation turns.

        Instead of using only the last user message, this collects recent
        user messages to preserve conversational context and avoid
        semantic drift in multi-turn dialogs.
        """
        user_messages = []
        for msg in reversed(self.state.messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if content:
                    user_messages.append(content)
            if len(user_messages) >= 3:
                break

        if not user_messages:
            return ""

        user_messages.reverse()

        if len(user_messages) == 1:
            return user_messages[0]

        return " ".join(user_messages)

    async def _maybe_extract_memories(self) -> None:
        """Extract and store memories if conditions are met."""
        memory_guard = self.shared_context.memory_guard
        if not memory_guard:
            return

        logger = logging.getLogger(__name__)

        try:
            new_messages = self.state.messages[self.state._last_extracted_idx:]
            new_user_count = len([m for m in new_messages if m.get("role") == "user"])
            threshold = self.shared_context.config.memory.extraction_threshold
            if new_user_count < threshold:
                logger.debug(
                    f"Skipping memory extraction: {new_user_count} new user messages < threshold {threshold}"
                )
                return

            logger.info(f"Attempting memory extraction from {new_user_count} new user messages in session {self.session_id}")
            memories = await memory_guard.extract_memories(new_messages)
            if not memories:
                logger.info(f"No important memories extracted from session {self.session_id}")
                return

            vector_store = self.shared_context.vector_store
            now = datetime.now().isoformat()

            for mem in memories:
                # 检查是否有更新指令
                if mem.get("_action") == "update" and mem.get("_target"):
                    target_id = mem["_target"]
                    # 获取旧文档
                    old_docs = await vector_store.get([target_id])
                    if old_docs:
                        old_meta = old_docs[0].metadata
                        # 保留 created_at，更新其他字段
                        new_meta = {
                            "category": mem.get("category", "fact"),
                            "importance": mem.get("importance", 5),
                            "keywords": ",".join(mem.get("keywords", [])),
                            "session_id": self.session_id,
                            "created_at": old_meta.get("created_at", now),
                            "updated_at": now,
                        }
                        await vector_store.update(
                            id=target_id,
                            document=mem["content"],
                            metadata=new_meta,
                        )
                        logger.debug(f"Updated memory {target_id}: {mem['content']}")
                    else:
                        logger.warning(f"Target memory {target_id} not found, creating new instead")
                        # fallback to create
                        new_meta = {
                            "category": mem.get("category", "fact"),
                            "importance": mem.get("importance", 5),
                            "keywords": ",".join(mem.get("keywords", [])),
                            "session_id": self.session_id,
                            "created_at": now,
                            "updated_at": now,
                        }
                        await vector_store.add(
                            documents=[mem["content"]],
                            metadatas=[new_meta],
                            ids=[target_id]  # 使用原ID
                        )
                        logger.info(f"✨ Created memory (fallback) {target_id}: {mem['content']}")  # 新增日志
                else:
                    # 普通创建
                    new_meta = {
                        "category": mem.get("category", "fact"),
                        "importance": mem.get("importance", 5),
                        "keywords": ",".join(mem.get("keywords", [])),
                        "session_id": self.session_id,
                        "created_at": now,
                        "updated_at": now,
                    }
                    await vector_store.add(
                        documents=[mem["content"]],
                        metadatas=[new_meta],
                    )
                    logger.info(f"✨ Created new memory: {mem['content']}")  # 新增日志

            logger.info(f"Processed {len(memories)} memories from session {self.session_id}")
            self.state._last_extracted_idx = len(self.state.messages)
        except Exception as e:
            logger.warning(f"Memory extraction failed: {e}", exc_info=True)

    async def _handle_tool_calls(
        self,
        tool_calls: list["LLMToolCall"],
    ) -> None:
        """Handle tool calls from the LLM response."""
        tool_call_results = await asyncio.gather(
            *[self._execute_tool_call(tool_call) for tool_call in tool_calls]
        )

        for tool_call, result in zip(tool_calls, tool_call_results):
            tool_msg: Message = {
                "role": "tool",
                "content": result,
                "tool_call_id": tool_call.id,
            }
            self.state.add_message(tool_msg)

    async def _execute_tool_call(
        self,
        tool_call: "LLMToolCall",
    ) -> str:
        """Execute a single tool call."""
        # Extract key arguments
        try:
            args = json.loads(tool_call.arguments)
        except json.JSONDecodeError:
            args = {}

        try:
            result = await self.tools.execute_tool(tool_call.name, session=self, **args)
        except Exception as e:
            result = f"Error executing tool: {e}"

        return result

    async def stream_chat(self, message: str) -> AsyncGenerator[Dict[str, Any], None]:
        """流式对话，产生事件：'token', 'tool_calls', 'status', 'tool_result', 'done', 'error'"""
        user_msg: Message = {"role": "user", "content": message}
        self.state.add_message(user_msg)
        await self._retrieve_memories()

        tool_schemas = self.tools.get_tool_schemas()
        logger = logging.getLogger(__name__)

        while True:
            messages = self.state.build_messages()
            self.state = await self.context_guard.check_and_compact(self.state)

            # 这里调用 LLMProvider.stream_chat
            async for chunk in self.agent.llm.stream_chat(messages, tool_schemas):
                event_type = chunk.get("type")

                if event_type == "token":
                    yield {"type": "token", "data": chunk["data"]}

                elif event_type == "tool_calls":
                    tool_calls = chunk["data"]  # list[LLMToolCall]
                    # 可以发送一个状态，告知前端即将执行工具
                    yield {"type": "status", "data": f"正在执行 {len(tool_calls)} 个工具..."}

                    # 执行所有工具
                    tool_results = []
                    for tc in tool_calls:
                        result = await self._execute_tool_call(tc)
                        tool_results.append(result)
                        # 单个工具完成结果
                        yield {"type": "tool_result", "data": {"name": tc.name, "result": result}}

                    # 将 assistant 消息（含 tool_calls）和 tool 结果加入 state
                    assistant_msg: Message = {
                        "role": "assistant",
                        "content": "",  # 首轮可能无文本，但有时也有文本 + tool_calls，视情况而定
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {"name": tc.name, "arguments": tc.arguments}
                            }
                            for tc in tool_calls
                        ]
                    }
                    self.state.add_message(assistant_msg)
                    for tc, result in zip(tool_calls, tool_results):
                        self.state.add_message({
                            "role": "tool",
                            "content": result,
                            "tool_call_id": tc.id
                        })

                    # 工具执行完毕，继续循环（让 LLM 基于工具结果再次生成）
                    break  # 跳出 for，继续 while

                elif event_type == "done":
                    # 生成结束
                    yield {"type": "done", "finish_reason": chunk.get("finish_reason", "stop")}
                    return  # 直接结束生成器

                elif event_type == "error":
                    yield {"type": "error", "data": chunk["data"]}
                    return

            else:
                # 如果 for 循环正常结束（没有 break），说明没有 tool_calls 也没有 done，但正常情况会有 done
                # 这里可以认为结束了
                yield {"type": "done", "finish_reason": "stop"}
                break
