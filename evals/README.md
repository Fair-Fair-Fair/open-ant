# Open-Ant 评测套件（Phase 3D）

秋招叙事里"有数字、有对照、可复现"的评测层：检索指标纯函数 + 人工标注的
中文检索数据集 + 多轮记忆任务集 + 可重复运行的 Qdrant 对照实验。

```
src/evals/
├── metrics.py              # recall@k / MRR / NDCG@k（纯函数，零依赖，可单测）
├── dataset_retrieval.py    # 20 篇中文语料（doc_01..doc_20）+ 30 条标注查询（q_01..q_30）
├── dataset_memory_tasks.py # 10 个多轮记忆任务（陈述 → 干扰 → 追问）
├── run_retrieval_eval.py   # CLI：连 Qdrant 跑对照实验，产出 report_retrieval.md
├── run_longmemeval_eval.py # Phase 7：LongMemEval 公开 benchmark（四模式消融）
├── longmemeval_judge.py    #   ↑ 官方 judge 协议（prompt 逐字移植，MIT）
├── cleanup_longmemeval_graph.py  #   ↑ graph-on 跑完清理评测命名空间节点
└── README.md
```

## 三个组件怎么跑

### 1. 指标单测（不依赖任何外部服务）

```bash
cd src
python -m pytest ant/tests/test_eval_metrics.py -q
```

覆盖 recall@k / MRR / NDCG@k 的手工算例（含空结果、全命中、部分命中、k 截断
边界），以及数据集的完整性校验（20 篇语料、每篇 150–300 字且关键词非空、
30 条查询的 ground truth 均引用存在的 doc id、记忆任务 3 轮结构）。

### 2. 检索对照实验（需要 Qdrant）

```bash
cd src
python -m evals.run_retrieval_eval                 # 全默认：auto embedding + rerank 尝试
python -m evals.run_retrieval_eval --embedder hash --no-rerank   # 纯降级冒烟
python -m evals.run_retrieval_eval --top-k 5
```

前置条件：

- `.env` 提供 `QDRANT_URL` / `QDRANT_API_KEY`（经 `ant.utils.settings.InfraSettings`
  读取，见脚本内 `_EvalSettings` 子类——不改动生产 settings）。
- `pip install qdrant-client`（Phase 3A 后成为项目正式依赖）。
- 可选：本地缓存了 `BAAI/bge-small-zh-v1.5`（真实 embedding）与
  `BAAI/bge-reranker-base`（rerank 列）。

行为：每次运行**重建**专用集合 `open_ant_retrieval_eval`（不碰 .env 里的生产
集合名）→ 切 chunk → 入库 → 跑 30 条查询 → 输出三列对照
（dense-only / hybrid(RRF) / +rerank）的 recall@5、MRR、NDCG@10 与逐查询
命中明细 → 写 `report_retrieval.md`（含时间戳）。脚本可重复运行，结果可复现。

### 3. 多轮记忆任务（Phase 5 接入 CI 的自动化素材）

```bash
cd src
python -c "from evals.dataset_memory_tasks import MEMORY_TASKS; print([t.task_id for t in MEMORY_TASKS])"
```

任务集本身是纯数据（可单测校验结构）。要真正执行需要完整 server + LLM：

1. `open-ant server --workspace ./workspace`（含记忆提取/注入管线）；
2. 对每个任务依次输入 `turns` 里的三条用户消息（第 1 轮陈述事实 → 第 2 轮
   干扰 → 第 3 轮追问）；
3. 人工/自动化检查第 3 轮的回答或注入的上下文是否包含 `expected_hits` 中
   的事实（每条都关联了检索数据集里的 doc_id，两套评估共用同一份 ground truth）。

> 标注：完整执行依赖 Phase 3A（Qdrant 记忆管线）/ Phase 3C（rerank）就绪；
> 当前作为**人工评分剧本**使用，Phase 5 接 CI 时改为会话级自动化断言。

## 指标定义（与 plan.md 评测条目一致）

相关性为二元（命中与否），ground truth 是 doc 级 id；检索结果先按
chunk→source doc 去重（保留最优名次）再计分。

| 指标 | 定义 | 说明 |
|---|---|---|
| recall@5 | `\|gt ∩ top5\| / \|gt\|` | 5 条结果里召回标注文档的比例 |
| MRR | 首个命中排名的倒数 | 排序质量的粗糙度量，对深命中惩罚重 |
| NDCG@10 | DCG/IDCG，增益 `2^rel-1`（二元即 1.0），按 `1/log2(rank+1)` 折扣 | 越靠前的命中权重越高，完全正确序为 1.0 |

聚合口径：30 条查询的宏平均（每条查询等权）。

## 当前状态与依赖矩阵

| 组件 | 状态 | 依赖 | 降级路径 |
|---|---|---|---|
| metrics.py | ✅ 完成、已单测 | 无 | — |
| dataset_retrieval.py | ✅ 完成（20 篇 / 30 条） | 无 | — |
| dataset_memory_tasks.py | ✅ 完成（10 任务） | 无 | — |
| run_retrieval_eval.py | ✅ 完成 | Qdrant（Phase 3A 引入） | 直连 `qdrant_client`；3A 的 `QdrantStore` 落地后替换 `_QdrantBackend` |
| embedding | ⚠️ 尽力而为 | Phase 3A embedding 层 | sentence-transformers 本地模型（bge-small-zh-v1.5）→ 不可用则 hash 伪向量（醒目警告，仅冒烟） |
| hybrid BM25 | ✅ 复用现有实现 | `ant.provider.memory.bm25_index` | 导入失败则 hybrid 退化为纯 dense |
| rerank | ⚠️ 尽力而为 | Phase 3C cross-encoder | 模型不可用 → 报告列显示 N/A |

诚实边界（面试口径）：**降级路径下的数字没有语义意义**，只证明管线能跑通；
语义结论以真实 embedding 跑出的 `report_retrieval.md` 为准。当前基线 commit
（36f1b60）时 Phase 3A 的 `QdrantStore` 尚未合并，故脚本走直连路径。

## 如何扩展数据集

1. **加语料**：在 `RETRIEVAL_DOCS` 追加一条 `RetrievalDoc`，`doc_id` 沿用
   `doc_21` 递增，正文保持 150–300 中文字，`keywords` 3–5 个（仅人工审查用，
   不进 ground truth）。完整性测试会校验字数与 id 唯一性。
2. **加查询**：在 `RETRIEVAL_QUERIES` 追加 `RetrievalQuery`，`ground_truth`
   只能引用已存在的 doc_id（测试强制），1–3 个；`query_type` 按
   specific / vague / rewrite / combined 标注，并写一句 `note` 说明设计意图。
3. **加记忆任务**：在 `MEMORY_TASKS` 追加 `MemoryTask`，`turns` 保持
   陈述→干扰→追问三段结构，`expected_hits` 尽量复用检索数据集里的 doc_id。
4. 跑 `python -m pytest ant/tests/test_eval_metrics.py -q` 验证数据完整性。

## 与 Phase 5 的衔接

- 检索：`run_retrieval_eval` 接 CI（有 Qdrant 服务时），报告进发布页面；
- 记忆任务集：转为会话级自动化断言（完整 server + LLM），与 Agent 任务集、
  guardrail 评估并列三套 eval；
- 全部依赖真实 embedding/rerank 的结论，以 3A/3C 合并后的自动切换为准。

## Phase 7：LongMemEval 公开 benchmark（对外可对标数字）

LongMemEval（ICLR 2025, [xiaowu0162/LongMemEval](https://github.com/xiaowu0162/LongMemEval)，MIT）：
500 道 QA 考长期交互记忆，6 题型 + 30 道 abstention，每题自带 40+ 会话
haystack（S 集 ~115k tokens/题）。与自建 30 查询/10 任务的区别：**数字可对外
对标**（GPT-4o 官方基线 57.7%）。

```bash
# 数据（repo 外，一次性）：
mkdir -p ../workspace/evals/longmemeval/LongMemEval/data && cd $_
wget https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
wget https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json

# 四模式消融（同一套题，先便宜后贵）：
python -m evals.run_longmemeval_eval --mode baseline --n 500   # 无记忆地板
python -m evals.run_longmemeval_eval --mode oracle   --n 500   # evidence 注入上限
python -m evals.run_longmemeval_eval --mode chunks   --n 500   # 纯 chunk 检索消融
python -m evals.run_longmemeval_eval --mode memory   --n 500 --graph off  # 完整记忆管线

# 官方 judge 协议评分（deepseek 默认；换更强模型复评可 --judge-model）：
python -m evals.longmemeval_judge \
    --hyp ../workspace/evals/longmemeval/out/memory/hypotheses.jsonl \
    --ref ../workspace/evals/longmemeval/LongMemEval/data/longmemeval_s_cleaned.json

# graph on 跑完后清理评测命名空间（源/实体名过滤，不碰用户数据）：
python -m evals.cleanup_longmemeval_graph
```

隔离设计（不污染生产记忆）：专用 Qdrant 集合 `ant_memory_lmeval`
（`QDRANT_COLLECTION` 覆盖 + 跑前重建）；每实例 payload
`session_id=lmeval-<idx>` 作 where 过滤（复用已有 KEYWORD 索引，Phase 7 起
`MemoryRetriever.retrieve(where=...)` / `MemoryGuard.extract_memories(where=...)`
支持租户级隔离——这也是多用户绑定遗留项的落地）；graph-on 时实体名加
`lmeval-<idx>::` 命名空间 + `source=longmemeval` 标记。

诚实边界：① 提取 prompt 只取用户消息（生产策略）——single-session-assistant
类证据在助手侧，预期显著偏低（`--extract-assistant` 可跑对照）；② 批量提取
时间戳取批内最大会话日期；③ judge 默认用配置模型（官方用 gpt-4o，自评偏差
披露于报告）。**报告在 `evals/report_longmemeval.md`（跑完 judge 后生成）。**
