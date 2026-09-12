# LongMemEval 评测报告（Open-Ant / OpenAnt-MemoryArk）

> 日期：2026-09-12（协议 v2 最终版）· 数据集：LongMemEval-S（ICLR 2025，MIT，500 题 × 6 题型）
> 模型：deepseek-v4-flash · **协议 v2：answerer / 提取 / judge 全链非思考**
> （对齐官方 gpt-4o 协议形态：官方 judge 非思考 + max_tokens=10，官方 answerer 非思考）
> judge：官方 prompt 逐字移植（MIT）+ 退化回答确定性短路
> 口径：消融主表 = 同子集分层抽样（seed=42, n=100）；全量 500 参考行单独标注

---

## 1. 结论摘要

**同子集消融阶梯（n=100, seed=42，协议 v2）：**

| 模式 | 准确率 | 归因 |
|---|---|---|
| baseline（无记忆） | 4.0% | 地板 ≈ abstention 得分，证明题离开历史确实答不了 |
| **memory（user-only 提取，生产口径）** | **50.0%** | 生产记忆管线：只用用户消息提取（防助手知识污染） |
| memory（user+assistant 提取，对照） | 53.0% | 放开提取口径仅 +3pp |
| chunks（原始文本检索） | 67.0%（子集 n=100）/ **64.0%（全量 500）** | 检索层上限：压缩必然有损 |
| oracle（evidence 注入） | 77.0%（子集）/ 66.4%（全量 500，thinking 时代归档） | 数据无损时模型的上限 |

**三个核心发现：**

1. **思考模式是记忆提取环节的系统性负优化。** thinking 时代生产口径只有 4–5%，协议 v2（非思考提取）直接到 **50.0%（10×）**。机制实测：reasoning tokens 吃 max_tokens 预算 → 提取截断/螺旋/空输出（oracle 曾 125/500 空回答）。官方协议本来就不用思考模型——**对齐官方协议本身就是修复**。
2. **生产口径的隐私保护几乎免费。** user-only vs user+assistant 从 thinking 时代的 19pp 差距缩到 **3pp**——只取用户消息、拒绝助手内容入记忆的生产策略，几乎不损失 QA 能力。
3. **压缩记忆在 knowledge-update 类反超原始文本：80.0% vs 66.7%。** 结构化记忆把"旧值→新值"并存保留，原始文本块里新旧信息互相淹没——这是记忆管线的净收益，不是妥协。

---

## 2. 分题型（同子集 n=100, seed=42，%）

| 题型 | baseline | oracle | chunks | mem(user) | mem(u+asst) |
|---|---|---|---|---|---|
| single-session-user | 7.1 | 100.0 | 92.9 | 78.6 | 78.6 |
| single-session-assistant | 0.0 | 100.0 | 90.9 | 9.1 | 9.1 |
| multi-session | 7.4 | 77.8 | 44.4 | 33.3 | 37.0 |
| temporal-reasoning | 0.0 | 48.1 | 66.7 | 51.9 | 55.6 |
| knowledge-update | 6.7 | 100.0 | 66.7 | **80.0** | **80.0** |
| single-session-preference | 0.0 | 50.0 | 66.7 | 50.0 | 66.7 |

### 2.1 数字 = 分数对照（每个百分比都可核，零 LLM 复算：`workspace/evals/longmemeval/audit_numbers.py`）

小分母必然产生"整"数——分题型分母只有 27/15/14/11/6：

| 数字 | 分数 | 数字 | 分数 |
|---|---|---|---|
| 67.0%（chunks 子集） | 67/100 | 66.7%（temporal-chunks） | 18/27 |
| 64.0%（chunks 全量） | 320/500 | 55.6%（temporal u+asst） | 15/27 |
| 50.0%（mem user-only） | 50/100 | 51.9%（temporal user-only） | 14/27 |
| 53.0%（mem u+asst） | 53/100 | 48.1%（temporal oracle） | 13/27 |
| 80.0%（knowledge-update 记忆两档） | 12/15 | 44.4%（multi-session chunks） | 12/27 |
| 66.7%（knowledge-update chunks） | 10/15 | 37.0%（multi u+asst） | 10/27 |
| 100.0%（oracle 多类） | 15/15、11/11、14/14 | 33.3%（multi user-only） | 9/27 |
| 92.9%（user-chunks） | 13/14 | 78.6%（user 记忆两档） | 11/14 |
| 90.9%（assistant-chunks） | 10/11 | 9.1%（assistant 记忆两档） | 1/11 |
| 4.0%（baseline） | 4/100 | 7.1%（user-baseline） | 1/14 |

## 3. judge 尺子法证（评测协议本身的三类缺陷，全部定位+修复+回归测试）

1. **旧 judge 假阴性**：思考模式 + max_tokens=256 偶发空 content → 恒判 no（实测 2%）。修复：非思考 + max_tokens=10（官方契约值）。
2. **新 judge 假阳性**：非思考 judge 对空 hypothesis（退化输入）误判 yes（oracle 22/500 实测）。修复：`empty_hypothesis_verdict()` 空回答确定性短路，不进 LLM。
3. **litellm 参数静默丢弃**：litellm 1.89.3 的 deepseek transformer 只转发 `thinking={"type":"enabled"}`，`disabled`/`reasoning_effort="none"` 都被丢弃——必须 `extra_body` 直传（实测 reasoning_tokens 41→0）。

完整证据链可复现：`workspace/evals/longmemeval/audit_judge_ruler.py`（第 1 节参数实测 / 第 2 节生产路径 / 第 3 节确定性审计+头对头+空回答法证）。回归测试：`src/ant/tests/test_longmemeval_eval.py`（judge kwargs、退化短路、wipe 按模式、提取幂等、非思考 overrides 共 10+ 个）。

## 4. 工程保障（本轮跑数期间修复的评测基建缺陷）

- **wipe 按模式**：隔离修复后 wipe 仍删旧共享集合名 → fresh 跑不清旧点、新旧记忆混存（16.6% 污染事故同类）。修复 + 回归测试。
- **提取幂等**：memory_id 是随机 UUID，中断续跑会留重复记忆 → 提取前 `delete_by_filter` 自清本实例旧点。
- **BM25 缓存恢复**：fastembed BM25 缓存被 Windows 清 Temp 干掉且 GCS 直连失败；BM25 本体只是停用词表，从 HF 拉 english.txt 进缓存目录即恢复（`fix_bm25_cache.py`）。实测 BM25 ~37k 条/s，jieba 仅 ~1k 条/s（GIL 串行）——评测不能默认 jieba（chunks 500 曾因此 6 小时 vs 2.5 小时）。
- **中断续跑**：逐实例落盘 + `--resume` + 确定性 chunk ID + 提取幂等。chunks 500 实际中断两次（134/500、0/500）均无损续完。

## 5. 成本

| 项 | 估算 |
|---|---|
| chunks 500（本地 embedding ~2.5h CPU + 500 非思考回答） | ~¥5 |
| baseline/oracle 各 100 | <¥1 |
| memory 两档 n=100（提取 ~16k 条记忆 + 回答） | ~¥15-20 |
| judge（约 2,200 次判定，非思考 max_tokens=10） | <¥2 |
| **合计（协议 v2 全量）** | **<¥30** |

## 6. 诚实边界清单（面试主动说）

1. **模型不同**：对齐的是官方**协议形态**（非思考 + 同款 judge 契约），模型是 deepseek-v4-flash ≠ 官方 gpt-4o。全量 500 口径：我们 chunks 64.0%；官方 gpt-4o 检索 57.7%（检索配置不同，仅量级对照）。
2. **子集披露**：消融主表 n=100 seed=42；chunks 子集 67.0% vs 全量 64.0%（3pp，抽样波动方向一致），两者都报告。
3. **自评偏差**：judge 与 answerer 同模型；官方同用 gpt-4o 作 judge，偏差方向与官方一致，有更强模型 key 时 `--judge-model` 可复评。
4. **负结果**：single-session-assistant 9.1%（助手侧证据提取召回残差）；temporal 记忆 < chunks（提取压缩丢时间先后，图时间边是 Phase 8 方向）；preference 类 oracle 仅 50%（模型天花板）。
5. baseline 4% ≈ abstention 得分——题目有效性的证据，不是失败。

## 7. 面试一句话

> "在 LongMemEval（ICLR 2025）上我做了五档消融：无记忆 4% → 生产口径记忆 50% → 原始文本检索 67% → oracle 77%。我最有价值的产出不是数字，而是归因过程：发现思考模式在提取环节是 10× 的负优化（对齐官方非思考协议即修复）、发现评测 judge 自身的三类缺陷并做了确定性审计、证明 user-only 生产策略的隐私保护只花 3pp。每一个 Δ 都能指到具体组件。"
