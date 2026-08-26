# 🐜 Open-Ant

**生产级 LLM 多智能体运行时** —— 事件驱动内核 + 图增强记忆 + 深度安全治理，全部组件在真实基础设施（MySQL / RabbitMQ / Redis / Qdrant / Neo4j）上集成验证，**378 个自动化测试 + CI 门禁**。

> 可通过 PyPI 体验：`pip install open-ant-harness`（线上为早期版本；生产级能力见本仓库，Phase 5 收尾后发布新版）

---

## 架构

```text
                     CLI │ Telegram │ Discord │ WebSocket(认证)
                        │            │          │
                        ▼            ▼          ▼
                ┌───────────────────────────────────────┐
                │          CompositeBus                  │
                │   持久事件 ──► RabbitMQ / Outbox       │
                │   (durable队列 + DLX五级重试 + DLQ)    │
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

## 快速开始

```bash
# 1. 安装（PyPI 或源码最新版）
pip install open-ant-harness
# 源码安装（含全部生产级能力）：
git clone https://github.com/Fair-Fair-Fair/open-ant.git && cd open-ant
pip install -e src

# 2. 初始化 workspace（会生成 config.user.yaml 与默认 agent）
open-ant init --workspace ./workspace

# 3. 配置凭据（open-ant/.env，变量名如下——值全部自行填写，代码绝不打印凭据）
#    LLM: DEEPSEEK_API_KEY / LLM_MODEL_ID / BASE_URL
#    基础设施: MYSQL_USERNAME / MYSQL_PASSWORD / RABBITMQ_USERNAME / RABBITMQ_PASSWORD
#    记忆: QDRANT_URL / QDRANT_API_KEY / QDRANT_COLLECTION / QDRANT_VECTOR_SIZE
#          NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD / NEO4J_DATABASE
#    Redis 本机无密码默认 redis://127.0.0.1:6379/0
#    说明：MySQL/RabbitMQ 凭据缺失时自动回退 JSONL/内存总线并在 doctor 中报 ERROR

# 4. 启动自检（九项：config/路由/agent/docker/history/mysql/rabbitmq/磁盘）
open-ant doctor --workspace ./workspace

# 5. 开聊
open-ant chat --workspace ./workspace              # CLI 对话
open-ant chat --workspace ./workspace --agent pickle
open-ant server --workspace ./workspace            # 24/7 服务（WebSocket/Telegram/Discord/cron）
open-ant ingest ./docs/myfile.pdf --workspace ./workspace   # 文档入库
open-ant migrate-chroma --workspace ./workspace    # Chroma → Qdrant 迁移
```

**基础设施依赖**：MySQL / RabbitMQ / Redis 建议本机或容器（默认端口 3306/5672/6379）；Qdrant / Neo4j 用 `.env` 里配置的云服务或自建。缺凭据不阻塞启动——对应能力降级并有明确告警，`doctor` 会告诉你缺了什么。

## 核心能力

### 可靠性（消息不丢，失败可恢复）
- **RabbitMQ**：durable 队列 + manual ack + **DLX 五级 TTL 重试阶梯**（5s→30min）+ 死信队列；投递失败 nack 重试，消费端 `processed_messages` **幂等去重**（at-least-once 语义，真实 broker 集成测试验证）
- **Outbox 模式**：事件与业务状态同事务落 MySQL，publisher confirm 后标记——进程崩溃不丢消息
- **LLM 层**：litellm Router 重试/超时/模型降级链；每会话 token/成本记账（`usage_records` 可查）；上下文阈值按模型动态计算，压缩失败硬截断兜底
- **优雅停机**：publisher→workers→bus→uvicorn→存储引擎顺序 drain；worker 停机 15s 超时兜底；崩溃 worker 指数退避重启（5s→120s）

### 图增强记忆（不只是向量库）
- **Qdrant**：dense + BM25 sparse 双命名向量、服务端 prefetch + RRF 融合、payload filter、payload 索引自动创建
- **Neo4j 记忆图**：实体/关系建模、**冲突检测与 LLM 仲裁**（SUPERSEDES 边）、低重要度记忆软归档 TTL
- **检索管线**：query 改写 → hybrid → 子图扩展 → cross-encoder 重排 → `<retrieved>` 定界符防注入
- **提取层**：工具调用约束 JSON（单条坏数据不连坐整批）
- **自带评测**：`python -m evals.run_retrieval_eval` —— 20 篇中文语料 × 30 条标注查询，dense **0.983** / hybrid 0.917 / +rerank **0.967**（recall@5，报告可复现）

### 安全
- 三层沙箱：路径（阻断配置/密钥）· Docker 命令（`--user` 非 root、内存/CPU 硬限、只读根文件系统）· 网络（SSRF 防御、域名黑白名单+私有 IP 阻断）
- 输入护栏（NFKC 规范化/混合脚本检测/regex 注入 + **LLM-judge 语义复核**）+ 输出护栏（**流式脱敏**：滑动缓冲先审后出）+ 工具结果注入扫描
- WS/API token 认证（常量时间比较、4401 拒绝）、确认审批 fail-closed 绑定、Redis 滑窗限流（挂则放行）
- 凭据纪律：密钥仅存 `.env`，日志/测试/文档零泄露（发布有泄露扫描门禁）

### 工程
- **378 个自动化测试**（pytest）+ ruff + GitHub Actions CI；含真实 MySQL / RabbitMQ / Qdrant 云 / Neo4j Aura 集成测试（无凭据环境自动 skip）
- 演进可回溯：26.1 玩具 → 27.0 止血+测试 → 28.0 存储/消息 → 29.0 LLM/工具 → 30.0 记忆 → 31.0 安全/可观测（`git log` 每步可复现）

## 目录结构

```
src/                      # git 仓库根
├── ant/
│   ├── core/             # 管线/守卫/路由/上下文/FSM/追踪
│   ├── server/           # workers/auth/限流/可观测性/app
│   ├── bus/              # EventBus 协议 + InMemory/RabbitMQ/Composite/Outbox
│   ├── storage/          # SQLAlchemy 模型/仓库/Alembic 迁移
│   ├── memory/           # Neo4j 记忆图/约束提取/重排
│   ├── provider/         # LLM Router/Qdrant/embedding(Redis 缓存)/检索
│   ├── tools/            # 内置工具/策略治理/审计
│   ├── channel/ cli/ utils/
│   └── tests/            # 378 测试
├── evals/                # 检索评测（语料/指标/对照 runner/报告）
└── pyproject.toml        # 打包/测试/lint 配置（sdist allowlist 防密钥泄露）
```

## 测试与评测

```bash
cd src
python -m pytest -q                 # 378 passed
ruff check ant                      # 0 错误
python -m evals.run_retrieval_eval  # 检索三方法对照 + 报告写入 evals/report_retrieval.md
```

## 当前边界（诚实声明）

- 单机单进程模型：EventBus 已基于 RabbitMQ 可横向扩展，worker 多副本部署为 Phase 5 待办
- 多用户隔离为单用户模型（认证保护端点，session 级多用户绑定留待扩展）
- PyPI 线上版本为早期版，生产级代码将在 Phase 5 收尾后发布
