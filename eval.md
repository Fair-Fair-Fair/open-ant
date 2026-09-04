# Open-Ant 基准测试计划（Benchmark Plan）

> 动机：面试被问"你怎么评判你的 Agent 执行任务的好与坏"——只答"自建数据集 +
> 人工/LLM judge 对比"太弱。自建小样本评测（检索 30 查询 / guardrail 40 样本 /
> 10 任务）**无法对外对标**。本文件是公开 benchmark 接入的完整计划与执行状态，
> 目标是产出可写进简历的数字："我们在 LongMemEval（ICLR 2025）上跑出 X%，
> 对比无记忆基线 +Y%"。
>
> 状态：**Phase 7 进行中**（2026-09-02）。LongMemEval 基础设施 + 两个模式已出数；
> 剩余运行见「恢复执行」。GAIA / RULER 为后续阶段。详细逻辑见 `workspace/code.md`
> Phase 7 章节（对应提交 `d1a1a7d` / `be046e1` / `976d328` / `5824618`）。

---

## 1. 评估体系设计（面试口径）

三层，缺一不可：

| 层 | 作用 | 本项目现状 |
|---|---|---|
| **公开 benchmark**（对外对标） | 数字与论文/排行榜可比，证明"不是自说自话" | LongMemEval 进行中（本文档） |
| **消融实验**（证明机制） | 模型分数低没关系，机制增量（memory on/off、仲裁 on/off）才是工程能力的证据 | 四模式消融（baseline/oracle/memory/chunks）+ 后续 graph on/off |
| **自建回归**（防倒退） | 每次改动自动门禁，不追求对外意义 | 检索 recall@5=0.983 / guardrail 85%/FP 0% / offline tasks（CI 已接） |

一句话答案：**"我用公开 benchmark 证明系统有效（可对标）、用消融证明每个组件有贡献（可归因）、用自建 eval 做回归门禁（可迭代）。"**

## 2. 选型结论

| Benchmark | 测什么 | 决策 | 理由 |
|---|---|---|---|
| **LongMemEval**（ICLR 2025, MIT） | 长期记忆 QA：500 题 × 6 题型 + 30 abstention，每题 40+ 会话 haystack（S 集 ~115k tokens） | ✅ **进行中** | 直接打在记忆仲裁/提取的差异点上；knowledge-update 考记忆更新、abstention 考"不知道要承认"——正是 Neo4j 冲突检测 + 提取管线的用武之地；官方 judge 协议开源可复刻 |
| GAIA | 端到端个人助手任务（165 题 validation 公开） | ⏭ 下一阶段 | OpenClaw 同定位的"高考"；工具面已够（Tavily/web read/bash）；HF gated 授权（ModelScope 有免 gate 镜像）；多 agent vs 单 agent 消融是杀手锏 |
| RULER | 长上下文 needle/multi-hop | ⏭ 可选 | ContextGuard 压缩 trade-off 的公开尺子（compression ratio vs Δaccuracy 曲线） |
| SWE-bench | 编码 agent | ❌ 不做 | Docker/算力成本与项目定位（个人助手）不符 |

## 3. LongMemEval 执行状态

### 3.1 已完成 ✅

- **生产修复**（`d1a1a7d`）：发现并修复 `graph.ingest` 从未被调用（生产图是空的，记忆仲裁从未真实生效）+ 向量/图 id 对齐 + `where` 租户隔离过滤（`MemoryRetriever.retrieve/retrieve_semantic`、`MemoryGuard.extract_memories`）。
- **harness**（`be046e1`）：`evals/run_longmemeval_eval.py`（四模式 runner）+ `evals/longmemeval_judge.py`（官方 prompt 逐字移植，MIT）+ `evals/cleanup_longmemeval_graph.py`。37 个纯函数测试。
- **性能修复**（`5824618`）：批量 add（chunks 100 条/批、记忆按提取批聚合——全量 24.6 万 turn 逐条 upsert 需 7 小时）、逐实例增量落盘（可中断 + `--resume`）、提取 `max_tokens` 透传（大批次 4000）。
- **两模式已出数**（500 题全量，官方 judge 协议，deepseek-v4-flash）：

| 模式 | Overall | 含义 |
|---|---|---|
| baseline（无记忆） | **6.0%** | 地板 ≈ 30 道 abstention 得分，证明题离开历史确实答不了 |
| oracle（evidence 注入） | **62.6%** | 上限：信息无损时模型的答案能力 |

  oracle 分题型：assistant 98.2% / user 90.0% / knowledge-update 73.1% / multi-session 61.7% / **temporal 36.8%** / **preference 23.3%**（后两者是模型天花板，预先免责）。
  官方 GPT-4o 检索模式基线 57.7%——我们的 memory 管线数字落在 6%–62.6% 区间内，越接近上限越好。

### 3.2 踩过的坑（面试素材）

1. **推理模型吃光 judge 预算**：官方 `max_tokens=10`（gpt-4o 非推理够用），deepseek-v4-flash 是推理模型——10 token 被隐藏 reasoning 吃光（实测 212 token）、content 恒为空 → 500 题全判 False（0.0000%）。改 256 后正常。**教训：评测推理模型时输出预算必须给思考 token 留空间，否则判定协议静默失效。**
2. **向量维度与模型不符**：bge-small-**zh**-v1.5 是 **512 维**（≠ 英文 small 的 384），`.env` 旧值 QDRANT_VECTOR_SIZE=384 导致写入 400——用户已改 512。⚠️ 待查生产集合 `ant_memory` 是否按旧维度创建（若是，生产记忆写入一直在静默失败——异常被 `_maybe_extract_memories` 吞掉）。
3. 数据集比预期大一个数量级（24.6 万 turn），逐条 HTTP 写入不可行 → 批量化。

### 3.3 恢复执行（从这里继续）

数据（repo 外）：`workspace/evals/longmemeval/LongMemEval/data/longmemeval_s_cleaned.json`（277MB）+ `longmemeval_oracle.json`（15MB）。输出：`workspace/evals/longmemeval/out/<mode>/hypotheses.jsonl`。

```bash
cd src
# ① chunks 500（批量化版，约 40 分钟；已中断可 --resume 续跑）
python -m evals.run_longmemeval_eval --mode chunks --n 500 --resume

# ② memory 500 graph off（bs=12/conc=10/max_tokens=4000，约 1.5–2 小时；同样可 --resume）
python -m evals.run_longmemeval_eval --mode memory --n 500 --graph off --resume

# ③ （可选）memory graph on 对照：Neo4j Aura 实体名 lmeval-<idx>:: 命名空间隔离
python -m evals.run_longmemeval_eval --mode memory --n 500 --graph on --resume
# 跑完必须清理（只删评测命名空间，不碰用户数据）：
python -m evals.cleanup_longmemeval_graph

# ④ 官方 judge 评分（deepseek 默认；换更强模型 --judge-model 可复评）
python -m evals.longmemeval_judge \
    --hyp ../workspace/evals/longmemeval/out/chunks/hypotheses.jsonl \
    --ref ../workspace/evals/longmemeval/LongMemEval/data/longmemeval_s_cleaned.json
# 对 memory / baseline / oracle 同理
```

注意：`--resume` 依赖 hypotheses.jsonl 增量落盘；**全新运行会重建专用 Qdrant 集合 `ant_memory_lmeval`**（无 resume 时），所以 memory/chunks 的续跑必须带 `--resume`，否则集合被清、已入库实例的记忆丢失（续跑时 500 题里已写完的会跳过，未写的会重新提取入库）。

### 3.4 出报告（judge 全部跑完后）

1. 写 `src/evals/report_longmemeval.md`：四模式 × 分题型汇总表 + 消融分析（memory vs baseline 的 Δ = 记忆系统贡献；memory vs oracle 的差距 = 提取/检索损耗）+ 成本 + 诚实边界（提取只取用户消息 → assistant 类预期低分，`--extract-assistant` 可跑对照；批量时间戳近似；judge 与 answerer 同模型的自评偏差）。
2. `workspace/code.md` 追加最终数字（只追加）。
3. `src/interview.md`：新增/更新「怎么评判 Agent 好坏」Q&A——三层评估体系 + LongMemEval 数字。
4. `application.md`（简历）：bullet 示例——"在 LongMemEval（ICLR 2025，500 题）上以 deepseek 跑出 X%：无记忆基线 6.0% → 记忆管线 X%，oracle 上限 62.6%；记忆图入库缺陷（仲裁从未生效）由 benchmark 前验收发现并修复"。

## 4. 后续阶段计划（未开始）

### 4.1 GAIA（端到端）

- **数据**：HF `gaia-benchmark/GAIA`（gated，通常秒批）或 ModelScope 免 gate 镜像；validation 165 题（L1/L2/L3），官方 quasi-exact-match scorer 本地复刻（数字归一化/列表比对/字符串清洗）。
- **接入**：CLI channel 逐题发问 → Agent 自主多轮（Tavily 搜索 + web read + bash）→ 比对官方答案。
- **消融**：单 agent vs 多 agent（子代理并行研究）——harness 贡献的归因。
- **成本**：deepseek 全量 165 题约 $10–30；harness 工作量 1–2 天。
- **预期**：公开验证集 OWL 69% / 简单复刻 54.5%（L1 64%）/ GPT-4+plugins 15%；我们用 flash 预计 20–40%——**诚实报告 + 消融增量是叙事核心**。

### 4.2 RULER（ContextGuard 压缩 trade-off）

- 官方 repo API 直跑；ContextGuard off/on 对比 Δaccuracy + token 压缩比；成本 <$5，工作量半天。
- 产出："4× 压缩下任务精度保持 X%（对照不压缩 Y%）"。

## 5. 成本与预算汇总

| 项 | 预估 |
|---|---|
| LongMemEval 四模式（500 题） | 已花约 $2–5；剩余 memory 全量约 $5–10 |
| LongMemEval judge | 500 题 × 256 token 输出 ≈ $0.1（可忽略） |
| GAIA validation 165 题 | $10–30 |
| RULER | <$5 |

## 6. 诚实边界清单（写报告/面试时主动说）

1. 提取 prompt 只取**用户**消息（生产策略）——LongMemEval 的 single-session-assistant 类证据在助手侧，预期显著偏低；这是系统边界不是 bug。
2. 批量提取时时间戳取批内最大会话日期（批内先后近似）。
3. judge 与 answerer 用同一模型（deepseek-v4-flash），存在自评偏差；官方用 gpt-4o——有更强模型 key 时用 `--judge-model` 复评。
4. 报告必须披露子集大小（若用 `--n < 500`）与抽样种子。
5. baseline 6.0% ≈ abstention 得分——**这正是题目有效性的证据**，不是失败。
