import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

# stream output support
from typing import TYPE_CHECKING, Any, AsyncGenerator, Awaitable, Callable, Dict

import litellm
from litellm.types.completion import (
    ChatCompletionMessageParam as Message,
)

from ant.core.context_guard import ContextGuard
from ant.core.events import EventSource
from ant.core.session_fsm import SessionFSM, SessionPhase
from ant.core.session_state import SessionState
from ant.core.stream_pipeline import PipelineContext, StreamPipeline
from ant.core.stream_stages import (
    StreamContextBuildStage,
    StreamContextGuardStage,
    StreamInputGuardStage,
    StreamLLMCallStage,
    StreamObservabilityStage,
    StreamOutputGuardStage,
    StreamTerminalStage,
    StreamToolExecutionStage,
    StreamValidationStage,
)
from ant.core.tracer import ExecutionTracer
from ant.provider.llm import LLMProvider
from ant.provider.llm.usage import UsageRecorder

# 17 document ingestion
from ant.tools.doc_ingest_tool import ingest_document

# 14 post-message tool
from ant.tools.post_message_tool import create_post_message_tool
from ant.tools.registry import ToolRegistry

# 18 knowledge search
from ant.tools.retriever_knowledge_tool import retriever_knowledge
from ant.tools.skill_tool import create_skill_tool

# 15 agent-dispatch
from ant.tools.subagent_tool import create_subagent_dispatch_tool
from ant.tools.webread_tool import create_webread_tool
from ant.tools.websearch_tool import create_websearch_tool

if TYPE_CHECKING:
    from ant.core.agent_loader import AgentDef
    from ant.core.context import SharedContext
    from ant.provider.llm import LLMToolCall


def _looks_like_secret(value: str) -> bool:
    """Heuristic for credential-shaped values (audit redaction).

    A value is treated as a secret when it is long (> 20 chars) and
    contains '=' (key=value style) or the word "token".
    """
    if len(value) <= 20:
        return False
    return "=" in value or "token" in value.lower()


def _redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """Redact credential-shaped *args* values before persisting an audit row.

    Top-level string values matching ``_looks_like_secret`` are replaced
    with "[REDACTED]".  Nested structures are kept as-is (top-level
    heuristic by design).
    """
    return {
        key: "[REDACTED]" if isinstance(value, str) and _looks_like_secret(value) else value
        for key, value in args.items()
    }


def _make_audit_sink(
    session_factory: Any,
    session_id: str,
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """Build the MySQL ``audit_log`` sink for one session (Phase 4E).

    The sink is bound to *session_id* (governance is rebuilt per session
    in ``_build_tools``, so the closure naturally carries that session's
    id) and appends one ``audit_log`` row per tool call through a fresh
    session.  Best-effort by design: any failure is logged, never raised —
    the caller already runs us fire-and-forget (see
    ``ToolGovernance.record_call``).  jsonl mode has no session factory and
    never calls this.
    """
    from ant.storage.models import AuditLogRecord

    async def audit_sink(entry: dict[str, Any]) -> None:
        try:
            async with session_factory() as session:
                session.add(
                    AuditLogRecord(
                        session_id=session_id,
                        event_type="tool_call",
                        payload={
                            "tool": entry.get("tool"),
                            "args": _redact_args(entry.get("args") or {}),
                            "result_preview": entry.get("result_preview", ""),
                            "elapsed": entry.get("elapsed", 0.0),
                        },
                    )
                )
                await session.commit()
        except Exception:
            logging.getLogger(__name__).warning(
                "audit_log write failed (session=%s tool=%s); tool call is "
                "unaffected",
                session_id,
                entry.get("tool"),
                exc_info=True,
            )

    return audit_sink


class Agent:
    """A configured agent that creates and manages conversation sessions."""

    def __init__(self, agent_def: "AgentDef", context: "SharedContext") -> None:
        self.agent_def = agent_def
        self.context = context
        self.llm = LLMProvider.from_config(agent_def.llm)

    def _build_tools(
        self, include_post_message: bool, session_id: str | None = None
    ) -> ToolRegistry:
        """Build a ToolRegistry with tools appropriate for the session.

        *session_id* (optional) binds the governance audit sink to one
        session: ``_build_tools`` is invoked per session, so each
        governance gets a sink closure carrying that session's id.
        """
        # Build ToolGovernance if a tool_policy is configured on this agent.
        # Lazy import to avoid circular dependency: agent.py → registry →
        # sandbox → core.__init__ → agent.py.
        from ant.tools.policy import ToolGovernance, ToolPolicy

        governance: ToolGovernance | None = None
        if self.agent_def.tool_policy:
            policy = ToolPolicy(**self.agent_def.tool_policy)
            governance = ToolGovernance(policy)
            # Phase 4E audit persistence: MySQL-backed storage (session
            # factory present) gets a sink writing each recorded call into
            # the audit_log table; jsonl mode keeps the in-memory audit
            # only.  Governance is per-session (rebuilt on every call),
            # so the sink closure is bound to this session's id.
            if session_id and getattr(self.context, "_session_factory", None) is not None:
                governance.set_audit_sink(
                    _make_audit_sink(self.context._session_factory, session_id)
                )

        registry = ToolRegistry.with_builtins(governance=governance)

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
        """Get token threshold based on model's context window.

        Dynamic: 80% of the model's ``max_input_tokens`` from litellm's
        model registry.  Custom/unknown model names (not in the registry)
        fall back to 160000 (80% of a 200k window) with a warning.
        """
        model = self.agent_def.llm.model
        try:
            info = litellm.get_model_info(model)
            max_input = (
                int(info.get("max_input_tokens") or 0) if isinstance(info, dict) else 0
            )
            if max_input > 0:
                return int(max_input * 0.8)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "get_model_info(%s) failed: %s — falling back to threshold 160000",
                model,
                exc,
            )
            return 160000
        logging.getLogger(__name__).warning(
            "Model %s not in litellm model registry — "
            "falling back to threshold 160000",
            model,
        )
        return 160000

    async def new_session(
        self,
        source: EventSource,
        session_id: str | None = None,
    ) -> "AgentSession":
        """Create a new conversation session."""
        session_id = session_id or str(uuid.uuid4())

        include_post_message = source.is_cron
        tools = self._build_tools(include_post_message, session_id)

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

        await self.context.history_store.create_session(
            self.agent_def.id, session_id, source
        )
        return session

    async def resume_session(self, session_id: str) -> "AgentSession":
        """Load an existing conversation session."""
        session_query = [
            session
            for session in await self.context.history_store.list_sessions()
            if session.id == session_id
        ]
        if not session_query:
            raise ValueError(f"Session not found: {session_id}")

        session_info = session_query[0]
        source = session_info.get_source()

        # Get all messages (no max_history limit)
        history_messages = await self.context.history_store.get_messages(session_id)

        # Convert HistoryMessage to litellm Message format
        messages: list[Message] = [msg.to_message() for msg in history_messages]

        include_post_message = source.is_cron
        # Build tools for resumed session
        tools = self._build_tools(include_post_message, session_info.id)

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
    fsm: SessionFSM = field(default_factory=SessionFSM)
    tracer: ExecutionTracer = field(default_factory=ExecutionTracer)

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

    def _truncate_old_tool_results(self) -> None:
        """Truncate tool messages from previous turns in the in-memory state.

        The LLM has already seen the full results and synthesized a response.
        Keeping them in full bloats the context for every subsequent turn.
        We keep the first ~500 chars which capture the title and source URL.
        """
        MAX_TOOL = 500
        for i, msg in enumerate(self.state.messages):
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > MAX_TOOL:
                    self.state.messages[i] = {**msg, "content": content[:MAX_TOOL] + "…"}

    async def harness_stream_chat(self, message: str) -> AsyncGenerator[Dict[str, Any], None]:
        """Streaming chat via harness pipeline.

        Each turn is traced (ExecutionTracer) and the session lifecycle
        is governed by a finite state machine (SessionFSM).
        """
        _logger = logging.getLogger(__name__)

        # Truncate tool results from *previous* turns so they don't bloat
        # the context.  The LLM already synthesized them into its response;
        # keeping only the first ~500 chars (title + URL) is sufficient for
        # future turns to recall what was searched.
        self._truncate_old_tool_results()

        # ── IMPORTANT: do NOT add the user message to session state yet. ──
        # If the input guardrail blocks this message (prompt injection),
        # we must not persist the blocked message in history — otherwise
        # the LLM will "answer" the blocked question in a future turn,
        # completely bypassing the guardrail.
        #
        # The user message is added later in StreamContextBuildStage
        # (after StreamInputGuardStage has passed).
        # Reset per-turn confirmation denials (new user message = new turn)
        self.shared_context.confirmation_broker.reset_turn(self.session_id)

        # Reset per-turn tool call counts (new user message = new turn),
        # otherwise max_calls_per_turn silently becomes a session total.
        if self.tools._governance:
            self.tools._governance.reset_turn_counts()

        await self._retrieve_memories(message)

        # ── FSM: enter active processing ──
        try:
            self.fsm.transition_to(SessionPhase.ACTIVE)
        except ValueError as exc:
            _logger.warning("FSM transition skipped: %s", exc)

        # ── Start execution trace ──
        trace = self.tracer.start_trace(self.session_id)

        pipeline = StreamPipeline()
        pipeline.add_stage(StreamValidationStage())
        pipeline.add_stage(StreamInputGuardStage())
        pipeline.add_stage(StreamObservabilityStage())
        pipeline.add_stage(StreamContextBuildStage())
        pipeline.add_stage(StreamContextGuardStage())
        pipeline.add_stage(StreamLLMCallStage())
        pipeline.add_stage(StreamToolExecutionStage())
        pipeline.add_stage(StreamOutputGuardStage())
        pipeline.add_stage(StreamTerminalStage())

        ctx = PipelineContext(
            session=self,
            user_message=message,
            tool_schemas=self.tools.get_tool_schemas(),
            trace=trace,
        )

        # ── Harness tuning from config ─────────────────────────────────
        # PipelineContext fields `tool_timeout` / `max_parallel_tools` are
        # being added by the parallel pipeline agent; setattr works whether
        # or not the dataclass field exists yet, so this stays safe either
        # way.  (`max_iterations` already exists on PipelineContext.)
        cfg = self.shared_context.config
        for _name, _value in (
            ("max_iterations", cfg.pipeline.max_iterations),
            ("tool_timeout", cfg.tools.default_timeout),
            ("max_parallel_tools", cfg.pipeline.max_parallel_tools),
            ("parallel_writes", cfg.tools.parallel_writes),
        ):
            setattr(ctx, _name, _value)

        # ── Usage accounting ───────────────────────────────────────────
        # ctx.usage_recorder: async callable receiving the usage dict that
        # LLMProvider.stream_chat emits in its `usage` event.  Records into
        # the MySQL usage_records table when storage is MySQL-backed
        # (session_factory present); no-op otherwise.  Same setattr safety
        # as above.
        recorder = UsageRecorder(
            session_factory=self.shared_context._session_factory
        )

        async def _record_usage(data: dict) -> None:
            await recorder.record_usage(
                session_id=self.session_id,
                model=data.get("model", ""),
                prompt_tokens=data.get("prompt_tokens", 0),
                completion_tokens=data.get("completion_tokens", 0),
                cost=data.get("cost", 0.0),
            )

        setattr(ctx, "usage_recorder", _record_usage)

        error_occurred = False
        async for event in pipeline.run(ctx):
            if event.get("type") == "error":
                _logger.error(
                    "Stream error in session %s: %s",
                    self.session_id,
                    event.get("data", "unknown"),
                )
                error_occurred = True
            yield event

        # ── FSM: terminal state ──
        try:
            if error_occurred:
                self.fsm.transition_to(SessionPhase.FAILED)
            else:
                self.fsm.transition_to(SessionPhase.COMPLETED)
        except ValueError as exc:
            _logger.warning("FSM transition skipped: %s", exc)

        # ── Finish trace ──
        trace_summary = self.tracer.finish_trace(trace)
        _logger.info(
            "Trace summary: session=%s spans=%d duration=%.0fms",
            self.session_id,
            trace_summary.get("total_spans", 0),
            trace_summary.get("total_duration_ms", 0),
        )

        # ── Async memory extraction ──
        task = asyncio.create_task(self._maybe_extract_memories())
        task.add_done_callback(self._on_memory_extraction_done)

    def _on_memory_extraction_done(self, task: asyncio.Task) -> None:
        """Callback to log any unhandled exceptions from memory extraction."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger = logging.getLogger(__name__)
            logger.warning(f"Memory extraction task failed: {exc}")

    async def _retrieve_memories(self, current_message: str = "") -> None:
        """Retrieve relevant memories and inject into session state.

        修复（记忆方舟演示暴露）：当前用户消息在 InputGuard 通过前
        **不进入** state（护栏纪律，见 harness_stream_chat 注释），
        而旧实现只从 state 的历史消息建 query——全新会话里 state 为空、
        当前问题从未参与检索，memory_context 恒空。现在把当前消息作为
        参数传入（仅用于检索，不落 state），query = 当前消息 + 最近历史。
        """
        retriever = self.shared_context.memory_retriever
        if not retriever:
            return

        query = self._build_retrieval_query(current_message)
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

    def _build_retrieval_query(self, current_message: str = "") -> str:
        """Build a context-aware retrieval query from recent conversation turns.

        Instead of using only the last user message, this collects recent
        user messages to preserve conversational context and avoid
        semantic drift in multi-turn dialogs.
        """
        user_messages = []
        if current_message and current_message.strip():
            user_messages.append(current_message.strip())
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
                    f"Skipping memory extraction: {new_user_count} new user messages < threshold {threshold}"  # noqa: E501
                )
                return

            logger.info(f"Attempting memory extraction from {new_user_count} new user messages in session {self.session_id}")  # noqa: E501
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
                        await self._ingest_memory_to_graph(mem, target_id, new_meta)
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
                        await self._ingest_memory_to_graph(mem, target_id, new_meta)
                        logger.info(f"✨ Created memory (fallback) {target_id}: {mem['content']}")  # 新增日志  # noqa: E501
                else:
                    # 普通创建
                    # Phase 7 修复：显式传 ids=[memory_id]，保证 Qdrant 点 id 与
                    # 图节点 memory_id 一致——graph.expand 靠 memory id 在两者间
                    # 对齐，此前不传 id 时两者对不上、子图扩展永远为空。
                    memory_id = mem.get("memory_id") or uuid.uuid4().hex
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
                        ids=[memory_id],
                    )
                    await self._ingest_memory_to_graph(mem, memory_id, new_meta)
                    logger.info(f"✨ Created new memory: {mem['content']}")  # 新增日志

            logger.info(f"Processed {len(memories)} memories from session {self.session_id}")
            self.state._last_extracted_idx = len(self.state.messages)
        except Exception as e:
            logger.warning(f"Memory extraction failed: {e}", exc_info=True)

    async def _ingest_memory_to_graph(
        self,
        mem: dict,
        memory_id: str,
        meta: dict,
    ) -> None:
        """同步记忆到 Neo4j 记忆图（Phase 7 修复）。

        MERGE :Memory 节点 + 实体 + MENTIONED_IN 边，使
        detect_conflicts / mark_superseded / expand 真正有数据可用。
        图失败只降级（设计原则 11）：图是可选增强，绝不阻塞向量入库。
        """
        logger = logging.getLogger(__name__)
        graph = getattr(self.shared_context, "graph", None)
        if graph is None:
            return
        try:
            await graph.ingest({
                "memory_id": memory_id,
                "content": mem["content"],
                "category": meta.get("category", "fact"),
                "importance": meta.get("importance", 5),
                "created_at": meta.get("created_at"),
                "updated_at": meta.get("updated_at"),
                "source": "agent",
                "session_id": meta.get("session_id", self.session_id),
                "entities": mem.get("entities", []),
            })
        except Exception as exc:  # noqa: BLE001 — 图失败不阻塞记忆入库
            logger.warning("Graph ingest failed (degraded to vector-only): %s", exc)

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

