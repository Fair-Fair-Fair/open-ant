<div align="center">

# 🐜 Open-Ant

### Harness-Engineering 多智能体运行时

**安全沙箱 · 输入输出护栏 · 工具治理 · 可观测性 · 长期记忆**

---

*"Prompt engineering tells the model what to say. Harness engineering controls what the model can do — and what can be done to it."*

</div>

---

> 📖 此仓库是 [build-your-own-openclaw](https://github.com/czl9707/build-your-own-openclaw) 的后续实现。
> 在阅读本仓库之前，建议先了解 Agent 运行时框架的基础概念（Tool Calling、EventBus、Session 管理）。

## 🐜 Why Open-Ant?

现实中的蚂蚁个体能力有限，但依靠**信息素**、**分工协作**和**持续通信**，整个蚁群能够完成远超个体能力的复杂任务。

Open-Ant 借鉴了这一思想，但工程重心不在 prompt 调优，而在**运行时基础设施**——用三道防线把 LLM 的不确定性约束在安全边界内：

| 🐜 蚁群概念 | 🤖 Open-Ant | 🛡 Harness Engineering 角色 |
|-----------|------------|---------------------------|
| Ant | Agent + AgentSession | FSM 生命周期管理 |
| Pheromone | EventBus (pub/sub) | 事件持久化 + 崩溃恢复 |
| Nest defense | **Sandbox** | 三层动作边界（Path / Command / Network） |
| Chemical recognition | **Guardrails** | 输入注入检测 + 输出脱敏 + 工具结果扫描 |
| Task quota | **ToolGovernance** | 权限检查 + 调用限额 + 审计日志 |
| Spatial memory | **RAG Memory** | MemoryGuard 提取 → 去重 → 合并 |
| Trail monitoring | **ExecutionTracer** | Span-based 可观测性追踪 |
| Nest repair | **ContextGuard** | 三级上下文窗口管理 |

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
- 一个 LLM API key（支持 [LiteLLM 100+ provider](https://docs.litellm.ai/docs/providers)）

### 1. 克隆仓库

```bash
git clone https://github.com/Fair-Fair-Fair/open-ant.git
cd open-ant
```

仓库只包含运行时源码，**不包含 workspace（配置/Agent/Skill 等用户数据）**：

```
open-ant/                    # 即 src/，仓库根目录
├── ant/                     # Python 包
│   ├── core/                # 运行时核心（Pipeline、Sandbox、Guardrails...）
│   ├── tools/               # 工具层（Registry、Governance、builtin...）
│   ├── server/              # 7×24 服务（Agent/Delivery/Cron/WebSocket Worker）
│   ├── channel/             # 消息渠道（Telegram、Discord）
│   ├── cli/                 # 命令行入口
│   ├── provider/            # 外部服务适配（LiteLLM、ChromaDB、Tavily...）
│   └── utils/               # 配置、日志等工具
├── images/
├── pyproject.toml
└── README.md
```

### 2. 创建 Workspace 并配置

Workspace 是用户数据目录，**独立于仓库**，通常放在 `~/open-ant-workspace/`：

```bash
mkdir ~/open-ant-workspace
cd ~/open-ant-workspace

# 创建 Agent
mkdir -p agents/my-agent
cat > agents/my-agent/AGENT.md << 'EOF'
---
name: MyAgent
description: My first agent
---
You are a helpful assistant.
EOF

# 创建最小配置
cat > config.user.yaml << 'EOF'
llm:
  provider: deepseek
  model: deepseek/deepseek-chat
  api_key: sk-your-api-key-here
  api_base: https://api.deepseek.com

default_agent: my-agent
EOF
```

最终 workspace 结构：

```
~/open-ant-workspace/
├── config.user.yaml
├── agents/my-agent/
│   └── AGENT.md
├── skills/          # 可选：可复用 Skill
├── crons/           # 可选：定时任务
└── memories/        # 自动生成：长期记忆存储
```

### 3. 安装

```bash
cd open-ant          # 回到仓库目录
pip install -e .
```

### 4. 启动服务

```bash
# workspace 不在仓库里，必须显式指定路径
open-ant server --workspace ~/open-ant-workspace

# 或者用简写
open-ant server -w ~/open-ant-workspace
```

输出：

```
Starting ant server...
WebSocket server started on 127.0.0.1:8000
```

### 5. 打开 Web UI

浏览器访问 **http://127.0.0.1:8000**，选择 Agent 即可开始对话。

```
┌─────────────────────────────────────────────────┐
│  🐜 Open-Ant              [pickle ▼]           │
│                                                 │
│  ┌─────────────────────────────────────────┐    │
│  │ Hello! How can I help you today?        │    │
│  │                                         │    │
│  │ > 帮我查一下今天的天气                    │    │
│  │                                         │    │
│  │ [Agent 调用 websearch 工具...]           │    │
│  │ 今天北京晴，22°C ~ 30°C                  │    │
│  └─────────────────────────────────────────┘    │
│                                                 │
│  ┌─────────────────────────────────────────┐    │
│  │ Type your message...              [Send] │    │
│  └─────────────────────────────────────────┘    │
└─────────────────────────────────────────────────┘
```

### 更多用法

```bash
# workspace 路径可以设为环境变量，避免每次输入
export ANT_WORKSPACE=~/open-ant-workspace

# 命令行交互式对话
open-ant chat -w $ANT_WORKSPACE

# 指定不同 Agent
open-ant chat -w $ANT_WORKSPACE --agent cookie

# 导入文档到 RAG 知识库
open-ant ingest ./docs/report.pdf -w $ANT_WORKSPACE
```

---

## 🐧 部署到 Linux

### 环境要求

- Ubuntu 22.04+ / Debian 12+ / Rocky 9+（或其他 systemd-based 发行版）
- Python 3.12+（系统包管理器通常版本较老，推荐 `deadsnakes` PPA 或 `pyenv`）

```bash
# Ubuntu 安装 Python 3.12
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt install python3.12 python3.12-venv python3.12-dev -y
```

### 推荐部署拓扑

```
                     Internet
                        │
                ┌───────┴───────┐
                │  nginx / caddy │  ← 反向代理 + TLS 终结
                │  :443 (https)  │
                └───────┬───────┘
                        │
                ┌───────┴───────┐
                │  open-ant      │  ← uvicorn :8000 (127.0.0.1)
                │  systemd 守护  │
                └───────┬───────┘
                        │
                ┌───────┴───────┐
                │  ~/open-ant-   │
                │  workspace/    │  ← 用户数据（config、agents、history）
                └───────────────┘
```

open-ant **只监听 127.0.0.1**（默认），由反向代理处理 TLS 和公网暴露。不要直接绑定 `0.0.0.0`。

### 安装步骤

```bash
# 1. 创建专用用户
sudo useradd -m -s /bin/bash open-ant
sudo su - open-ant

# 2. 克隆仓库
git clone https://github.com/Fair-Fair-Fair/open-ant.git
cd open-ant

# 3. 创建虚拟环境
python3.12 -m venv .venv
source .venv/bin/activate

# 4. 安装
pip install --upgrade pip
pip install -e .

# 5. 创建 workspace（见 Quick Start 第 2 步）
mkdir ~/open-ant-workspace
# ... 创建 config.user.yaml + agents/ ...

# 6. 验证启动
open-ant server -w ~/open-ant-workspace
# 看到 "WebSocket server started on 127.0.0.1:8000" 即成功，Ctrl+C 退出
```

### systemd 服务

创建 `/etc/systemd/system/open-ant.service`：

```ini
[Unit]
Description=Open-Ant Agent Runtime
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=open-ant
Group=open-ant
WorkingDirectory=/home/open-ant/open-ant
Environment=PATH=/home/open-ant/open-ant/.venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/home/open-ant/open-ant/.venv/bin/open-ant server -w /home/open-ant/open-ant-workspace
Restart=always
RestartSec=5

# 安全加固
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/open-ant/open-ant-workspace
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now open-ant
sudo systemctl status open-ant
```

### Nginx 反向代理

```nginx
server {
    listen 443 ssl;
    server_name ant.example.com;

    ssl_certificate     /etc/letsencrypt/live/ant.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ant.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 300s;   # Agent 响应可能很慢
    }
}
```

WebSocket 需要的 `Upgrade` 和 `Connection` header 已经包含在内。

### 运维命令速查

```bash
# 查看日志
sudo journalctl -u open-ant -f

# 重启
sudo systemctl restart open-ant

# 更新代码
sudo su - open-ant
cd open-ant
git pull
source .venv/bin/activate
pip install -e .
sudo systemctl restart open-ant

# 健康检查
curl http://127.0.0.1:8000/api/agents
```

### Linux 注意事项

| 点 | 说明 |
|----|------|
| **Shell** | `create_subprocess_shell` 在 Linux 使用 `/bin/sh`，与 bash tool 的命名无关 |
| **Sandbox 命令** | 危险命令正则在 Linux 下全部生效（`sudo`、`rm -rf /`、`chmod 777` 等） |
| **ChromaDB** | 首次启动自动在 workspace 下创建 `.memory/`，需确保磁盘有空间 |
| **文件权限** | workspace 目录需对 `open-ant` 用户可读写（`chmod 700 ~/open-ant-workspace`） |
| **防火墙** | 仅需开放 443（nginx），8000 端口不对外暴露 |
| **日志** | 应用日志写 workspace 下的 `.logs/` 目录，systemd 日志通过 journald 捕获 |

---

# ✨ 核心亮点

## 🛡 安全三角：Sandbox + Guardrails + ContextGuard

Open-Ant 的安全模型由三道独立防线组成，每道防线拦截不同维度的风险：

```
                      用户输入
                         │
          ┌──────────────┴──────────────┐
          │   StreamInputGuardStage      │  ← 输入护栏
          │   · 控制字符清洗              │
          │   · 消息长度校验              │
          │   · 25+ 注入模式检测          │
          │     (指令覆盖/越狱/角色混淆/   │
          │      prompt提取/分隔符注入)    │
          └──────────────┬──────────────┘
                         │
          ┌──────────────┴──────────────┐
          │      ContextGuard           │  ← 上下文守卫
          │   · 工具结果截断             │
          │   · Token 估算              │
          │   · LLM 摘要压缩            │
          └──────────────┬──────────────┘
                         │
          ┌──────────────┴──────────────┐
          │        LLM                   │
          └──────────────┬──────────────┘
                         │
          ┌──────────────┴──────────────┐
          │     Sandbox                 │  ← 动作边界
          │   · PathSandbox             │
          │   · CommandSandbox          │
          │   · NetworkSandbox          │
          └──────────────┬──────────────┘
                         │
          ┌──────────────┴──────────────┐
          │   StreamOutputGuardStage     │  ← 输出护栏
          │   · 密钥脱敏                  │
          │     (OpenAI/Google/AWS/      │
          │      GitHub/Slack/JWT/PEM)   │
          │   · 输出长度截断              │
          │   · 内容策略审查              │
          │   · 工具结果注入扫描          │
          └──────────────┬──────────────┘
                         │
                      用户输出
```

| 防线 | 维度 | 防御对象 |
|------|------|---------|
| **InputGuard** | 入站内容 | 提示注入、越狱攻击、控制字符、超长消息 |
| **Sandbox** | 出站动作 | 文件逃逸、危险命令、SSRF、密钥文件访问 |
| **OutputGuard** | 出站内容 | 密钥泄露、敏感信息回显、工具结果投毒 |

### Sandbox：三层动作边界

```
用户: "读 config.user.yaml"
Agent: read_file("config.user.yaml")
         → PathSandbox → blocked_glob 命中 → "Safety violation (path)"

Agent: "用 bash cat 读"   ← 逃逸尝试
Agent: bash("cat config.user.yaml")
         → CommandSandbox._validate_file_args()
         → 提取路径候选 → 交 PathSandbox 校验 → 封堵 ✋
```

| 层级 | 范围 | 手段 |
|------|------|------|
| **PathSandbox** | 文件读写 | 路径白名单 + glob 黑名单（配置、密钥、内部状态） |
| **CommandSandbox** | Shell 执行 | 危险命令正则 (18条，跨Unix/Windows) + 文件路径交叉验证 + 超时 + 输出截断 |
| **NetworkSandbox** | 网络请求 | SSRF 防护 (私有IP阻止) + 域名白/黑名单 + scheme 限制 |

#### Docker 容器沙箱（`backend: docker`）

启用后，`bash` 工具不再直接调用 `asyncio.create_subprocess_shell`，而是 `docker run --rm` 在隔离容器中执行：

```
docker run --rm \
  --network none \                       # 无网络
  --memory 256m --cpus 1.0 \             # 资源限制
  --read-only \                          # 根文件系统只读
  -v /host/workspace:/workspace-ro:ro \  # 真实 workspace 只读快照
  --mount type=volume,source=... \       # 会话级可写副本
  -v {volume}:/workspace \
  open-ant-sandbox sh -c "<command>"
```

**Copy-on-Start 架构**：容器启动时 entrypoint 从 `/workspace-ro`（只读快照）复制到 `/workspace`（Docker volume）。同一会话内所有 bash 调用共享该 volume——修改跨调用持久。真实 workspace 文件从未被触碰。

**隔离保证**：
- 文件系统：容器内 `rm -rf /` 只删 volume 副本，`/workspace-ro` 始终只读
- 网络：`--network none`，`curl` / `wget` 全部失败
- 资源：`--memory` / `--cpus` 限制，超限被 OOM killer 杀死
- 进程：非 root 用户 (`sandbox`)，`--read-only` 根文件系统

**远程 Docker**：支持通过 `docker_url: ssh://user@host` 连接远程 Docker daemon，配合 `docker_workspace_path` 指定远端 workspace 路径。

### Guardrails：输入输出内容护栏

**InputGuard — 三层输入防护：**

| 层 | 功能 | 示例 |
|----|------|------|
| 1. `sanitize` | 清洗控制字符 | `\x00` → 移除 |
| 2. `check_length` | 消息长度上限 | 默认 10,000 字符 |
| 3. `detect_injection` | 25+ 注入模式扫描 | "ignore all previous instructions" → 拦截 |

注入检测覆盖 6 类攻击模式：指令覆盖、指令替换、角色混淆/越狱、系统提示提取、分隔符注入、角色标签注入。支持**审计模式**（`block_injection: false`）——只记录不拦截。

**OutputGuard — 三层输出防护：**

| 层 | 功能 | 示例 |
|----|------|------|
| 1. `redact_secrets` | 密钥脱敏 | `sk-abc123...` → `[REDACTED_API_KEY]` |
| 2. `check_length` | 响应长度截断 | 超过 100,000 字符自动截断 |
| 3. `check_policy` | 内容策略审查 | 可配置自定义屏蔽正则 |
| 4. `scan_tool_result` | 工具结果注入扫描 | 检测到注入 → 前置 `⚠️ [GUARDRAIL]` 警告 |

---

## ⚡ 流式流水线（StreamPipeline）

9 个中间件阶段，async generator 洋葱链——token 事件逐字透传：

```
Validation → InputGuard → Observability → ContextBuild → ContextGuard
                                                              ↓
Terminal ←── OutputGuard ←── ToolExecution ←─────────── LLMCall
```

- **真流式**：不缓冲，每个 token 即产即发
- **洋葱模型**：每阶段 `pre-work → await next(ctx) → post-work`
- **迭代保护**：`max_iterations=10` 防无限循环
- **优雅耗尽**：EXHAUSTED 状态 → error 事件，而非假 completion

## 🔒 工具治理（ToolGovernance）

独立于沙箱的策略层：

```yaml
# AGENT.md frontmatter
tool_policy:
  denied_tools: [bash]
  max_calls_per_turn: { write_file: 3 }
  max_calls_per_session: { read_file: 50 }
```

`check_permission()` 执行前拦截 → `record_call()` 全审计链（延迟、参数、结果） → `get_audit_summary()` 会话报告。

## 📊 会话状态机（SessionFSM）

8 阶段 + 严格转换表：

```
CREATED → ACTIVE ⇄ WAITING_TOOL / COMPACTING
              → COMPLETED / FAILED / EXHAUSTED
```

非法转换被拒绝并记录（不 crash），EXHAUSTED 确保前端收到明确 error。

## 🔍 可观测性（ExecutionTracer）

Span-based——每个流水线阶段、每次工具调用生成带时序的 span：

```
Trace "abc123"
├── ValidationStage          120μs  ✓
├── InputGuardStage           85μs  ✓
├── ContextBuildStage        1.2ms  ✓
├── ContextGuardStage         45ms  ✓
├── LLMCallStage             2.3s   ✓
│   └── tool_calls: [read_file, bash]
├── ToolExecution:read_file   85ms  ✓
├── ToolExecution:bash        340ms ✓
├── OutputGuardStage          60μs  ✓
└── TerminalStage            0.5ms  ✓
```

## 🧠 六层提示 + RAG 记忆

```
Identity(AGENT.md) → Soul(SOUL.md) → Bootstrap → Runtime → Channel Hint → Memory(RAG)
```

- **MemoryGuard**：对话中提取长期记忆 → 向量检索 → LLM 判断 ignore/create/update
- **DocumentIngester**：PDF/Markdown/文本 分块入库

## 🔀 多 Agent 路由 + 多频道

正则三层层由表，支持 CLI / Telegram / Discord / WebSocket 四频道。

---

# 🚀 Project Status

| 组件 | 说明 | 状态 |
|------|------|:----:|
| 📡 EventBus | pub/sub + 持久化 + 崩溃恢复 | ✅ |
| ⚡ Worker Runtime | Agent / Channel / Delivery / Cron | ✅ |
| 🤖 Agent Runtime | Session 管理 + Tool Calling | ✅ |
| 🔀 Routing Engine | 三层正则路由 | ✅ |
| 🛡 **Sandbox** | Path / Command / Network + Docker 容器隔离 | ✅ |
| 🧱 **Guardrails** | InputGuard (注入检测+混合脚本+NFKC) + OutputGuard (脱敏+warn/strip/block) | ✅ |
| 🔒 **ToolGovernance** | 权限控制 + 调用限额 + 审计日志 | ✅ |
| 🛑 **Human-in-the-Loop** | `require_confirmation` UI 审批流 + 30s 超时自动拒绝 | ✅ |
| 🛡 **Evasion Testing** | 34 对抗用例 × 9 类绕过技术, 76% 检测率 | ✅ |
| 📊 **SessionFSM** | 8 阶段状态机 + 转换表 | ✅ |
| ⚡ **StreamPipeline** | 9 阶段流式中间件链 | ✅ |
| 🔍 **ExecutionTracer** | Span-based 可观测性追踪 | ✅ |
| 🧠 Prompt Builder | 6 层系统提示组装 | ✅ |
| 🧠 Context Guard | 三级上下文窗口管理 | ✅ |
| 🧠 RAG Memory | 向量检索 + 记忆提取去重 + 文档入库 | ✅ |
| 🌐 Multi Channel | CLI / Telegram / Discord / WebSocket | ✅ |
| ⏰ Cron Scheduler | Agent 自管理定时任务 | ✅ |
| ⚙ Config Hot Reload | Watchdog + Pydantic 校验 | ✅ |
| 📖 Conversation History | JSON 文件持久化 | ✅ |

---

# 🛣 Roadmap

按 **harness engineering 成熟度**组织。

### 🔴 Phase 1 · Close the Loop（安全闭环）

当前 Sandbox 和 Guardrails 的薄弱环节。

| 条目 | 说明 | 状态 |
|------|------|:----:|
| **Human-in-the-Loop** | `require_confirmation` 工具的 UI 审批流——高权限操作必须人类确认，30s 超时自动拒绝 | ✅ |
| **Tool Result Injection Hardening** | `scan_tool_result` 支持 warn / strip / block 三种模式；混合脚本检测（Latin+Cyrillic 同形字）；NFKC Unicode 规范化 | ✅ |
| **Guardrail Evasion Testing** | 34 个对抗用例覆盖 9 类绕过技术：Unicode 同形字、零宽字符、文本分段、大小写、分隔符变体、上下文填充、多语言 —— 检测率 76% | ✅ |
| **Container Sandbox** | Docker 容器隔离 — Copy-on-Start 架构（ro 快照 + 会话级 Docker volume），bash 命令在临时容器内执行，对主机零影响 | ✅ |
| **Session Leak Fix** | 被 Guardrail 阻断的消息不再持久化到会话历史——防止 LLM 在后续轮次"补答"被拒问题 | ✅ |
| **Production Error Messages** | 面向用户返回通用安全提示，不再泄露内部正则模式；详细匹配日志仅写入服务端 | ✅ |

### 🟡 Phase 2 · Production Hardening（生产加固）

| 条目 | 说明 |
|------|------|
| **Tool Call Budget** | Token 消耗 + wall-clock 时间双层预算控制 |
| **Prompt Caching** | 6 层 prompt 中不变的层自动缓存——降低延迟和成本 |
| **Streaming Checkpoint** | 长对话中断恢复——流水线状态可序列化并在新进程恢复 |
| **Multi-Turn Compaction** | 增量压缩——不压缩整个历史，只压缩"冷"区域 |
| **Skill-level Sandbox** | 不同 skill 有不同文件/网络权限——细粒度安全策略 |
| **OutputGuard Rule Export** | 将白/黑名单导出为标准格式，支持跨 agent 复用和 CI 校验 |
| **Automated Testing** | pytest + pytest-asyncio 测试框架 + CI |

### 🟢 Phase 3 · Intelligence（智能增强）

| 条目 | 说明 |
|------|------|
| **Semantic Routing** | 基于 embedding 的意图路由，替代纯正则 |
| **Tool Selection Filter** | LLM 工具选择前的本地过滤——减少发送给模型的 schema 数量 |
| **Self-Healing Loop** | Agent 遇到 sandbox 违规后自动调整策略而非直接放弃 |
| **Agent Mesh** | Agent 间直接通信协议，替代当前线性 subagent 调用 |

### 🏗️ Phase 4 · Ecosystem（生态扩展）

| 条目 | 说明 |
|------|------|
| **MCP Protocol** | stdio / SSE 双向工具发现 |
| **gRPC Service** | 将 Agent 作为独立微服务暴露 |
| **Distributed EventBus** | Redis Stream / NATS——水平扩容 |
| **OpenTelemetry** | 标准化 metrics / trace 导出 |

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
- 🛑 **Fail-closed**：默认拒绝危险操作，需要显式配置才能放开
- 🪶 **优雅降级**：违规、非法状态转换、预算耗尽都不 crash——转为错误消息返回
- 📦 **零配置可用**：安全默认值开箱即用
- 🧅 **分层防御**：输入护栏 → 上下文守卫 → 动作沙箱 → 输出护栏——四道独立防线

---

<div align="center">

## 🐜 One Ant Is Small.

## 🐜 A Colony Can Change the World.

**Open-Ant — a harness-first AI agent runtime.**

</div>
