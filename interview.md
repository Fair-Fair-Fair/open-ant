# OpenAnt-MemoryArk 架构与面试题集

> 本文档分两部分：**Project** —— 系统各关键流程在项目内的完整流转路径（架构图 / Agent Loop / LLM Loop / 组件设计 / 技术栈作用 / 配置流转），供阅读者快速熟悉仓库；**Interview** —— 面试题与基于真实实现的回答大纲，供作者自测与复盘。
> 所有流程均可在代码中定位：仓库根为 `src/`，包在 `ant/` 下；数字与结论均可复现（见 README「测试与评测」）。

---

# Project

> 阅读约定：每个流程图的节点都可定位到代码——统一使用 `文件路径.类名.方法名` 记法（如 `bus/outbox.py::OutboxPublisher.run`），§1-§10 每节开头附「组件定位」清单。

## 0. 系统全景

```text
                     CLI │ Telegram │ Discord │ WebSocket(认证+限流)
                        │            │          │
                        ▼            ▼          ▼
                ┌───────────────────────────────────────┐
                │          CompositeBus                  │
                │   持久事件 ──► RabbitMQ / Outbox       │
                │   (durable + DLX五级重试 + DLQ)        │
                │   瞬态事件(流式token/确认) ──► 进程内   │
                └──────────────┬────────────────────────┘
                               │ 消费(幂等去重)
        ┌──────────────┬───────┴────────┬──────────────┐
        ▼              ▼                ▼              ▼
  AgentWorker    DeliveryWorker   ChannelWorker   CronWorker
        │
        ▼
┌────────────────────────────────────────────────────┐
│         StreamPipeline（9 阶段洋葱中间件链）         │
│  Validation → InputGuard(regex+LLM-judge) →        │
│  Observability → ContextBuild → ContextGuard →     │
│  LLMCall(Router+StreamRedactor) → ToolExecution    │
│  (确认先行→写类串行→只读并行+超时) → OutputGuard    │
│  → Terminal (断流兜底)                              │
└───────────────┬────────────────────────────────────┘
                │
   ┌────────────┼────────────────┬────────────────┐
   ▼            ▼                ▼                ▼
 MySQL      RabbitMQ          Qdrant(dense+     Neo4j(记忆图:
 (历史/审计  (事件总线)         sparse hybrid)    冲突仲裁/衰减)
 /成本/outbox)                  │                │
   │            │               ▼                ▼
   └──── Redis ─┘       检索管线: 改写→hybrid→子图扩展→rerank→定界注入
    (embedding缓存/限流)         │
                                ▼
                    Prometheus /metrics · /healthz · /readyz
```

## 1. 事件流转（入站 → 出站）

> 组件定位：`bus/composite.py::CompositeBus`、`bus/memory.py::InMemoryBus`、`bus/rabbitmq.py::RabbitMqBus`、`bus/outbox.py::OutboxPublisher`、`storage/outbox_ops.py::enqueue`、`core/context.py::SharedContext._build_outbox_writer`、`server/dedup.py`

```
Channel 收到消息
  → channel_worker.py::ChannelWorker._create_callback
      构造 InboundEvent（含 source/session_id）
  → bus/composite.py::CompositeBus.publish
      ├─ 瞬态事件(StreamChunk/Confirmation*) → bus/memory.py::InMemoryBus
      └─ 持久事件 → context.py::SharedContext._build_outbox_writer 闭包:
           1. 开新 MySQL session
           2. storage/outbox_ops.py::enqueue 写 outbox_events 行（与业务同事务）
           3. commit —— 状态写入 ⇔ 事件发出原子化
  → bus/outbox.py::OutboxPublisher.run 轮询(1s) published_at IS NULL 的行
  → 投递 RabbitMQ（durable 队列, prefetch=1, manual ack）
  → bus/rabbitmq.py::RabbitMqBus._on_message
       handler 正常返回 → 自动 ack；抛异常 → nack → DLX
  → bus/rabbitmq.py::RabbitMqBus._on_retry_message
      ant.retry 五级 TTL(5s→30min) → x-death>5 → ant.dlq
```

**设计要点**：outbox 保证崩溃不丢消息；DLX 重试把"失败即不确认"升级为 broker 语义；`server/dedup.py::mark_processed` 幂等去重（message_id 透传，重复投递只处理一次）。

## 2. Agent Loop 与 LLM Loop（一次用户消息的完整生命周期）

> 组件定位：`server/agent_worker.py::AgentWorker`、`core/agent.py::AgentSession.harness_stream_chat`、`core/stream_pipeline.py::StreamPipeline.run`、`core/stream_stages.py::StreamLLMCallStage`、`core/stream_stages.py::StreamToolExecutionStage`、`core/guardrails.py::StreamRedactor`、`provider/llm/usage.py::UsageRecorder`、`core/session_fsm.py::SessionFSM`

```
server/agent_worker.py::AgentWorker.dispatch_event(InboundEvent)
  ├─ server/observability.py::record_event_consumed 埋点
  ├─ rabbitmq 模式幂等检查（server/dedup.py::is_processed）
  ├─ 会话解析：history_store → routing → 斜杠命令 → Agent
  └─ core/agent.py::AgentSession.harness_stream_chat(message)
       ├─ 截断上一轮 tool 结果 / reset 确认缓存 / 检索记忆注入
       ├─ core/stream_pipeline.py::StreamPipeline.run（9 阶段洋葱链）
       │    core/stream_stages.py::StreamLLMCallStage.execute
       │       token → guardrails.py::StreamRedactor.feed(滑动缓冲先审后出) → 前端
       │       tool_calls → stream_stages.py::StreamToolExecutionStage.execute
       │          tools/base.py::validate_args → 三步执行计划 → 结果回传
       │       usage → provider/llm/usage.py::UsageRecorder.record_usage 落库
       │    stop_reason == "tool_calls" → StreamPipeline.run 循环重跑全链
       │    stop_reason == "stop" → stream_stages.py::StreamTerminalStage 落库 + done
       └─ core/session_fsm.py::SessionFSM 终态 + trace 汇总 + 异步记忆提取
  → OutboundEvent → server/delivery_worker.py::DeliveryWorker.handle_event
      → channel.reply → 用户
```

**防死循环**：`max_iterations`（`stream_stages.py::StreamValidationStage` 达上限时置 `stop_reason="exhausted"` 终止外层循环——这是验收时修过的真 bug）；单工具硬超时（`asyncio.wait_for`）；FSM EXHAUSTED 态。
**防断流**：`core/stream_pipeline.py::StreamPipeline.run` 捕获 GeneratorExit/CancelledError 时写占位消息"响应中断"，会话可继续；流式 token 不进 broker（进程内直连，断网不影响事件可靠性）。

## 3. 记忆写入流（提取 → 双存储 → 仲裁）

> 组件定位：`core/memory_guard.py::MemoryGuard.extract_memories`、`memory/extraction.py::extract_memories`、`memory/graph.py::MemoryGraph`、`provider/memory/qdrant_store.py::QdrantStore.add`

```
用户对话若干轮 → core/memory_guard.py::MemoryGuard.extract_memories
  → memory/extraction.py::extract_memories：工具调用约束 JSON
     (extract_memories tool schema, temperature 0.2)
  → 单条坏数据丢弃 + warning（不连坐整批）
  → 每条记忆:
     ├─ memory/graph.py::MemoryGraph.detect_conflicts
     │    （同实体+同类别+更旧, top 3）
     │    有冲突 → LLM 仲裁 keep_new/keep_old/merge
     │    keep_new/merge → memory/graph.py::MemoryGraph.mark_superseded 建边
     ├─ memory/graph.py::MemoryGraph.ingest
     │    MERGE 记忆节点 + 实体 + MENTIONED_IN
     └─ provider/memory/qdrant_store.py::QdrantStore.add
          dense(dashscope/bge) + sparse(BM25/jieba) 双命名向量
          + payload（源/类别/重要度/关键词/时间戳）
  → 软归档：memory/graph.py::MemoryGraph.archive_stale
       (低重要度+过期) → archived=true（不物理删）
```

## 4. 检索流（改写 → hybrid → 图扩展 → 重排 → 注入）

> 组件定位：`core/memory_retriever.py::MemoryRetriever.retrieve`、`provider/memory/qdrant_store.py::QdrantStore.query`、`memory/graph.py::MemoryGraph.expand`、`memory/rerank.py::rerank`、`core/memory_retriever.py::MemoryRetriever.format_for_prompt`、`core/prompt_builder.py::PromptBuilder.build`

```
core/memory_retriever.py::MemoryRetriever.retrieve(query)
  ├─ core/memory_retriever.py::MemoryRetriever._rewrite_query
  │    （memory.query_rewrite_enabled, 默认关, 失败回退原 query）
  ├─ provider/memory/qdrant_store.py::QdrantStore.query(prefer_hybrid=True)
  │    服务端 prefetch: dense + sparse 双分支 → RRF 融合
  │    （payload filter / 分数 min-max 归一化）
  ├─ memory/graph.py::MemoryGraph.expand(hit ids)：一跳子图
  │    同实体其他记忆 + SUPERSEDES 链上较新记忆（archived 排除）
  ├─ 合并去重（doc id）
  ├─ memory/rerank.py::rerank（cross-encoder, to_thread, 失败原序返回）
  └─ core/memory_retriever.py::MemoryRetriever.format_for_prompt
       每条 <retrieved> 定界 + 不可信数据声明
       → core/prompt_builder.py::PromptBuilder.build 第 6 层注入 system prompt
```

## 5. Context 与 Prompt 组装

> 组件定位：`core/prompt_builder.py::PromptBuilder.build`、`core/context_guard.py::ContextGuard.check_and_compact`

```
core/prompt_builder.py::PromptBuilder.build（六层, 每次 LLM 调用前重建）
  Identity(AGENT.md) → Soul(SOUL.md) → Bootstrap(BOOTSTRAP.md+定时任务)
  → Runtime(agent id/时间) → Channel Hint(平台提示) → Memory(RAG 定界注入)

core/context_guard.py::ContextGuard.check_and_compact 三级防御
  （160k 动态阈值 = core/agent.py::Agent._get_token_threshold, get_model_info×0.8）
  1. tool 结果截断(1000 字符)    —— ContextGuard._truncate_large_tool_results
  2. litellm token_counter 估算 —— ContextGuard.estimate_tokens
  3. LLM 摘要压缩               —— ContextGuard._compact_messages
       （summarize_model 小模型优先；失败硬截断兜底）
```

## 6. 工具执行流（三步计划）

> 组件定位：`tools/registry.py::ToolRegistry.execute_tool`、`tools/base.py::validate_args`、`tools/policy.py::ToolGovernance`、`core/stream_stages.py::StreamToolExecutionStage`、`core/agent.py::_make_audit_sink`

```
LLM 返回 tool_calls → tools/registry.py::ToolRegistry.execute_tool
  ├─ tools/base.py::validate_args
  │    （additionalProperties:false；失败返回点名错误串）
  ├─ tools/policy.py::ToolGovernance.check_permission（白/黑名单+限额）
  └─ core/stream_stages.py::StreamToolExecutionStage 三步执行计划:
       ① require_confirmation 工具：先行逐个串行 + 审批流
       ② 写类(write/edit/bash)：按 LLM 顺序串行（防竞态）
       ③ 只读：gather 并行 + Semaphore(max_parallel_tools)
     每工具 wait_for(tool_timeout) 硬超时 → 错误串回传
     结果按 LLM 原始顺序还原；审计 fire-and-forget
       → core/agent.py::_make_audit_sink 落 audit_log
```

## 7. SubAgent 流转

> 组件定位：`tools/subagent_tool.py::create_subagent_dispatch_tool`、`server/agent_worker.py::AgentWorker._cancel_subagent_tasks`

```
主 Agent 调用 tools/subagent_tool.py::create_subagent_dispatch_tool
  → 校验 agent 存在 → 创建子会话(绑 parent_session_id)
  → 订阅 DispatchResultEvent → publish DispatchEvent(带 timeout 预算)
  → server/agent_worker.py::AgentWorker.dispatch_event 消费
      → 子会话 harness_stream_chat
  → DispatchResultEvent 回传 → 主侧 wait_for(timeout_seconds 10-600s)
  → 超时返回错误串；主任务取消
      → server/agent_worker.py::AgentWorker._cancel_subagent_tasks
        级联取消子任务（单进程内传递）
```

## 8. 配置与凭据流转

> 组件定位：`utils/config.py::Config`、`utils/settings.py::InfraSettings`、`core/context.py::SharedContext`、`cli/doctor.py`

```
config.user.yaml（workspace 级：后端开关/模型/护栏/记忆参数）
  └─ utils/config.py::Config(pydantic v2) + watchdog 热重载（仅 yaml）
.env（凭据分量：MYSQL_USERNAME/PASSWORD、RABBITMQ_*、QDRANT_URL/KEY、
     NEO4J_URI/USER/PASSWORD、各 API key）
  └─ utils/settings.py::InfraSettings(pydantic-settings, 环境变量优先)
       → mysql_dsn()/rabbitmq_url()/qdrant_url()… 组装连接串
       → core/context.py::SharedContext 装配
           _create_history_store / _assemble_bus
           凭据缺失 → 显式 WARNING 回退(mysql→jsonl, rabbitmq→memory)
           cli/doctor.py 会报 ERROR
凭据纪律：值只在 .env；日志/测试/文档零泄露（check_publish 门禁）
```

## 9. 基础设施角色速查

| 组件 | 职责 | 关键文件.类.方法 |
|---|---|---|
| MySQL | 会话/消息/outbox/审计/成本/幂等表 | `storage/repository.py::MysqlHistoryRepository`、`storage/db.py::run_migrations`（SQLAlchemy async + Alembic） |
| RabbitMQ | 持久事件总线（DLX 重试阶梯 + DLQ） | `bus/rabbitmq.py::RabbitMqBus._on_message / _on_retry_message` |
| Redis | embedding 缓存（挂则直算）、滑窗限流 | `provider/memory/embedding.py::EmbeddingProvider._embed_cached`、`server/rate_limit.py::SlidingWindowLimiter.allow` |
| Qdrant | dense+sparse 双向量 hybrid（服务端 RRF） | `provider/memory/qdrant_store.py::QdrantStore.query / _ensure_collection` |
| Neo4j | 记忆图（冲突检测/SUPERSEDES/软归档） | `memory/graph.py::MemoryGraph.detect_conflicts / expand / archive_stale` |
| litellm Router | LLM 重试/超时/降级/成本 | `provider/llm/base.py::LLMProvider.chat / stream_chat` |

## 10. Trace 流转（OTel 埋点 → 传播 → 导出）

> 组件定位：`observability/tracing.py::init_tracing / inject_current_traceparent / start_consume_span / FileSpanExporter`、`core/tracer.py::ExecutionTracer.start_trace / Trace.start_span`、`core/stream_stages.py::_start_span / StreamLLMCallStage / StreamToolExecutionStage`、`bus/composite.py::CompositeBus.publish`、`bus/rabbitmq.py::RabbitMqBus._on_message`、`bus/memory.py::InMemoryBus._notify`、`core/events.py::Event.traceparent`

```
一次用户消息 = 一条 Trace
  ├─ 根 span：core/tracer.py::ExecutionTracer.start_trace → "agent.run"（属性 session.id）
  ├─ 阶段 span：core/stream_stages.py::_start_span（9 阶段各一个）
  │    ValidationStage / InputGuardStage / ObservabilityStage / ContextBuildStage /
  │    ContextGuardStage / LLMCallStage / ToolExecutionStage / OutputGuardStage / TerminalStage
  │    ├─ LLMCallStage：事件 first_token(ttft_ms) / tool_calls_requested / llm_error
  │    │   属性 llm.model / llm.prompt_tokens / llm.completion_tokens / llm.cost /
  │    │        llm.finish_reason / llm.response_length
  │    └─ ToolExecutionStage：子 span "ToolExecution:{name}"
  │        属性 tool.result_length / tool.status（超时置 error）
  ├─ 发布事件：bus/composite.py::CompositeBus.publish
  │    tracing.inject_current_traceparent → event.traceparent = "00-<32hex>-<16hex>-01"
  │    （W3C 格式随事件 JSON 序列化，经 RabbitMQ 载荷跨进程传递）
  └─ 消费事件：bus/rabbitmq.py::RabbitMqBus._on_message（memory.py::_notify 同构）
       tracing.start_consume_span(extract traceparent) → otel_trace.use_span 包裹全部 handler
       → 消费线程内 contextvars 重连 → 子 Agent 的 agent.run 及以下 span 自动续接同一条 Trace

导出三态（observability/tracing.py::init_tracing，幂等初始化）：
  OTEL_EXPORTER_OTLP_ENDPOINT → OTLP 批量导出（对接 Jaeger / Tempo / Collector）
  observability.trace_to_file   → FileSpanExporter 落 .logs/traces.jsonl（每行一个 span）
  observability.trace_console   → 控制台（开发调试）
  全部未配置 → no-op（零开销，test_tracing.py 断言过）
```

**设计要点**：① 与成本账本分工——`provider/llm/usage.py::UsageRecorder` 落 `usage_records` 表回答"每笔花了多少 token/钱"（可聚合出账单）；Trace 回答"这次请求去了哪、慢在哪"。两者交叉不重复，被问 trace 时只答成本埋点等于只说了一半；② 传播纪律——contextvars/ThreadLocal 过不了异步消息边界，所以 Trace Context **显式进事件载荷**，消费端 extract 重建父链；③ 脱敏纪律——span 属性只放 metadata（长度/计数/模型名/状态），prompt/参数/工具结果内容绝不进 span（`tests/test_tracing.py::test_no_content_leaks_into_span_attributes`）。

### 示例：最终 Trace 长什么样

主 Agent 收到"查一下 X 并写进笔记"，其中调用子代理做检索。在 Jaeger/Tempo UI 里看到的树（时间即瓶颈，一眼定位慢在哪）：

```text
Trace 5f8b2c1d…9e6f   总耗时 9.8s   ← 一条用户消息 = 一条 Trace
└─ agent.run 9.8s                    [session.id=cli-8f3a]
   ├─ ValidationStage 0.2ms
   ├─ InputGuardStage 3.1ms
   ├─ ObservabilityStage 1.0ms
   ├─ ContextBuildStage 45ms
   ├─ ContextGuardStage 12ms
   ├─ LLMCallStage 2.4s              [llm.model=deepseek-chat  llm.prompt_tokens=3200
   │                                  llm.completion_tokens=180  llm.finish_reason=tool_calls
   │                                  llm.cost=0.0041]
   │   └─ 事件 first_token: ttft_ms=680
   ├─ ToolExecutionStage 6.9s
   │  └─ ToolExecution:dispatch_subagent 6.8s
   │     └─ agent.event.consume 6.7s [event.type=DispatchEvent, bus=rabbitmq]  ← 跨总线续链点
   │        └─ agent.run 6.5s        [session.id=sub-2c91]   ← 子代理自己的根
   │           ├─ ContextBuildStage 40ms
   │           ├─ LLMCallStage 1.9s  [llm.finish_reason=tool_calls]
   │           ├─ ToolExecution:web_search 3.1s [tool.result_length=2840, tool.status=ok]
   │           └─ LLMCallStage 2.0s  [llm.finish_reason=stop]
   ├─ LLMCallStage 0.9s              [llm.finish_reason=stop]   ← 汇总子代理结果后的最终回复
   └─ TerminalStage 3ms
```

同一条 Trace 落盘后（`trace_to_file` → `.logs/traces.jsonl`，每行一个 span）——注意 5 行的 `trace_id` 完全相同，第 3 行的 `parent_id` 指向的是**另一个消费任务里**发布事件的 span，这条跨 RabbitMQ 的边就是事件载荷里的 traceparent 重建出来的：

```json
{"trace_id":"5f8b2c1d9a4e7b3f6c0d1a2b3c4d5e6f","span_id":"a1b2c3d4e5f60718","parent_id":null,"name":"agent.run","attributes":{"session.id":"cli-8f3a"},"status":"OK","start_ms":1756756800000,"end_ms":1756756809800}
{"trace_id":"5f8b2c1d9a4e7b3f6c0d1a2b3c4d5e6f","span_id":"d1e2f3a4b5c60713","parent_id":"a1b2c3d4e5f60718","name":"ToolExecution:dispatch_subagent","attributes":{"tool.status":"ok"},"status":"OK","start_ms":1756756802600,"end_ms":1756756809400}
{"trace_id":"5f8b2c1d9a4e7b3f6c0d1a2b3c4d5e6f","span_id":"c1d2e3f4a5b60710","parent_id":"d1e2f3a4b5c60713","name":"agent.event.consume","attributes":{"event.type":"DispatchEvent","bus":"rabbitmq"},"status":"OK","start_ms":1756756803100,"end_ms":1756756809800}
{"trace_id":"5f8b2c1d9a4e7b3f6c0d1a2b3c4d5e6f","span_id":"d1e2f3a4b5c60711","parent_id":"c1d2e3f4a5b60710","name":"agent.run","attributes":{"session.id":"sub-2c91"},"status":"OK","start_ms":1756756803200,"end_ms":1756756809700}
{"trace_id":"5f8b2c1d9a4e7b3f6c0d1a2b3c4d5e6f","span_id":"e1f2a3b4c5d60712","parent_id":"d1e2f3a4b5c60711","name":"LLMCallStage","attributes":{"llm.model":"deepseek-chat","llm.prompt_tokens":2100,"llm.completion_tokens":150,"llm.finish_reason":"stop","llm.cost":0.0028},"status":"OK","start_ms":1756756805600,"end_ms":1756756809600}
```

查询：`grep <trace_id> .logs/traces.jsonl` 或 `jq -s 'group_by(.trace_id)' .logs/traces.jsonl` 即可还原整条链（Jaeger 里直接看树）。

---

## 11. 记忆方舟场景（认知辅助概念验证 + 语音交互）

> 场景叙事的完整设计在 `memory-ark.md`；愿景问答在 `value.md`；演示资产在
> `workspace/evals/memoryark/`（仓库外）。

### 场景：阿尔茨海默病认知辅助（"记忆方舟"）

- **一句话**："当人的大脑开始遗忘这个世界的时候，世界不要因此把这个人也遗忘掉"——AI 不能治愈 AD，但可以成为**外部记忆系统**：替老人保管人物关系/人生事件/生活习惯，困惑时温柔重锚定现实（"我是小安，不是您的女儿。您的女儿叫 Emily"），记忆异常时给家人发信号。
- **架构映射**（90% 已有）：人生档案 = Neo4j 记忆图 + hybrid 检索；温柔重锚定 = AGENT.md/SOUL.md persona + 检索注入；主动提醒 = cron；高风险确认 = HITL；承认不知道 = abstention 纪律（LongMemEval 验证过）。
- **三条伦理红线**（写成 persona 纪律 + 探针）：永不扮演真人亲属 / 永不提供医疗用药建议 / 高风险行动必须家人确认。演示 9/9 通过（6 问答含 knowledge-update 仲裁 + 3 红线）。
- **诚实边界**：概念验证——虚构 persona、无真实患者数据、非医疗产品。

### 语音交互流（终端 talk mode）

- **形态**：`open-ant chat` 启动时选择文字/语音（`--voice` / `--text` 显式指定，否则交互询问；对齐 OpenClaw talk mode 思路）。语音 = 又一个输入输出形态，底层同一条 harness 管线（流式 token 仍走进程内 pipeline）。
- **链路**：回车录音 6s（sounddevice 16kHz）→ faster-whisper small 转写 → InboundEvent → 管线 → OutboundEvent → 终端显示 + edge-tts 合成播放；任何一步失败降级纯文字（设计原则 11）。
- **选型**：faster-whisper（ctranslate2 CPU，免 torchaudio——本机 torch 2.12 无 torchaudio 故弃 funasr）+ edge-tts（免费、无 key、中文自然音色）+ miniaudio；无麦克风可用 `voice_demo.py --loopback` 自测全链。
- **踩坑**：faster-whisper 把 numpy 数组一律按 16kHz 解释、不接收采样率参数——edge-tts 的 24kHz mp3 直喂 → 语速错乱、转写全幻觉（"我女儿叫什么名字"→"永遠都受傷了"），scipy resample_poly 到 16k 修复。
- **诚实边界**：当前是 CLI demo 级；VoiceChannel 生产化（进 ChannelWorker）是 roadmap。

---

# Interview

> 每题给出基于**真实实现**的回答大纲（含可追问的落点）。诚实优先：没做/有边界的地方明确说，并给出改进方向——这比硬答更有说服力。

### 1. 记忆更新过程中，如何保证语义匹配准确性？匹配错误如何处理？

- **准确性三层**：① 写入侧：提取用工具调用约束 JSON（schema 强约束字段与类型，temperature 0.2），单条坏数据丢弃不连坐；② 检索侧：dense+sparse 双向量 + RRF 融合 + cross-encoder 重排 + Neo4j 子图扩展；③ 验证侧：30 条标注查询 eval，recall@5 = 0.983（数字可复现）。
- **匹配错误处理**：冲突检测（同实体+同类别+更旧事实 top3）→ LLM 仲裁 keep_new/keep_old/merge → SUPERSEDES 边记录取代关系；低重要度旧记忆软归档；检索失败一律降级返回（不炸链路）。
- 可追问落点：`memory/graph.py` detect_conflicts 的 Cypher 条件；`evals/report_retrieval.md`。

### 2. 为什么采用 Agent 架构？相比 Workflow 编排的优势？

- **本质区别**：Workflow 是确定性图（节点边预先定义），Agent 是"模型在循环里自主决策"（tool-calling loop）。开放任务（文件操作、检索、搜索混合）无法预先枚举路径，Agent 更合适；固定流程（如数据 ETL）Workflow 更稳。
- **本项目的 Agent 化设计**：事件总线解耦（协议三实现可替换）、9 阶段管线把不确定性的每一面都加了约束（护栏/预算/超时/审计）。
- **边界**：`max_iterations=10` + 单工具超时 + 写类串行，就是"Agent 自由度的收敛边界"——这是 Agent 与 Workflow 之间光谱的位置：自由决策 + 硬约束。
- 可追问落点：`stream_pipeline.py` 的循环条件、`stream_stages.py` 三步执行计划。

### 3. 全流程自动化（无人工审核）如何保证稳定性与可靠？

- **四道自动化防线**：沙箱（路径/Docker/网络）、护栏（regex+judge 输入、流式脱敏输出）、治理（限额+审计落库）、预算（迭代上限+工具超时）。
- **可靠性兜底**：LLM 重试/超时/降级链；消息 at-least-once+幂等+DLQ；断流占位消息；所有"可降级组件"失败都不炸主链路（原则 11）。
- **稳定性观测**：Prometheus 指标 + `/readyz` 真探活 + doctor 自检 + crash 指数退避重启。
- **风险承认**：审批流（require_confirmation）默认只覆盖配置内的高危工具；全自动场景下应把写类工具纳入审批或限定沙箱目录——诚实说这是配置取舍而非机制缺失。

### 4. 多工具调用整体流程如何设计？如何管理调用顺序？

- 见 Project §6 三步执行计划：① 需审批工具先行串行（审批语义独立）；② 写类串行（确定性、防同文件竞态）；③ 只读并行（Semaphore 限流、单工具硬超时）。
- **顺序管理**：结果按下标还原 LLM 原始顺序（下游消息组装不变）；写类顺序 = LLM 返回顺序（意图顺序）。
- 可追问落点：`stream_stages.py::StreamToolExecutionStage`；`test_pipeline_parallel.py` 的耗时断言。

### 5. 大模型调用外部工具的底层机制？模型如何决定调用哪个工具？

- 机制：请求里注入 `tools` 数组（JSON Schema：name/description/parameters）；模型在生成中返回 `tool_calls`（id + function.name + arguments JSON 串）；执行器按 name 分发执行；结果以 `role:"tool"` 消息回传；循环直到 `finish_reason="stop"`。
- 流式细节：tool_calls 的 arguments 跨 chunk 增量累积（index 对齐）；本项目修过 `tc["id"]` 缺失 KeyError 与多工具 index 兜底。
- 模型"决定"：由 schema 的 description 驱动选择——所以本项目把工具描述当 prompt 工程做（补行为约束、错误回传约定）。

### 6. Agent 系统核心模块与职责？

- 管线层（校验/护栏/上下文构建/守卫/LLM 调用/工具执行/输出护栏/终结）、记忆层（短期=会话+ContextGuard；长期=Qdrant+Neo4j）、工具层（schema 校验/沙箱/治理/审计）、通道层（四平台统一事件）、观测层（metrics/探活/埋点/OTel 链路追踪）。
- 每个模块都有独立文件与测试映射（462 用例）。

### 7. 短期记忆 vs 长期记忆及实现方式？

- **短期**：会话 messages（MySQL 持久化）+ ContextGuard 三级防御（截断→估算→摘要压缩，阈值按模型动态 0.8×max_input_tokens）+ 断流占位保持会话可续。
- **长期**：见 Project §3/§4——提取（约束 JSON）→ 双存储（Qdrant 语义 + Neo4j 图关系）→ 冲突仲裁 → 时间衰减软归档；Redis 只做 embedding 缓存。
- 差异点：记忆有**生命周期**（冲突/取代/归档），不是无限 append 的向量库。

### 8. Agent Harness 关键能力？企业级为什么需要？

- Harness = 约束 LLM 不确定性的运行时层：沙箱（动作边界）、护栏（内容边界）、治理（频率与权限边界）、预算（迭代/超时边界）、观测（可审计边界）、可靠（重试/降级边界）。
- 企业级需要：可审计（每次工具调用落库）、可回滚（沙箱）、可观测（成本/延迟/失败率）、可证明（eval 门禁）。裸调 API 这些全没有。

### 9. Skill 懒加载机制？为什么需要？

- 实现：`skill_loader.discover_skills()` 扫描 SKILL.md（frontmatter+正文）；agent 的 `_build_tools` 时注册 skill 工具；skill 定义只在被调用时解析。
- 为什么：按需加载定义、不常驻内存；多 agent 各自只注册允许的 skill。
- **诚实**：当前 discover 每次调用全盘重扫（无缓存）——这是已知改进点（文件监听+缓存），面试时主动提出。

### 10. 消息队列如何保证不丢？生产端/消费端分别做了什么？

- **生产端**：outbox 模式——事件与业务状态同事务落 MySQL，publisher confirm 后标记，崩溃不丢。
- **Broker**：durable 队列 + persistent 消息。
- **消费端**：manual ack；失败 nack → DLX 五级 TTL 重试（5s→30min）→ 超 5 次进 DLQ（可人工补偿）；幂等：processed_messages 唯一键，重复投递只处理一次。
- **瞬态分流**：流式 token/确认不进 broker（进程内直连）——网络往返只付给业务事件。
- 验证：真实 broker 集成测试覆盖 round-trip/DLQ/message_id 透传。

### 11. RAG 用什么实现？多格式文件怎么解析？

- 自研管线（Qdrant + Neo4j），未用 LangChain 全家桶；文档解析用 langchain-community loaders（pdf/docx/csv/json/html/pptx/xlsx 统一 chunk），确定性 chunk id 幂等重入。
- **诚实**：不同格式目前共用一套 chunk 策略，没有定向优化（如 PDF 按标题切分/表格结构化）——这是明确的未来方向。

### 12. 做过哪些优化提升召回率与准确率？

- 双向量 hybrid + RRF；cross-encoder 重排；Neo4j 子图扩展；query 改写；diversity_by_source（防长文档挤占窗口）。
- **数据驱动**：eval 对照发现英文稀疏模型拖累中文（0.917<0.983），换 bge-small-zh 后 hybrid 追平 0.983；jieba 中文稀疏实验（0.967）未跑赢——保留 fastembed 默认。所有结论有报告。

### 13. 交给成熟产品（开源/阿里/字节）会不会更好？为什么自研？

- **边界判断**：成熟产品胜在开箱即用与生态；自研的价值在①对链路的完全可控（出问题能定位到行）②深度定制（图记忆冲突仲裁是通用 RAG 产品没有的）③学习价值。
- **诚实的折中**：不该自研的全用成熟件——LLM 接入用 litellm（而非自写 provider）、文档解析用 langchain loaders、消息用 RabbitMQ（而非自写队列）。"自研调度与记忆语义，借力成熟基础设施"。
- 有数据支撑：eval 报告证明自研检索的分数可量化、可对照。

### 14. 你的产品看起来像一个小 Agent，你认为是吗？

- **承认定位**：是"个人助手级运行时"，不是平台级产品——功能面（四通道/工具数）确实小。
- **但工程深度是生产级**：五组件基础设施集成验证、462 测试、三套 eval、at-least-once 消息语义、密钥门禁。小不等于玩具；玩具的判据是"能不能经受故障与追问"，本项目每一层都有测试与真实集成兜底。
- 对比 Claude Code/OpenClaw：能力覆盖更窄，但可靠性工程链路完整可讲。

### 15. Agent 怎么判断任务执行完而不死循环？底层逻辑？

- **正常退出**：`finish_reason="stop"`（模型不再请求工具）。
- **兜底**：`max_iterations=10`——达上限时 ValidationStage 置 `stop_reason="exhausted"` 终止外层循环（验收时修过"只 yield error 不改 stop_reason 导致无限循环"的真 bug）；单工具 `wait_for` 超时；FSM EXHAUSTED 态；子代理超时+取消传播。
- 如果我来设计防死循环：迭代上限、单步超时、token 预算、重复动作检测（连续相同 tool_calls 提前熔断——本项目尚未做，可作改进点）。

### 16. 了解 MCP 或 Skills 吗？自己写过吗？

- 概念清楚：MCP = 模型上下文协议（tools/resources/prompts 标准化，Agent 生态的 USB-C）；Skills = 可复用能力包。
- **本项目**：有 Skill 系统（SKILL.md 定义 + 懒加载注册），工具 schema 手写 JSON Schema（与 MCP 同构思想）；**未实现 MCP server/client**——是明确的生态接入方向（接入后工具面直接扩到社区生态）。

### 17. 页面断网几秒又恢复，协议层怎么处理？

- **流式层**：token 走进程内直连（不进 broker），断网 = WS 断开 → GeneratorExit → 写占位消息"响应中断"，会话状态已持久化，重连后按 source 恢复同一会话继续。
- **事件层**：RabbitMQ `connect_robust` 自动重连；投递失败 nack→DLX 重试（不依赖客户端在线）。
- **消费层**：幂等去重保证"重连重投"不产生重复回复。
- 诚实：WS 重连后的 token 续流（断点续传）未实现——占位恢复是当前语义。

### 18. 系统架构是什么？最难的问题如何解决？

- 架构：见 Project §0 全景图（一句话：事件总线 + 9 阶段管线的多智能体运行时，五组件基础设施）。
- **最难的三件事**：① 崩溃一致性——outbox 同事务 + broker 至少一次 + 消费幂等三件套；② LLM 不确定性收敛——六层边界（沙箱/护栏/治理/预算/观测/可靠）；③ 流式体验与可靠性语义的冲突——瞬态/持久事件分流（token 不进 broker）。
- 实战证据：真云验收抓出的坑（payload 索引 400、point id UUID、Result 会话内消费、StaticPool 回滚竞态）——每个都有修复与测试。

### 19. BM25 原理？为什么对词频饱和处理？

- 公式：`score(D,Q) = Σ IDF(qi) · f(qi,D)·(k1+1) / (f(qi,D) + k1·(1−b+b·|D|/avgdl))`；IDF 用 BM25+ 变体 `log(1+(N−df+0.5)/(df+0.5))`。
- **饱和**：k1（默认 1.5）抑制词频线性增长——"出现 10 次"不等于"相关 10 倍"（对齐人类直觉：2 次到 4 次比 8 次到 10 次更有区分度）；b 控制文档长度惩罚。
- 项目里：Qdrant 服务端 sparse（fastembed Qdrant/bm25）+ IDF modifier 代算全局 IDF；备选 jieba 中文分词方案（sha256 稳定哈希索引空间）。

### 20. 为什么不使用 LangChain/LangGraph？

- **选型边界**：LLM 接入用 litellm Router（LangChain 同源），但编排自研——LangGraph 是图编排抽象，本项目是**运行时**（事件总线+管线+治理），两者解决的问题不同：图编排解决"流程怎么走"，运行时解决"流程之外发生什么"（崩溃、重试、审计、隔离）。
- **自研的理由**：at-least-once 消息语义/幂等/DLQ、图记忆冲突仲裁，框架生态没有现成；学习与控制链路。
- **兜底**：462 测试 + eval 证明自研部分的质量；该用成熟件的（解析、消息、向量库）全部用成熟件——"自研的是语义，不是轮子"。

### 21. Agent 框架中规划、执行、工具、模型如何分层？

- **规划**：Agent 自主（tool-calling 循环 + max_iterations/预算约束）——本项目不做显式 planner，靠模型+约束。
- **执行**：三步执行计划（审批/写类/只读）+ 单工具超时 + 审计。
- **工具**：schema 校验 + 沙箱前置 + 治理限额——工具层不信任模型输入。
- **模型**：Router 抽象（可换模型/降级链），业务代码不感知具体 provider。
- 分层解耦的证据：bus/storage/llm/tools 各自 Protocol，测试可独立替换实现。

### 22. 多 Agent 协作中，如何证明消息没有被重复消费或遗漏？

- **不重复**：processed_messages 表（message_id 唯一键）——消费前查、处理成功插入；重复投递直接跳过并 ack。集成测试：真实 broker 重复投递同 message_id → 只处理一次。
- **不遗漏**：outbox 同事务（发出即落库）+ publisher confirm + 失败进 DLX/DLQ（永不静默丢弃）+ DLQ 可人工补偿。
- **单进程级联**：子代理任务按 parent_session_id 注册，父任务取消级联取消子任务（跨进程取消是明确边界，如实说明）。

### 23. Trace 怎么做？（跨 Agent 链路追踪）

- **别只答成本埋点**：`UsageRecorder`（`provider/llm/usage.py`，落 `usage_records` 表）是成本**账本**（每笔 token/钱，可聚合成账单）；完整答案 = 成本账本 + 过程追踪。一条用户消息 = 一条 Trace：根 span `agent.run`，管线 9 阶段 / LLM / 工具 / 事件 publish·consume 都是 span（完整流转见 Project §10）。
- **最有价值的点（跨总线续链）**：Main→SubAgent 经 RabbitMQ 异步通信，contextvars/ThreadLocal 在消息边界断链——所以 publish 时 `inject_current_traceparent` 把 W3C traceparent 注入事件载荷（`core/events.py::Event.traceparent` 随 JSON 序列化跨进程），consume 时 `start_consume_span` extract 出父链并用 `use_span` 包裹 handler，子代理整棵子树自动续接同一条 Trace。端到端断言：`tests/test_tracing.py::test_composite_bus_injects_traceparent_and_handler_runs_under_consume`。
- **标准与工程**：OpenTelemetry SDK；导出三态（OTLP→Jaeger/Tempo/Collector、trace_to_file→JSONL、console，未配置 no-op 零开销）；span 属性 metadata-only（模型/token/耗时/长度/状态，prompt 与工具 payload 脱敏不落）；查询 = grep trace_id 或 Jaeger 树（示例见 Project §10）。
- 可追问落点：`observability/tracing.py::start_consume_span` 的 parent context 重建；`stream_stages.py::StreamLLMCallStage` 的属性集合与 first_token 事件；为什么 Span 属性只放 metadata（隐私 + 存储成本）。

### 24. 你的项目解决什么问题？（2026 秋招高频第一问）

- **30 秒版（背）**："我的项目是一个 24/7 常驻的个人 AI 助手运行时。2026 年 always-on agent 成为行业公认品类（OpenClaw 引爆，Gemini Spark、微软 Scout 跟进），但这类系统要把电脑和账号权限交给模型——安全厂商公开警告（CVE-2026-25253 网关劫持 8.8 分、恶意 skill 市场、prompt injection 删邮件事故）。我的项目回答这个品类的**信任问题**：消息不丢（RabbitMQ DLX 五级重试 + outbox + 幂等）、权限可控（三层沙箱/HITL/审计）、记忆可仲裁（Neo4j 冲突检测 + LongMemEval 评测）、全链路可观测（OTel/Prometheus），462 个自动化测试 + CI，已发布 PyPI。"
- **场景升华**：第一个应用场景是记忆方舟（AD 认知辅助概念验证，见 Project §11）——不是"造轮子"，是技术恰好能承载一个真正重要的场景。
- **数字化证据链**：LongMemEval（ICLR 2025，500 题官方 judge 协议）baseline 6.0% / oracle 62.6%，memory 管线数字落在区间内（`eval.md` §3.3 恢复命令）；检索 recall@5=0.983；护栏 85%/FP 0%。
- 可追问落点：`eval.md`（三层评估体系）、`memory-ark.md`（场景映射表）、README 的"为什么是 OpenAnt"章节。

### 25. 语音交互怎么做？（终端语音模式）

- 见 Project §11 语音交互流——核心一句话：**语音是 channel 形态的输入输出，底层同一条 harness 管线**，所以不需要"语音版 agent"，只需要输入输出的换装。
- 选型逻辑（面试官爱听的 trade-off）：ASR 弃 funasr 用 faster-whisper（本机 torch 2.12 CPU 无 torchaudio，依赖链要轻）；TTS 用 edge-tts（免费无 key vs 云厂商付费实时 API）；本地推理 vs 云端 API 的取舍、延迟换自托管。
- 诚实：CLI demo 级，ChannelWorker 生产化是 roadmap；无麦克风用 loopback 自测全链（TTS 合成问题→ASR 转回→agent→TTS）。

### 26. 评测/决策调用踩过什么坑？（推理模型的隐藏成本）

- **坑 1（judge 全判 False）**：官方 judge 协议 max_tokens=10（gpt-4o 非推理够用），deepseek-v4-flash 是推理模型——10 token 被隐藏 reasoning 吃光（实测 212）、content 恒空 → 500 题全判 False（0.0000%）。改 256 后正常。
- **坑 2（仲裁静默失效，更隐蔽）**：自由文本 JSON 决策任务上，推理模型可能把 4095/4096 token 全烧进 reasoning（finish=length，content 空），prompt 里加"don't overthink"无效——**修复：决策调用改工具约束**（arbitrate/resolve function schema），与提取层同机制后零空响应。教训：对"必须结构化输出"的任务，用 function calling 强制收敛，而不是 prompt 祈祷。
- **坑 3（转写全幻觉）**：faster-whisper 把 numpy 数组一律按 16kHz 解释——24kHz 音频直喂语速错乱，转写全是幻觉文本（见 Project §11）。
- 面试句式："评测推理模型时，输出预算要给思考 token 留空间；结构化任务用工具约束保证收敛——这两个修复都有测试和日志实锤。"

---

*本文档与代码同步维护：每个流程指向的实现文件若重构，请同步更新对应章节。*
