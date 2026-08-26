# Retrieval Eval Report — open-ant (Phase 3D)

- 生成时间: 2026-08-26T19:51:17+08:00
- 集合: `open_ant_retrieval_eval`（每次运行重建，可复现）
- 语料: 20 篇 / 20 chunks（`evals/dataset_retrieval.py`）
- 查询: 30 条标注 query（ground truth doc 级）
- 向量维度: 512
- Embedding: sentence-transformers BAAI/bge-small-zh-v1.5 (dim=512)
- 降级状态: 真实 embedding，数字有语义意义
- Rerank (Phase 3C): 可用
- 指标口径: recall@5 / MRR / NDCG@10，doc 级去重后计算

## 汇总对照

| 方法 | recall@5 | MRR | NDCG@10 |
|---|---|---|---|
| dense-only | 0.9833 | 0.9028 | 0.9176 |
| hybrid (RRF) | 0.9167 | 0.8361 | 0.8574 |
| hybrid + rerank | 0.9667 | 0.9094 | 0.9137 |

## 逐查询明细（top-5 命中，doc id）

| query_id | query | gt | dense-only | hybrid(RRF) | +rerank |
|---|---|---|---|---|---|
| q_01 | 我写代码最讨厌啰嗦的错误处理，我是不是更吃系统级语言那一套？ | doc_01 | doc_05 doc_01 doc_18 doc_20 doc_19 | doc_01 doc_05 doc_18 doc_16 doc_20 | doc_01 doc_05 doc_18 doc_20 doc_09 |
| q_02 | 我的个人助手项目对外宣传的话，应该突出什么理念？ | doc_02 | doc_02 doc_16 doc_11 doc_18 doc_06 | doc_02 doc_16 doc_20 doc_15 doc_11 | doc_02 doc_15 doc_20 doc_06 doc_18 |
| q_03 | 我在云上租的那台小机器是什么配置？一个月开销多少？装的什么系统？ | doc_03 | doc_03 doc_17 doc_11 doc_15 doc_19 | doc_03 doc_17 doc_14 doc_11 doc_05 | doc_03 doc_17 doc_05 doc_04 doc_11 |
| q_04 | 我当时换向量数据库的核心理由是什么？换之前坚持先做什么对比？ | doc_04 | doc_04 doc_16 doc_03 doc_19 doc_11 | doc_04 doc_16 doc_11 doc_02 doc_03 | doc_04 doc_02 doc_11 doc_06 doc_19 |
| q_05 | 我写代码是用图形界面里的开发工具，还是纯键盘流的终端？ | doc_05 | doc_05 doc_14 doc_20 doc_02 doc_06 | doc_05 doc_14 doc_02 doc_20 doc_01 | doc_05 doc_20 doc_14 doc_01 doc_02 |
| q_06 | 项目发版之前有哪些强制检查？去年那次密钥泄露事故是怎么发生的？ | doc_06 doc_10 | doc_06 doc_10 doc_19 doc_11 doc_02 | doc_06 doc_10 doc_19 doc_02 doc_17 | doc_06 doc_10 doc_19 doc_17 doc_15 |
| q_07 | 下午开会前想喝点提神的，我平时喝什么豆子、什么口味舒服？ | doc_07 | doc_07 doc_13 doc_09 doc_20 doc_08 | doc_07 doc_09 doc_13 doc_20 doc_18 | doc_07 doc_20 doc_09 doc_18 doc_13 |
| q_08 | 我备战马拉松的训练安排是怎样的？膝盖有没有伤病影响？ | doc_08 | doc_08 doc_13 doc_20 doc_17 doc_06 | doc_08 doc_13 doc_06 doc_19 doc_20 | doc_08 doc_09 doc_20 doc_13 doc_11 |
| q_09 | 明天早上八点给我安排个会合不合适？我几点睡几点起？ | doc_09 | doc_09 doc_20 doc_13 doc_07 doc_18 | doc_09 doc_20 doc_07 doc_13 doc_19 | doc_09 doc_20 doc_07 doc_18 doc_13 |
| q_10 | 我的开源项目用的什么协议？别人拿去商用我介意吗？ | doc_10 | doc_02 doc_10 doc_05 doc_06 doc_15 | doc_10 doc_02 doc_15 doc_06 doc_14 | doc_10 doc_02 doc_15 doc_06 doc_03 |
| q_11 | 我的聊天记录会被存到别人的服务器上吗？数据默认放在哪里？ | doc_11 | doc_11 doc_17 doc_03 doc_15 doc_16 | doc_11 doc_17 doc_03 doc_02 doc_19 | doc_11 doc_02 doc_17 doc_04 doc_19 |
| q_12 | 国庆出行是坐飞机还是自己开车？具体路线怎么走？ | doc_12 | doc_12 doc_09 doc_20 doc_11 doc_08 | doc_12 doc_09 doc_02 doc_11 doc_05 | doc_12 doc_09 doc_06 doc_20 doc_18 |
| q_13 | 我控制体重期间的饮食结构是什么？每周有没有破戒的一顿？ | doc_13 | doc_13 doc_07 doc_08 doc_20 doc_09 | doc_13 doc_08 doc_20 doc_19 doc_07 | doc_13 doc_08 doc_20 doc_12 doc_19 |
| q_14 | 我最近业余在捣鼓的那个技术方向，用什么语言写的？ | doc_14 | doc_16 doc_02 doc_14 doc_18 doc_20 | doc_16 doc_02 doc_18 doc_01 doc_11 | doc_02 doc_18 doc_05 doc_14 doc_01 |
| q_15 | 机器人的推送消息我一般在哪里收？域名和加速是哪个服务商？ | doc_15 | doc_15 doc_11 doc_20 doc_02 doc_17 | doc_15 doc_11 doc_18 doc_20 doc_02 | doc_15 doc_19 doc_11 doc_02 doc_17 |
| q_16 | 我最近在读什么书？我读书有做笔记的习惯吗？ | doc_16 | doc_16 doc_11 doc_20 doc_13 doc_07 | doc_16 doc_11 doc_01 doc_14 doc_08 | doc_16 doc_05 doc_20 doc_18 doc_01 |
| q_17 | 除了我自己的硬盘，我的数据还在哪里留了副本？ | doc_17 | doc_17 doc_11 doc_16 doc_03 doc_04 | doc_17 doc_11 doc_04 doc_16 doc_03 | doc_17 doc_04 doc_11 doc_03 doc_16 |
| q_18 | 别人给我发一条 59 秒的语音，我会是什么反应？ | doc_18 | doc_18 doc_20 doc_15 doc_19 doc_09 | doc_18 doc_06 doc_09 doc_15 doc_02 | doc_18 doc_20 doc_06 doc_05 doc_09 |
| q_19 | 上个月网站半夜挂过一次，还记得根因吗？后来做了哪些加固？ | doc_19 | doc_19 doc_11 doc_06 doc_20 doc_17 | doc_19 doc_04 doc_03 doc_17 doc_16 | doc_19 doc_03 doc_17 doc_04 doc_06 |
| q_20 | 我一天里哪些时间段固定用来做什么？ | doc_20 | doc_20 doc_09 doc_16 doc_13 doc_08 | doc_09 doc_20 doc_07 doc_14 doc_08 | doc_07 doc_09 doc_20 doc_14 doc_13 |
| q_21 | 我是不是一个挺在意数据安全的人？ | doc_11 | doc_11 doc_03 doc_06 doc_17 doc_16 | doc_03 doc_11 doc_10 doc_17 doc_16 | doc_11 doc_10 doc_15 doc_16 doc_03 |
| q_22 | 我适合哪种工作节奏？早晨状态好还是晚上状态好？ | doc_09 doc_20 | doc_20 doc_09 doc_13 doc_18 doc_16 | doc_20 doc_09 doc_13 doc_08 doc_07 | doc_09 doc_20 doc_05 doc_18 doc_08 |
| q_23 | 我训练日的蛋白质补充和减脂期饮食是怎么配合的？ | doc_08 doc_13 | doc_13 doc_08 doc_20 doc_07 doc_09 | doc_13 doc_08 doc_07 doc_20 doc_06 | doc_13 doc_08 doc_12 doc_18 doc_16 |
| q_24 | 为了跑图形学我专门做了什么硬件上的准备？ | doc_14 | doc_14 doc_16 doc_17 doc_06 doc_19 | doc_14 doc_08 doc_19 doc_06 doc_02 | doc_14 doc_02 doc_06 doc_04 doc_20 |
| q_25 | 我挑软件和工具时最看重什么？ | doc_05 doc_11 | doc_02 doc_16 doc_01 doc_11 doc_05 | doc_01 doc_16 doc_18 doc_11 doc_05 | doc_01 doc_18 doc_16 doc_02 doc_05 |
| q_26 | 万一服务器彻底挂了，我有什么办法把数据找回来？ | doc_03 doc_17 | doc_11 doc_17 doc_04 doc_03 doc_19 | doc_11 doc_03 doc_04 doc_17 doc_19 | doc_03 doc_17 doc_19 doc_04 doc_11 |
| q_27 | 我每个月在云端基础设施上大概烧多少钱？都买了哪些服务？ | doc_03 doc_15 | doc_03 doc_11 doc_19 doc_17 doc_14 | doc_11 doc_19 doc_03 doc_14 doc_17 | doc_03 doc_11 doc_04 doc_02 doc_19 |
| q_28 | 接下来三个月我有什么想达成的技术目标？ | doc_14 | doc_14 doc_20 doc_16 doc_02 doc_13 | doc_14 doc_16 doc_02 doc_20 doc_13 | doc_14 doc_02 doc_20 doc_03 doc_08 |
| q_29 | 我喝咖啡的时间会不会影响我睡觉？ | doc_07 doc_09 | doc_07 doc_09 doc_20 doc_13 doc_08 | doc_07 doc_09 doc_20 doc_13 doc_08 | doc_07 doc_09 doc_13 doc_20 doc_17 |
| q_30 | 我是不是那种凡事都提前规划好的人？ | doc_20 | doc_20 doc_09 doc_16 doc_08 doc_07 | doc_08 doc_09 doc_13 doc_06 doc_12 | doc_09 doc_20 doc_18 doc_16 doc_06 |

> 说明: dense-only 与 hybrid 数字来自同一次 30-query 运行；rerank 不可用时该列记 N/A。
> 数据集扩展方式见 `evals/README.md`；Phase 5 将把本报告接入 CI 与 Agent 任务集、guardrail 评估并列。
