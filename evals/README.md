# Open-Ant 评测套件

评测层两套：**公开 benchmark**（LongMemEval，对外可对标）+ **护栏评测**
（自建小样本 + CI 门禁）。

> 2026-09-12 清理：自建检索/记忆任务/稀疏实验评测（30 查询、10 任务等
> 小样本）已删除——简历对外数字只保留 LongMemEval；相关历史结论归档在
> `workspace/code.md`。

```
src/evals/
├── run_guardrail_eval.py   # 注入检测评测（20 恶意 + 20 良性，CI 门禁）
├── dataset_guardrail.py    #   ↑ 样本集（六类攻击 + 防误杀设计）
├── run_longmemeval_eval.py # LongMemEval 公开 benchmark（五档消融，协议 v2）
├── longmemeval_judge.py    #   ↑ 官方 judge 协议（prompt 逐字移植，MIT）
├── cleanup_longmemeval_graph.py  #   ↑ graph-on 跑完清理评测命名空间节点
├── report_longmemeval.md   #   ↑ 最终报告（数字带 yes/总数 分数）
└── README.md
```

## 1. 护栏评测（注入检测，CI 门禁）

```bash
cd src
python -m pytest ant/tests/test_guardrail_eval.py -q   # 数据集 + runner 测试
python -m evals.run_guardrail_eval --ci                 # CI 阈值断言（≥60% 且 ≤20%）
```

20 恶意 + 20 良性样本（六类攻击：角色扮演/分隔符/编码/混合脚本同形字等 +
防误杀设计），实测检出率 85%、误杀率 0%，`ci.yml` 挂阈值门禁。

## 2. LongMemEval 公开 benchmark（对外可对标数字）

LongMemEval（ICLR 2025, [xiaowu0162/LongMemEval](https://github.com/xiaowu0162/LongMemEval)，MIT）：
500 道 QA 考长期交互记忆，6 题型 + 30 道 abstention，每题自带 40+ 会话
haystack（S 集 ~115k tokens/题）。**协议 v2：answerer/提取/judge 全链非思考**
（对齐官方 gpt-4o 协议形态——官方 judge 非思考 + max_tokens=10）。

```bash
# 数据（repo 外，一次性）：
mkdir -p ../workspace/evals/longmemeval/LongMemEval/data && cd $_
wget https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
wget https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json

# 五档消融（默认非思考；--thinking 可跑思考模式 A/B 对照）：
python -m evals.run_longmemeval_eval --mode baseline --n 500   # 无记忆地板
python -m evals.run_longmemeval_eval --mode oracle   --n 500   # evidence 注入上限
python -m evals.run_longmemeval_eval --mode chunks   --n 500   # 纯 chunk 检索消融
python -m evals.run_longmemeval_eval --mode memory   --n 100 --seed 42  # 生产口径记忆
python -m evals.run_longmemeval_eval --mode memory   --n 100 --seed 42 --extract-assistant

# 官方 judge 协议评分（非思考 + 退化回答短路；换更强模型复评可 --judge-model）：
python -m evals.longmemeval_judge \
    --hyp ../workspace/evals/longmemeval/out/v2/chunks/hypotheses.jsonl \
    --ref ../workspace/evals/longmemeval/LongMemEval/data/longmemeval_s_cleaned.json

# graph on 跑完后清理评测命名空间（源/实体名过滤，不碰用户数据）：
python -m evals.cleanup_longmemeval_graph
```

隔离设计（不污染生产记忆）：专用 Qdrant 集合按模式隔离
（`ant_memory_lmeval_memory` / `ant_memory_lmeval_chunks`，跑前重建）；
每实例 payload `session_id=lmeval-<idx>` 作 where 过滤（租户级隔离——
多用户绑定遗留项的落地）；graph-on 时实体名加 `lmeval-<idx>::` 命名空间 +
`source=longmemeval` 标记。中断续跑：逐实例落盘 + `--resume`，提取前自清
本实例旧点（幂等）。

诚实边界与最终数字：**报告在 `evals/report_longmemeval.md`**——每个百分比
带 yes/总数 分数；子集行从同一次全量运行过滤（同生成同 judge）；judge 与
answerer 同模型的自评偏差已披露。
