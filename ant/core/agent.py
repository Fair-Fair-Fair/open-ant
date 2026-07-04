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
from litellm.types.completion import (
    ChatCompletionMessageParam as Message,
    ChatCompletionMessageToolCallParam,
)

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

        last_user_msg = self.state.messages[-1].get("content", "") if self.state.messages else ""
        if not last_user_msg:
            return

        logger = logging.getLogger(__name__)
        try:
            memories = await retriever.retrieve(last_user_msg)
            if memories:
                self.state.memory_context = retriever.format_for_prompt(memories)
                logger.debug(f"Retrieved {len(memories)} memories for session {self.session_id}")
        except Exception as e:
            logger.debug(f"Memory retrieval failed: {e}")

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
