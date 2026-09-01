如果面试官在你前面的 **Agent + SubAgent + EventBus + Multi-Agent** 语境下问：

> **“Trace 你怎么做的？”**

他大概率不是问普通 Java 日志，而是在问 **Agent Runtime 的可观测性（Observability）怎么设计**。

你这个项目最好这样回答。

---

## 1. 先说核心：我会建立一条完整的 Trace

你的架构：

```text
User
 ↓
Main Agent
 ↓
LLM
 ↓
EventBus
 ↓
SubAgent
 ↓
Tool
 ↓
LLM
 ↓
SubAgent Result
 ↓
EventBus
 ↓
Main Agent
 ↓
LLM
 ↓
Final Answer
```

我要做到的是：

> **虽然任务跨越 Main Agent、SubAgent、LLM、Tool、EventBus 多个组件，但最终能够通过一个 Trace ID 把整个调用链串起来。**

比如：

```text
traceId = T001
```

下面有多个 Span：

```text
Trace T001
│
├── Span: MainAgent.run
│   │
│   ├── Span: LLM.call
│   │
│   ├── Span: EventBus.publish
│   │
│   ├── Span: SubAgent.run
│   │   │
│   │   ├── Span: LLM.call
│   │   ├── Span: Tool.search
│   │   └── Span: LLM.call
│   │
│   └── Span: LLM.call
│
└── Final Answer
```

这样你就可以回答：

> **“这个回答为什么慢？”**

然后一路查：

```text
MainAgent
  ↓ 500ms
LLM
  ↓ 3s
SubAgent
  ↓ 8s
Tool
  ↓ 7s
```

马上知道瓶颈在哪里。

---

# 2. Trace 和 Log 不一样

这个面试官很可能继续追问。

### Log

记录：

```text
2026-09-01 22:00:01
MainAgent started
```

它告诉你：

> **发生了什么。**

### Metric

例如：

```text
agent_request_total = 10000
agent_latency = 2.3s
tool_error_rate = 1.2%
```

告诉你：

> **系统整体怎么样。**

### Trace

告诉你：

> **某一次具体请求到底经历了什么。**

所以：

```text
Log      → What happened?
Metric   → How much / How often?
Trace    → Where did this request go?
```

这三个东西组成：

> **Observability**

---

# 3. 你这个 Agent 系统最重要的是“跨 Agent Trace”

普通 Web：

```text
HTTP
 ↓
Service A
 ↓
Service B
```

比较容易做 Trace：

```text
TraceId
 ↓
A
 ↓
B
```

但你的 Agent：

```text
MainAgent
   ↓
EventBus
   ↓
SubAgent
   ↓
Tool
```

中间是**异步消息**。

所以不能只依赖 HTTP Header。

你需要把 Trace Context 放进 Event。

例如：

```java
public class AgentEvent {

    private String eventId;

    private String traceId;

    private String spanId;

    private String parentSpanId;

    private String correlationId;

    private String sourceAgent;

    private String targetAgent;

    private String eventType;

    private Object payload;
}
```

例如：

```json
{
  "eventId": "E100",
  "traceId": "T001",
  "spanId": "S003",
  "parentSpanId": "S002",
  "correlationId": "TASK001",
  "sourceAgent": "main-agent",
  "targetAgent": "research-agent",
  "eventType": "SUB_AGENT_TASK"
}
```

这样 EventBus 就成为 Trace 的**传播媒介**。

---

# 4. TraceId 和 CorrelationId 不要混

这个是很好的面试加分点。

你前面说：

> SubAgent 执行完以后，把结果通过 EventBus 发回来。

那么我会同时维护：

### traceId

代表：

> **整个用户请求/Agent Run。**

例如：

```text
T001
```

整个生命周期都一样。

---

### spanId

代表：

> **Trace 中的某一个操作。**

例如：

```text
MainAgent.run → S001
LLM.call      → S002
publish       → S003
SubAgent.run  → S004
Tool.call     → S005
```

---

### correlationId

代表：

> **某一个异步任务/请求与响应之间的对应关系。**

例如：

```text
MainAgent
   │
   │ TASK-001
   ↓
SubAgent
   │
   │ RESULT TASK-001
   ↓
MainAgent
```

所以：

```text
TraceId
= 整个 Agent Run

SpanId
= 一次具体操作

CorrelationId
= 异步 Request / Response 的对应关系
```

这个区别你面试一定可以讲。

---

# 5. OpenTelemetry 是比较标准的实现

如果让我真正落地，我不会自己发明一套 Trace 系统。

我会优先采用：

> **OpenTelemetry**

它本身就是现在比较主流的可观测性标准。

> ✅ **本项目已落地（commit d93088d，Phase 6）**：`ant/observability/tracing.py`
> —— OTLP 环境变量对接 Jaeger/Tempo/Collector；`trace_to_file` 落
> `.logs/traces.jsonl`（本地可查询演示）；`tracer.py` 已桥接为 OTel 适配层
> （管线调用点零改动）。

架构可以是：

```text
Agent Runtime
      │
      │ OpenTelemetry SDK
      ↓
   OTEL Collector
      │
 ┌────┼────────┐
 ↓    ↓        ↓
Trace Metrics Logs
 ↓
Jaeger / Tempo / ...
```

Java/Spring Boot 的话可以利用 OpenTelemetry instrumentation。

你的 Agent Runtime 自己创建业务 Span：

```text
agent.run
agent.llm
agent.tool
agent.event.publish
agent.event.consume
agent.subagent
```

---

# 6. Agent 场景下，我不会只记录“请求耗时”

这是 Agent Trace 和普通微服务 Trace 最大的区别。

比如一个：

```text
agent.llm
```

我会记录：

```text
model = GPT-xxx
temperature = ...
input_tokens = 3200
output_tokens = 800
latency = 2.3s
finish_reason = tool_call
```

但是**不要默认把完整 prompt、用户隐私或敏感 payload 全塞进 Trace**。

生产环境应该考虑：

```text
脱敏
采样
字段白名单
Payload 大小限制
敏感信息过滤
```

---

# 7. Tool 也要单独 Trace

比如：

```text
MainAgent
    ↓
LLM
    ↓
Tool: search
    ↓
HTTP
    ↓
Search API
```

Trace：

```text
T001
│
├── agent.run
│
├── llm.call
│
└── tool.search
      │
      └── http.request
```

这样你可以知道：

> 是 LLM 慢，还是 Tool 慢。

甚至可以统计：

```text
search tool P99 = 5.2s
database tool P99 = 30ms
```

---

# 8. EventBus 这里尤其重要

你自己的架构最值得讲的其实就是这里。

发布：

```text
agent.event.publish
```

消费：

```text
agent.event.consume
```

于是：

```text
MainAgent
   │
   └── publish
         │
         ↓
      EventBus
         │
         ↓
      consume
         │
         ↓
     SubAgent
```

我会让：

```text
publish.span
```

和：

```text
consume.span
```

通过 Trace Context 建立关联。

于是最终 Trace UI 里能看到：

```text
MainAgent.run
  │
  ├── LLM
  │
  ├── Event Publish
  │
  └── SubAgent
        │
        ├── LLM
        ├── Tool
        └── Event Publish
```

这就是你这个项目真正有价值的 Trace。

---

# 9. 如果面试官问：“异步 EventBus 怎么保证 Trace 不断？”

你直接回答：

> **“同步调用可以通过 HTTP Header 等方式传播 Trace Context；异步 EventBus 则不能依赖线程上下文，所以我会在消息 Metadata 中显式携带 Trace Context，例如 traceId、spanId 和 correlationId。Producer 发布消息时注入 Context，Consumer 消费时提取 Context 并创建新的 Consumer Span，这样即使跨线程、跨进程甚至跨 Agent，最终也能在同一个 Trace 下关联起来。”**

这句话非常关键。

因为：

```text
ThreadLocal
```

**不能解决跨线程/跨进程的 Trace 传播。**

---

# 10. 最后给你一个面试版完整答案

如果面试官问：

> **“你的 Agent Trace 怎么做？”**

你可以说（本项目已全部落地，把"我会采用"换成"我的实现是"）：

> **“我的实现基于 OpenTelemetry：一个用户请求生成一个 TraceId（根 span `agent.run`），LLM 调用、Tool 调用、管线阶段、EventBus publish/consume 都是 Span。因为 Main Agent 和 SubAgent 通过 EventBus 异步通信，我不依赖 ThreadLocal，而是在事件载荷里显式携带 W3C traceparent——Producer 发布时注入当前 Span Context，Consumer 消费时 extract 并以 consume span 为父创建自己的 Span，这样 Main Agent → EventBus → SubAgent → Tool → EventBus → Main Agent 整条链路跨线程、跨进程都串在同一条 Trace 里。Span 属性只放 metadata（模型名/token 数/耗时/结果长度/状态），Prompt 与 Tool Payload 做脱敏不落 Trace。导出侧：OTLP 对接 Jaeger/Tempo/Collector，或落 JSONL 本地查询，禁用时零开销。”**

然后**主动补一句**：

> **“Agent Trace 和传统微服务 Trace 不太一样，我还会重点记录 Agent Step、LLM 调用、Tool Call、Token Usage、模型版本和决策结果，但 Prompt 和 Tool Payload 需要做脱敏和采样，避免把敏感数据直接写进 Trace。”**

这个回答已经是比较完整的 **Agent Runtime Observability** 设计了。

而且你现在的项目恰好有 **EventBus + MainAgent + SubAgent**，所以这不是纯八股，完全可以拿你自己的实现来讲。
