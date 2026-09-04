# 🐜 OpenAnt-MemoryArk · 记忆方舟

> **当人的大脑开始遗忘这个世界的时候，世界不要因此把这个人也遗忘掉。**

**OpenAnt-MemoryArk 是一个 24/7 常驻的个人 AI 助手运行时**：事件驱动多智能体内核、图增强长期记忆、深度安全治理，全部组件在真实基础设施（MySQL / RabbitMQ / Redis / Qdrant / Neo4j）上集成验证，**431 个自动化测试 + CI 门禁**。

它的第一个应用场景是**记忆方舟（Memory Ark）**——面向阿尔茨海默病家庭照护的认知辅助原型：替老人保管正在消失的外部世界（人物关系、人生事件、生活习惯），在困惑时温柔重锚定现实，在记忆异常时向家人发出信号。

> ⚠️ 诚实声明：记忆方舟是**技术概念验证**，不是医疗产品——不做诊断、不提供用药建议、不宣称疗效、永不扮演真人亲属。真实照护场景需要医疗合规与人工监督。场景设计见 [`memory-ark.md`](./memory-ark.md)。

> PyPI 已发布生产级版本：`pip install open-ant-harness`（v0.2.0）

---

## 场景速览：记忆方舟的一天

```text
09:00  小安（语音）："陈奶奶，该吃早饭了，今天周三。"
10:30  老人："你是谁？"
       小安："我是小安，陪了你三年的伙伴。你的女儿叫 Emily，
             她小时候常陪你去公园，她今天下午会来看你。"
       老人："我想不起来……"
       小安："没关系。我会替你记着。"
19:00  家人收到日报（Telegram）："本周'我女儿在哪'重复询问 14 次，
       较上周 +40%；服药提醒完成 6/7 次。建议本周复诊时告知医生。"
```

一个场景，三层技术：**记忆**（人生档案 = Neo4j 记忆图 + 混合检索）、**对话**（温柔重锚定 = 人格配置 + 检索注入）、**守护**（信号日报 = cron 定时任务 + 多通道投递）。每一步都有评测背书：记忆问答跑 LongMemEval（ICLR 2025，500 题），"不知道要承认"有 abstention 纪律，高风险行动必须人工确认，注入护栏有检出率/误杀率数字。

## 为什么是 OpenAnt：这个品类需要生产级答案

2026 年，always-on agent 成为行业公认品类（OpenClaw 引爆，Gemini Spark / 微软 Scout 跟进）。但把电脑与账号权限交给模型，信任问题随之而来：安全厂商对 OpenClaw 公开警告（CVE-2026-25253 网关劫持 CVSS 8.8）、恶意 skill 市场、prompt injection 造成真实事故。在记忆方舟的场景里，这些问题不是"安全合规"，是底线——**一个记错亲人的助手不是 bug，是伤害**。

OpenAnt 为回答这个信任问题而建，每个承诺都有测试和数字（见下方核心能力）：

- **消息不丢**：RabbitMQ durable 队列 + DLX 五级重试 + outbox 同事务 + 消费端幂等
- **权限可控**：三层沙箱 / 确认审批（HITL）/ 全链路审计
- **记忆可仲裁**：Neo4j 冲突检测与 LLM 仲裁 + LongMemEval 评测
- **全程可观测**：OTel 跨 Agent 链路追踪 + Prometheus 指标

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
# 1. 安装（PyPI 生产级版本 v0.2.0）
pip install open-ant-harness
# 源码安装（开发/最新能力）：
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
- **自带评测**：`python -m evals.run_retrieval_eval` —— 20 篇中文语料 × 30 条标注查询，dense **0.983** / hybrid(RRF) **0.983**（bge-small-zh 下追平）/ +rerank **0.967**（recall@5，报告可复现）；长期记忆问答在 LongMemEval（ICLR 2025）上评测（见 `eval.md`）；中文稀疏模型对照实验见 `evals/report_sparse_zh.md`

### 安全
- 三层沙箱：路径（阻断配置/密钥）· Docker 命令（`--user` 非 root、内存/CPU 硬限、只读根文件系统）· 网络（SSRF 防御、域名黑白名单+私有 IP 阻断）
- 输入护栏（NFKC 规范化/混合脚本检测/regex 注入 + **LLM-judge 语义复核**）+ 输出护栏（**流式脱敏**：滑动缓冲先审后出）+ 工具结果注入扫描
- WS/API token 认证（常量时间比较、4401 拒绝）、确认审批 fail-closed 绑定、Redis 滑窗限流（挂则放行）
- **护栏有评测**：20 恶意 + 20 良性样本集，真实数字**检出率 85%、误杀率 0%**（`python -m evals.run_guardrail_eval --ci`，CI 门禁 ≥60% 且 ≤20%）
- 凭据纪律：密钥仅存 `.env`，日志/测试/文档零泄露（发布有 check_publish 泄露扫描门禁）

### 工程
- **431 个自动化测试**（pytest）+ ruff + GitHub Actions CI；含真实 MySQL / RabbitMQ / Qdrant 云 / Neo4j Aura 集成测试（无凭据环境自动 skip）
- **发布门禁**（`check_publish.py`）：密钥形态扫描 + 文件名黑名单，0.1.0 密钥泄露事故后强制流程，发布前非零禁止上传
- 演进可回溯：26.1 玩具 → 27.0 止血+测试 → 28.0 存储/消息 → 29.0 LLM/工具 → 30.0 记忆 → 31.0 安全/可观测 → 32.0 收尾 → 35.x 评测/追踪（`git log` 每步可复现）

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
│   └── tests/            # 431 测试
├── evals/                # 检索/护栏/LongMemEval 评测（语料/指标/对照 runner/报告）
└── pyproject.toml        # 打包/测试/lint 配置（sdist allowlist 防密钥泄露）
```

## 测试与评测

```bash
cd src
python -m pytest -q                        # 431 passed
ruff check ant                             # 0 错误
python -m evals.run_retrieval_eval         # 检索三方法对照 → evals/report_retrieval.md
python -m evals.run_guardrail_eval --ci    # 注入护栏检出率/误杀率（CI 同款门禁）
python -m evals.agent_task_runner          # 10 个记忆任务离线骨架评分
python -m evals.sparse_zh_experiment       # 中文稀疏模型真云对照实验
python check_publish.py                    # 发布前密钥扫描门禁（非零禁止上传）
```

## 架构与面试题

仓库内 [`interview.md`](./interview.md) 整理了**项目设计与面试题集**：
- **Project 部分**——系统全景图与关键流程的完整流转路径（事件流转 / Agent Loop 与 LLM Loop / 记忆写入 / 检索 / Context 与 Prompt 组装 / 工具执行 / SubAgent / 配置与凭据 / 基础设施角色速查 / Trace 流转），每个流程定位到具体文件，帮助快速熟悉代码库；
- **Interview 部分**——Agent 方向面试题与基于真实实现的回答大纲（含可追问的代码落点与诚实的边界声明）。

欢迎分享给更多人，也欢迎在 issue 里补充新的面试题。

## 参与贡献

如果你也对这个方向感兴趣——agent 运行时、图增强记忆、评测体系，或者记忆方舟这样的认知辅助场景——**欢迎加入贡献者**。好的开始：跑通[快速开始](#快速开始)，从 issue 认领一个小任务，或者直接提交 PR。代码规范很简单：`pytest` 全绿 + `ruff check ant` 零错误。

如果您希望这个项目被更多人看到、被服务商或研究机构资助，**请点亮 star ⭐**——star 是开源项目最直接的认可信号，也是它被看见、被资助的第一步。

## 许可（双许可）

- **代码**（`ant/`、`evals/` 等）：[MIT License](./LICENSE)
- **叙事与文档**（README、`memory-ark.md`、`interview.md` 等）：[CC BY-SA 4.0](./LICENSE-DOCS)（署名-相同方式共享）——转载、改编需保留出处并以相同许可发布。许可保护的是叙事文本的表达本身（想法不受版权保护，但"记忆方舟"这段叙事的表达属于本项目）。

## 当前边界（诚实声明）

- 单机单进程模型：EventBus 已基于 RabbitMQ 可横向扩展，worker 多副本部署为后续方向
- 多用户隔离为单用户模型（认证保护端点，session 级多用户绑定留待扩展）
- docker-compose 全栈已提供（含 healthcheck 门控启动），待实机环境做最终验证
- 记忆方舟场景为概念验证：虚构 persona 演示、无真实患者数据、无医疗合规——详见 `memory-ark.md`
