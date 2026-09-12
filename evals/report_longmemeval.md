# LongMemEval 评测报告（Open-Ant / OpenAnt-MemoryArk）

> 日期：2026-09-12（协议 v2 最终版）· 数据集：LongMemEval-S（ICLR 2025，MIT，500 题 × 6 题型）
> 模型：deepseek-v4-flash · **协议 v2：answerer / 提取 / judge 全链非思考**
> （对齐官方 gpt-4o 协议形态：官方 judge 非思考 + max_tokens=10，官方 answerer 非思考）
> judge：官方 prompt 逐字移植（MIT）+ 退化回答确定性短路
> 口径：**子集行 = 从同一次全量 500 运行中按 seed=42 子集（n=100）过滤**——
> 子集与全量同一次生成、同一个 judge，直接可比；memory 两档仅 n=100。

---

## 1. 结论摘要

**消融阶梯（协议 v2，同协议同 judge）：**

| 模式 | 子集 n=100 | 全量 500 | 归因 |
|---|---|---|---|
| baseline（无记忆） | 4.0%（4/100） | 6.0%（30/500） | 地板 ≈ abstention 得分 |
| **memory（user-only，生产口径）** | **50.0%（50/100）** | — | 生产记忆管线：只取用户消息提取 |
| memory（user+assistant，对照） | 53.0%（53/100） | — | 放开提取口径仅 +3pp |
| chunks（原始文本检索） | 67.0%（67/100） | 64.0%（320/500） | 检索层上限：压缩必然有损 |
| oracle（evidence 注入） | 72.0%（72/100） | 72.6%（363/500） | 数据无损时模型的上限 |

**三个核心发现：**

1. **思考模式是记忆提取环节的系统性负优化。** thinking 时代生产口径只有 4–5%，协议 v2（非思考提取）直接到 **50.0%（10×）**。机制实测：reasoning tokens 吃 max_tokens 预算 → 提取截断/螺旋/空输出（thinking 时代 oracle 空回答 125/500 = 25%）。官方协议本来就不用思考模型——**对齐官方协议本身就是修复**。
2. **生产口径的隐私保护几乎免费。** user-only vs user+assistant 差距仅 **3pp**——只取用户消息、拒绝助手内容入记忆的生产策略，几乎不损失 QA 能力。
3. **压缩记忆在 knowledge-update 类反超原始文本：80.0% vs 66.7%。** 结构化记忆把"旧值→新值"并存保留，原始文本块里新旧信息互相淹没——记忆管线的净收益。

**子集 vs 全量一致性**：oracle 0.6pp、chunks 3pp、baseline 2pp——同协议下无异常缺口（历史记录：thinking 时代 oracle 全量 66.4% 与 v2 子集 77.0% 的 10.6pp 差距，构成是 thinking answerer 25% 空回答 + 运行间波动，见 §6.4）。

## 2. 分题型（子集 n=100，从全量运行过滤，%）

| 题型 | baseline | oracle | chunks | mem(user) | mem(u+asst) |
|---|---|---|---|---|---|
| single-session-user | 7.1 | 92.9 | 92.9 | 78.6 | 78.6 |
| single-session-assistant | 0.0 | 100.0 | 90.9 | 9.1 | 9.1 |
| multi-session | 7.4 | 66.7 | 44.4 | 33.3 | 37.0 |
| temporal-reasoning | 0.0 | 48.1 | 66.7 | 51.9 | 55.6 |
| knowledge-update | 6.7 | 100.0 | 66.7 | **80.0** | **80.0** |
| single-session-preference | 0.0 | 33.3 | 66.7 | 50.0 | 66.7 |

### 2.1 数字 = 分数对照（每个百分比都可核；零 LLM 复算：`workspace/evals/longmemeval/audit_numbers.py`）

小分母必然产生"整"数——分题型分母只有 27/15/14/11/6：

| 数字 | 分数 | 数字 | 分数 |
|---|---|---|---|
| 72.6%（oracle 全量） | 363/500 | 64.0%（chunks 全量） | 320/500 |
| 72.0%（oracle 子集） | 72/100 | 67.0%（chunks 子集） | 67/100 |
| 50.0%（mem user-only） | 50/100 | 53.0%（mem u+asst） | 53/100 |
| 80.0%（knowledge-update 记忆两档） | 12/15 | 66.7%（knowledge-update chunks） | 10/15 |
| 66.7%（多处） | 18/27、10/15、4/6 | 44.4%（multi-session chunks） | 12/27 |
| 37.0%（multi u+asst） | 10/27 | 33.3%（multi user-only） | 9/27 |
| 55.6%（temporal u+asst） | 15/27 | 51.9%（temporal user-only） | 14/27 |
| 48.1%（temporal oracle） | 13/27 | 100.0%（oracle 多类） | 15/15、11/11、14/14 |
| 92.9%（user 类） | 13/14 | 78.6%（user 记忆两档） | 11/14 |
| 90.9%（assistant-chunks） | 10/11 | 9.1%（assistant 记忆两档） | 1/11 |

## 3. judge 尺子法证（评测协议本身的三类缺陷，全部定位+修复+回归测试）

1. **旧 judge 假阴性**：思考模式 + max_tokens=256 偶发空 content → 恒判 no（实测 2%）。修复：非思考 + max_tokens=10（官方契约值）。
2. **新 judge 假阳性**：非思考 judge 对空 hypothesis（退化输入）误判 yes（oracle 22/500 实测）。修复：`empty_hypothesis_verdict()` 空回答确定性短路，不进 LLM。
3. **litellm 参数静默丢弃**：litellm 1.89.3 的 deepseek transformer 只转发 `thinking={"type":"enabled"}`，`disabled`/`reasoning_effort="none"` 都被丢弃——必须 `extra_body` 直传（实测 reasoning_tokens 41→0）。

完整证据链可复现：`workspace/evals/longmemeval/audit_judge_ruler.py`（第 1 节参数实测 / 第 2 节生产路径 / 第 3 节确定性审计+头对头+空回答法证）。回归测试：`src/ant/tests/test_longmemeval_eval.py`（judge kwargs、退化短路、wipe 按模式、提取幂等、非思考 overrides 等 10+ 个）。

## 4. 工程保障（本轮跑数期间修复的评测基建缺陷）

- **wipe 按模式**：隔离修复后 wipe 仍删旧共享集合名 → fresh 跑不清旧点、新旧记忆混存（16.6% 污染事故同类）。修复 + 回归测试。
- **提取幂等**：memory_id 是随机 UUID，中断续跑会留重复记忆 → 提取前 `delete_by_filter` 自清本实例旧点。
- **BM25 缓存恢复**：fastembed BM25 缓存被 Windows 清 Temp 干掉且 GCS 直连失败；BM25 本体只是停用词表，从 HF 拉 english.txt 进缓存目录即恢复（`fix_bm25_cache.py`）。实测 BM25 ~37k 条/s，jieba 仅 ~1k 条/s（GIL 串行）——评测不能默认 jieba（chunks 500 曾因此 6 小时 vs 2.5 小时）。
- **中断续跑**：逐实例落盘 + `--resume` + 确定性 chunk ID + 提取幂等。chunks 500 实际中断两次（134/500、0/500）均无损续完。

## 5. 成本

| 项 | 估算 |
|---|---|
| chunks 500（本地 embedding ~2.5h CPU + 500 非思考回答） | ~¥5 |
| baseline/oracle 各 500（纯 LLM，非思考） | ~¥4 |
| memory 两档 n=100（提取 ~16k 条记忆 + 回答） | ~¥15-20 |
| judge（约 2,800 次判定，非思考 max_tokens=10） | <¥3 |
| **合计（协议 v2 全量）** | **<¥35** |

## 6. 诚实边界清单（面试主动说）

1. **模型不同**：对齐的是官方**协议形态**（非思考 + 同款 judge 契约），模型是 deepseek-v4-flash ≠ 官方 gpt-4o。全量 500 口径：我们 chunks 64.0%；官方 gpt-4o 检索 57.7%（检索配置不同，仅量级对照）。
2. **子集披露**：memory 两档 n=100（分层抽样 seed=42）；baseline/oracle/chunks 的子集行从同一次全量 500 运行过滤，与全量行同生成同 judge。子集 vs 全量：oracle 0.6pp / chunks 3pp / baseline 2pp。
3. **自评偏差**：judge 与 answerer 同模型；官方同用 gpt-4o 作 judge，偏差方向与官方一致，有更强模型 key 时 `--judge-model` 可复评。
4. **运行间波动**：temperature=0 仍非逐位确定——同种子 n=100 oracle 单独跑曾得 77.0%，全量内嵌子集 72.0%（±5pp 量级）。报告以同一次生成的数字为准。
5. **负结果**：single-session-assistant 9.1%（助手侧证据提取召回残差）；temporal 记忆 < chunks（提取压缩丢时间先后，图时间边是 Phase 8 方向）；preference 类 oracle 仅 33–50%（模型天花板）。
6. baseline 4–6% ≈ abstention 得分——题目有效性的证据，不是失败。

## 7. 面试一句话

> "在 LongMemEval（ICLR 2025）上我做了五档消融：无记忆 4% → 生产口径记忆 50% → 原始文本检索 67% → oracle 72.6%。最有价值的不是数字，是归因：发现思考模式在提取环节是 10× 的负优化（对齐官方非思考协议即修复）、法证了评测 judge 自身的三类缺陷、证明 user-only 生产策略的隐私保护只花 3pp。每个 Δ 都能指到具体组件，每个百分比都能拆成 yes/总数。"
