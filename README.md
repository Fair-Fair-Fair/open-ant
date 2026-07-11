<div align="center">

# 🐜 Open-Ant

### Harness-Engineering 多智能体运行时

**安全沙箱 · 输入输出护栏 · 工具治理 · 可观测性 · 长期记忆**

---

*"Prompt engineering tells the model what to say. Harness engineering controls what the model can do — and what can be done to it."*

</div>

---

## 为什么是 Open-Ant

蚁群个体能力有限，但依靠信息素、分工和通信能完成远超个体的复杂任务。Open-Ant 的工程重心不在 prompt 调优，而在**运行时约束**——五道独立防线把 LLM 不确定性收敛在安全边界内：

```
输入护栏 → 上下文守卫 → 工具治理 → 动作沙箱 → 输出护栏
```

---

# 🏗 Architecture

```text
                         CLI  │  Telegram  │  Discord  │  WebSocket
                                   │
                     ┌─────────────┴─────────────┐
                     │       📡 EventBus          │
                     │   持久化 · 订阅 · 回溯      │
                     └─────────────┬─────────────┘
                                   │
          ┌──────────────┬────────┴────────┬──────────────┐
          ▼              ▼                 ▼              ▼
   ┌──────────┐  ┌──────────────┐  ┌──────────┐  ┌──────────┐
   │ Agent    │  │ Channel      │  │ Delivery │  │ Cron     │
   │ Worker   │  │ Worker       │  │ Worker   │  │ Worker   │
   └────┬─────┘  └──────────────┘  └──────────┘  └──────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────┐
│              Harness Pipeline（9-Stage 中间件链）              │
│                                                               │
│  Validation → InputGuard → Observability → ContextBuild      │
│       ↓            ↓                         ↓               │
│  [empty/       [注入检测 +               [6层系统提示         │
│   exhausted]    控制字符清洗 +              + 历史组装]        │
│                 长度校验]                                     │
│                                                               │
│  ContextGuard → LLMCall → ToolExecution → OutputGuard → Term │
│       ↓            ↓           ↓              ↓          ↓   │
│  [三级窗口     [流式token]  [sandbox      [脱敏 +     [持久化 │
│   管理]                     check→exec    内容策略]    + done] │
│                             → audit]                          │
│                                                               │
│  ◄───────── loop until done or exhausted ──────────────────► │
│                                                               │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐         │
│  │ Sandbox  │ │Guardrails│ │Governance│ │   FSM    │         │
│  │ 3-layer  │ │input+out │ │perm+rate │ │ 8-phase  │         │
│  │ action   │ │ content  │ │ +audit   │ │lifecycle │         │
│  │ boundary │ │ boundary │ │          │ │          │         │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘         │
└──────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────┐
│  Provider Layer                                               │
│  LiteLLM · Tavily/Brave · Crawl4AI · ChromaDB · SBERT       │
└──────────────────────────────────────────────────────────────┘
```

---

# 🚀 Quick Start

### 前置要求

- Python 3.12+
- LLM API key（支持 [LiteLLM 100+ provider](https://docs.litellm.ai/docs/providers)）

### 安装与启动

```bash
git clone https://github.com/Fair-Fair-Fair/open-ant.git
cd open-ant
pip install -e .

# 创建 workspace（用户数据独立于仓库）
mkdir ~/open-ant-workspace
cat > ~/open-ant-workspace/config.user.yaml << 'EOF'
llm:
  provider: deepseek
  model: deepseek/deepseek-chat
  api_key: sk-your-api-key-here
  api_base: https://api.deepseek.com
default_agent: my-agent
EOF

mkdir -p ~/open-ant-workspace/agents/my-agent
cat > ~/open-ant-workspace/agents/my-agent/AGENT.md << 'EOF'
---
name: MyAgent
description: My first agent
---
You are a helpful assistant.
EOF

# 启动
open-ant server -w ~/open-ant-workspace
# 浏览器访问 http://127.0.0.1:8000
```

```bash
# 其他用法
export ANT_WORKSPACE=~/open-ant-workspace
open-ant chat -w $ANT_WORKSPACE              # CLI 对话
open-ant chat -w $ANT_WORKSPACE --agent foo  # 指定 Agent
open-ant ingest ./docs/report.pdf -w $ANT_WORKSPACE  # 导入文档到 RAG
```

### Linux 部署

支持 systemd 守护 + Nginx 反向代理。详见 [Linux 部署指南](./linux-deploy.md)。

---

# ✨ 核心亮点

### 🛡 安全纵深

```
用户输入
  → InputGuard：NFKC 规范化 + 混合脚本同形字检测 + 25+ 注入模式（6 类攻击）+ 控制字符清洗
  → ContextGuard：工具结果截断 → token 估算 → LLM 摘要压缩（阈值 160k）
  → Sandbox：
      Path    — 路径白名单 + glob 黑名单（配置/密钥/内部状态）
      Command — Docker 容器隔离（Copy-on-Start：只读快照 + 会话级 volume）+ 远程 Docker 支持
      Network — SSRF 防护 + 域名白/黑名单 + scheme 限制
  → OutputGuard：7 类密钥脱敏 + 工具结果注入扫描（warn/strip/block）+ 内容策略审查
  → 用户输出
```

Docker 沙箱隔离保证：`--network none` · `--read-only` · 非 root 用户 · `--memory` / `--cpus` 硬限制。容器内 `rm -rf /` 只删 volume 副本，真实文件从未触碰。

### ⚡ 流式 Pipeline + FSM + 可观测性

- **StreamPipeline**：9 阶段 async generator 洋葱链，token 级透传不缓冲，`max_iterations=10` 防无限循环
- **SessionFSM**：8 阶段状态机 + 严格转换表，非法转换拒绝不 crash
- **ExecutionTracer**：Span-based 全链路追踪，每阶段 + 每次工具调用生成带时序 span

### 🔒 工具治理 + Human-in-the-Loop

- **权限控制**：Agent 级 `denied_tools` / `require_confirmation` / `max_calls_per_turn` / `max_calls_per_session`
- **审批流**：`asyncio.Future` 异步确认代理，30s 超时自动拒绝（fail-closed），per-turn 缓存防重复弹窗
- **审计**：全调用链记录（延迟、参数、结果）

### 🧠 六层 Prompt + RAG 记忆 + 多频道路由

- **Prompt Builder**：Identity(AGENT.md) → Soul(SOUL.md) → Bootstrap → Runtime → Channel Hint → Memory(RAG)
- **MemoryGuard**：自动提取 → 向量检索（ChromaDB + SBERT）→ 去重合并；PDF/Markdown 分块入库
- **Routing**：三层正则路由 + Agent-to-Agent 异步委派（EventBus + Future）
- **Channels**：CLI / WebSocket / Telegram / Discord 四频道统一接入

---

# 🚀 Project Status

| 组件 | 说明 | 状态 |
|------|------|:----:|
| 📡 EventBus | pub/sub + 持久化 + 崩溃恢复 | ✅ |
| 🤖 Agent Runtime | Session 管理 + Tool Calling + Pipeline | ✅ |
| 🛡 **Sandbox** | Path / Command / Network + Docker 容器隔离 (Copy-on-Start) | ✅ |
| 🧱 **Guardrails** | InputGuard (注入检测+NFKC+混合脚本) + OutputGuard (脱敏+warn/strip/block) | ✅ |
| 🔒 **ToolGovernance** | 权限控制 + 调用限额 + 审计日志 | ✅ |
| 🛑 **Human-in-the-Loop** | `require_confirmation` UI 审批流 + 30s 超时自动拒绝 | ✅ |
| 🛡 **Evasion Testing** | 34 对抗用例 × 9 类绕过技术，检测率 76% | ✅ |
| 📊 **SessionFSM** | 8 阶段状态机 + 转换表 | ✅ |
| ⚡ **StreamPipeline** | 9 阶段流式中间件链 (async generator 洋葱模型) | ✅ |
| 🔍 **ExecutionTracer** | Span-based 可观测性追踪 | ✅ |
| 🧠 Prompt Builder + RAG | 6 层系统提示 + 向量检索 + 记忆去重 | ✅ |
| 🌐 Multi Channel | CLI / Telegram / Discord / WebSocket | ✅ |
| ⚙ Config Hot Reload | Watchdog + Pydantic v2 | ✅ |
| ⏰ Cron Scheduler | Agent 自管理定时任务 | ✅ |

---

# 🛣 Roadmap

### 🔴 Phase 1 · Close the Loop ✅

Docker 容器沙箱 + 输入/输出护栏 + Human-in-the-Loop + 对抗性安全测试 + 会话泄漏修复 + 生产环境错误消息脱敏。

### 🟡 Phase 2 · Production Hardening

Tool Call 双层预算 · Prompt Caching · 流式断点恢复 · 增量压缩 · Skill 级沙箱 · CI 自动化测试

### 🟢 Phase 3 · Intelligence

语义路由 · Tool Selection 本地过滤 · 自愈循环 (sandbox 违规后自动调整) · Agent Mesh 直连通信

### 🏗️ Phase 4 · Ecosystem

MCP 协议 · gRPC 服务暴露 · 分布式 EventBus (Redis/NATS) · OpenTelemetry

---

# 📦 Tech Stack

| Layer | Technology |
|------|------|
| 🐍 Language | Python 3.12 |
| ⚡ Async | asyncio |
| 🤖 LLM | LiteLLM（OpenAI / Anthropic / 国产模型等 100+ provider） |
| 📡 Channels | Telegram · Discord · CLI · WebSocket |
| 🔍 Search | Tavily · Brave Search |
| 🌍 Web Read | Crawl4AI · LangChain |
| 🧠 Vector Store | ChromaDB + SentenceTransformer |
| ⚙ Config | Pydantic v2 · YAML · Watchdog |

---

# 🎯 Design Philosophy

- 🛡 **Harness over Prompt**：把工程精力花在运行时约束上，而非提示词调优
- 👁 **显式优于隐式**：沙箱校验在工具函数体第一行显式调用——控制流完全透明
- 🛑 **Fail-closed**：默认拒绝，需显式配置才能放开
- 🪶 **优雅降级**：违规、非法状态转换、预算耗尽都不 crash——转为错误消息返回
- 🧅 **分层防御**：输入护栏 → 上下文守卫 → 工具治理 → 动作沙箱 → 输出护栏

---

<div align="center">

## 🐜 One Ant Is Small.

## 🐜 A Colony Can Change the World.

**Open-Ant — a harness-first AI agent runtime.**

</div>
