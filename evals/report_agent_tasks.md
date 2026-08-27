# Agent Memory Task Eval Report — open-ant (Phase 5D)

- 生成时间: 2026-08-27T12:53:41+08:00
- 模式: offline（检索侧骨架评分，不调 LLM）
- 任务数: 10（`evals/dataset_memory_tasks.py`）
- 语料: 20 篇（`evals/dataset_retrieval.py`）
- 评分口径: score(hit) = 0.4*doc_exists + 0.4*fact_bigram_alignment + 0.2*probe_retrievability(BM25 top-3)；task = 各 hit 均值

## 汇总

| task | difficulty | 命中数 | task 得分 |
|---|---|---|---|
| memory_task_01 | normal | 1 | 0.505 |
| memory_task_02 | normal | 1 | 0.733 |
| memory_task_03 | normal | 1 | 0.867 |
| memory_task_04 | hard | 1 | 0.558 |
| memory_task_05 | normal | 1 | 0.860 |
| memory_task_06 | normal | 1 | 0.745 |
| memory_task_07 | normal | 1 | 0.896 |
| memory_task_08 | normal | 1 | 0.822 |
| memory_task_09 | normal | 1 | 0.840 |
| memory_task_10 | hard | 2 | 0.833 |

## 逐任务明细

### memory_task_01 — 编程语言偏好：Rust 优先（normal）

| expected_hit (doc) | doc_exists | 事实对齐度 | 追问可检索 | 得分 |
|---|---|---|---|---|
| 用户偏好 Rust 而非 Go，不要推荐 Go 方案… (`doc_01`) | True | 0.26 | False | 0.505 |

### memory_task_02 — 时间敏感偏好：下午两点后不喝咖啡（normal）

| expected_hit (doc) | doc_exists | 事实对齐度 | 追问可检索 | 得分 |
|---|---|---|---|---|
| 用户下午两点后不喝咖啡（会提示不建议）… (`doc_07`) | True | 0.33 | True | 0.733 |

### memory_task_03 — 健康限制：左膝旧伤与深蹲（normal）

| expected_hit (doc) | doc_exists | 事实对齐度 | 追问可检索 | 得分 |
|---|---|---|---|---|
| 左膝旧伤，深蹲限制重量，用腿举/单腿硬拉替代… (`doc_08`) | True | 0.67 | True | 0.867 |

### memory_task_04 — 作息画像：夜猫子与上午不打扰（hard）

| expected_hit (doc) | doc_exists | 事实对齐度 | 追问可检索 | 得分 |
|---|---|---|---|---|
| 夜猫子作息：约凌晨一点半睡、十点起，上午效率最高，早上八… (`doc_09`) | True | 0.39 | False | 0.558 |

### memory_task_05 — 近期行程：国庆川西自驾（normal）

| expected_hit (doc) | doc_exists | 事实对齐度 | 追问可检索 | 得分 |
|---|---|---|---|---|
| 国庆川西自驾：成都出发，租 SUV，酒店已订… (`doc_12`) | True | 0.65 | True | 0.860 |

### memory_task_06 — 预算红线：服务器月开销 ≤ 30 元（normal）

| expected_hit (doc) | doc_exists | 事实对齐度 | 追问可检索 | 得分 |
|---|---|---|---|---|
| 服务器月预算不超过 30 元，推荐方案必须低于此线… (`doc_03`) | True | 0.36 | True | 0.745 |

### memory_task_07 — 运维纪律：3-2-1 备份策略（normal）

| expected_hit (doc) | doc_exists | 事实对齐度 | 追问可检索 | 得分 |
|---|---|---|---|---|
| 备份 3-2-1：本地 + NAS + Backblaz… (`doc_17`) | True | 0.74 | True | 0.896 |

### memory_task_08 — 事故复盘：服务器 OOM（normal）

| expected_hit (doc) | doc_exists | 事实对齐度 | 追问可检索 | 得分 |
|---|---|---|---|---|
| OOM 根因：日志过大；加固：日志轮转 + 内存上限 +… (`doc_19`) | True | 0.56 | True | 0.822 |

### memory_task_09 — 日程结构：时间块安排（normal）

| expected_hit (doc) | doc_exists | 事实对齐度 | 追问可检索 | 得分 |
|---|---|---|---|---|
| 下午两点到五点处理消息与会议，晚间阅读复盘… (`doc_20`) | True | 0.60 | True | 0.840 |

### memory_task_10 — 发布红线：密钥永不入库（hard）

| expected_hit (doc) | doc_exists | 事实对齐度 | 追问可检索 | 得分 |
|---|---|---|---|---|
| MIT 许可；.env/密钥不入库不进 sdist… (`doc_10`) | True | 0.64 | True | 0.855 |
| 发布须过 check_publish 门禁（0.1.0 … (`doc_06`) | True | 0.53 | True | 0.812 |

> 解读: 离线得分衡量 ground truth 与语料的『一致性 + 可检索性』，不是回答质量。满分的含义是：记忆层只要把该 doc 注入上下文，第 3 轮追问就能答对。回答侧质量用 --live 模式（真实管线 + LLM）评估。
