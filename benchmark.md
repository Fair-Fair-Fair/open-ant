Claudecode、codex等Agent有编码基准测试，有个打分，看谁家智能体强；大模型也是，GPT和Claude、GROK也会有基准测试比较模型性能。那么上面的评测、或者你的agent也用了检索有没有检索性能的基准测试、你使用了记忆仲裁有没有记忆混乱的基准测试、你使用了偏好提取作为长期记忆、上下文压缩有没有他们的基准测试？检索一下告诉我

对，你这次问到的其实是 **Agent 工程里更关键的一层：不能只评估“整个 Agent 答得好不好”，而要分别评估 Agent 内部每个可插拔组件有没有把自己的事情做好。**

我检索了一圈，结论很明确：

> **有，而且已经形成了相当清楚的 benchmark 体系。**
>
> 但是不像 SWE-bench 那样，所有东西集中成一个统一排行榜。**不同模块通常有自己的 benchmark；真正做 Agent Evaluation 时，再把这些模块 benchmark + 端到端 Agent benchmark 组合起来。**

你提到的四个东西，我分别给你梳理。

---

# 1. 先建立一个正确的 Agent Evaluation 分层

假设你的 Agent：

```text
                    Agent
                      │
        ┌─────────────┼─────────────┐
        ↓             ↓             ↓
     Retrieval      Memory        Context
        ↓             ↓             ↓
      RAG         preference     compression
        │         extraction
        └─────────────┼─────────────┘
                      ↓
                LLM / Planner
                      ↓
             Tool / SubAgent
                      ↓
                Final Answer
```

那么至少可以分成：

```text
Level 1：Component Evaluation
    ├── Retrieval
    ├── Memory
    ├── Preference Extraction
    └── Context Compression

Level 2：Agent Behavior Evaluation
    ├── Tool use
    ├── Planning
    ├── Multi-agent coordination
    └── Trajectory

Level 3：End-to-End Evaluation
    └── Task Success / Quality / Cost
```

**你这次问的主要是 Level 1。**

---

# 2. Retrieval：这个不仅有 benchmark，而且已经非常成熟

这个是四个里面最成熟的。

## 经典检索 Benchmark

### BEIR

BEIR 是信息检索领域非常经典的 benchmark suite，用不同领域的数据测试：

```text
Query → Retriever → Ranked Documents
```

通常看：

```text
NDCG@k
Recall@k
Precision@k
MAP
```

所以如果你自己做：

```text
Embedding
    ↓
Vector DB
    ↓
Top-K
    ↓
Reranker
```

完全可以单独 benchmark：

```text
Recall@5
Recall@10
NDCG@10
MRR
```

这就是：

> **“我的 Retriever 到底能不能把正确文档捞出来？”**

---

## MTEB

现在更大的一个体系是 **MTEB（Massive Text Embedding Benchmark）**。

它不只是 embedding similarity，还包含 Retrieval、Reranking 等任务。目前官方任务列表里已经有大量 retrieval 任务。([docs.mteb.org][1])

所以如果你换 embedding model：

```text
BGE
E5
Qwen embedding
OpenAI embedding
```

可以直接在标准 retrieval benchmark 上比较。

---

## Agent / Deep Research 场景还有更针对性的检索 benchmark

这个特别值得你记。

MTEB 现在已经收录：

> **BrowseComp-Plus Retrieval**

它专门把 **Deep Research Agent 的 Retriever 单独拿出来评估**。

特点是：

* 830 个高难度 multi-hop query
* 大约 100K web documents
* human-verified evidence labels
* hard negatives

指标包括：

```text
nDCG@10
```

也就是说，已经有人在专门问：

> **“不是整个 Deep Research Agent 好不好，而是它的检索模块到底好不好？”** ([docs.mteb.org][1])

这与你的问题几乎完全对应。

---

# 3. RAG 又多一层：不仅看 Recall，还看“找来的东西有没有用”

假设：

```text
Query
 ↓
Retriever
 ↓
10 chunks
 ↓
LLM
 ↓
Answer
```

仅仅：

```text
Recall@10 = 90%
```

并不代表最终答案好。

因为：

> 找到了正确 chunk ≠ LLM 正确利用了 chunk。

因此 RAG evaluation 又有一套指标。

例如 Ragas 就有：

### Context Precision

检查：

> **相关 chunk 有没有排在前面？**

核心是看排名质量。([GitHub][2])

### Context Recall

检查：

> **真正有用的信息有没有被检索回来？**

本质是减少 false negative。([GitHub][3])

---

再往后还有：

```text
Context Relevance
Answer Faithfulness
Answer Relevance
```

ARES 就专门做这个。

ARES 将 RAG 拆成：

```text
Context Relevance
Answer Faithfulness
Answer Relevance
```

并使用自动 evaluator 去评估 RAG 系统。([ACL Anthology][4])

所以 RAG 的 evaluation 大概是：

```text
                  RAG Evaluation
                       │
           ┌───────────┼───────────┐
           ↓           ↓           ↓
        Retrieval    Context      Answer
        quality      quality       quality
           │           │           │
      Recall@K       Precision   Faithfulness
      NDCG@K         Recall      Relevance
```

---

# 4. Memory：有，而且这块现在发展得非常快

这个和你最相关。

你问：

> **“我使用了记忆仲裁，有没有‘记忆混乱’的 benchmark？”**

答案是：

**有，而且已经不止一个。**

---

# 5. LongMemEval：我认为你最应该重点研究

这个是目前非常值得你放进面试回答的 benchmark。

**LongMemEval** 是 ICLR 2025 的 benchmark，专门测试聊天助手的长期记忆。

它设计了 500 个问题，覆盖：

```text
Information Extraction
Multi-Session Reasoning
Knowledge Updates
Temporal Reasoning
Abstention
```

也就是：

### 能不能记住？

### 跨 session 能不能推理？

### 新信息来了会不会更新旧记忆？

### 时间关系对不对？

### 没记住的时候能不能正确承认不知道？

这最后一个 **Abstention** 对你说的“记忆仲裁”尤其重要。

因为一个差的 memory system 不是只有：

> “忘掉东西”

还有一种很危险：

> **把错误的东西当成记忆。**

LongMemEval 就专门把这种长期交互记忆拿出来系统评估。([GitHub][5])

---

# 6. 更关键：LongMemEval 甚至可以拆 Retriever 和 Memory

LongMemEval 不只是看最终 QA。

它的框架实际上可以拆：

```text
History
 ↓
Indexing
 ↓
Retrieval
 ↓
Reading
 ↓
Answer
```

所以你可以分别研究：

```text
Memory Recall
↓
Downstream QA
```

论文还研究了：

* session decomposition
* fact-augmented key expansion
* time-aware query expansion

来改善 memory recall 和最终 QA。([arXiv][6])

所以你的“记忆仲裁”可以非常自然地放进去。

---

# 7. LoCoMo：另一个非常重要的长期记忆 benchmark

**LoCoMo** 专门评估 long-term conversational memory。

它的数据非常长，而且覆盖：

```text
single-hop
multi-hop
temporal
open-domain
adversarial
```

官方数据包含 10 个长对话，并带 QA / event summarization 标注。([GitHub][7])

这非常适合测：

> “我的 Memory 是否能在很长的历史中找到真正需要的信息？”

---

# 8. MemBench：更接近你说的“记忆能力本身”

还有一个 **MemBench**，专门针对：

> **LLM-based Agents 的 memory capability**

它把 memory 分成：

```text
Factual Memory
Reflective Memory
```

同时考虑：

```text
Participation
Observation
```

并从多个维度评估：

```text
Effectiveness
Efficiency
Capacity
```

这是 ACL 2025 Findings 的工作。([ACL Anthology][8])

这个比“最终回答准确率”更进一步，因为它开始问：

> **你的 memory mechanism 本身是否高效？容量扩张以后是否还能工作？**

---

# 9. 你说的“记忆混乱”，现在已经有专门指标了

这个我觉得是你问题里最有价值的一点。

Memory 不只是：

```text
Recall
```

还包括：

```text
Conflict
Update
Temporal consistency
Abstention
Contradiction resolution
```

尤其现在 Mem0 的公开 memory benchmark suite 已经把这些维度明确列出来。

他们现在公开的 benchmark suite 包括：

```text
LOCOMO
LongMemEval
BEAM
```

其中 **BEAM** 更进一步，把 memory 能力拆成：

```text
preference following
instruction following
information extraction
multi-session reasoning
knowledge update
summarization
temporal reasoning
event ordering
abstention
contradiction resolution
```

这已经非常接近你说的：

> **“记忆仲裁会不会把多个记忆搞混？”**

尤其：

> **Contradiction Resolution**

就是一个非常直接的答案。([GitHub][9])

而且 BEAM 还在：

```text
1M tokens
10M tokens
```

规模上测试 memory，这非常贴近真实 Agent 的长期运行场景。([Mem0][10])

---

# 10. Preference Extraction：你问的这个也有专门 benchmark

这个很有意思。

你说：

> **“我把用户偏好提取出来，作为长期记忆。”**

不要把它只当成普通 memory。

它可以拆成两个问题：

```text
Conversation
    ↓
Preference Extraction
    ↓
Structured Memory
    ↓
Preference Retrieval
    ↓
Preference Following
```

这已经有人专门 benchmark。

---

# 11. PrefEval

**PrefEval** 是一个专门测试：

> **LLM 能不能识别、记住并遵守用户偏好**

的 benchmark。

它包含：

```text
Explicit preference
Implicit choice-based preference
Implicit persona-based preference
```

大约：

```text
3000 preference-query pairs
20 topics
```

而且测试：

```text
10 turns
300 turns
100K token
```

等长上下文。([arXiv][11])

最有意思的是它明确发现：

> 很多模型在很短的对话里，preference following 就会明显下降。

而 RAG / Reminder 可以改善效果。([PrefEval][12])

---

# 12. 2026 年又出现更接近真实长期记忆的 benchmark

这个更值得你知道。

### AlpsBench

2026 年出现的 **AlpsBench** 专门针对：

> **LLM personalization + long-term interaction**

它来自真实人类-LLM 对话，并把生命周期拆成：

```text
Personalized Information Extraction
        ↓
Updating
        ↓
Retrieval
        ↓
Utilization
```

也就是：

```text
“你记没记对”
“你更新对没有”
“你取对没有”
“你最终用对没有”
```

这跟你说的：

> **“偏好提取作为长期记忆”**

几乎是正面对上的。

而且论文明确发现：

* latent user traits 很难提取
* memory update 存在性能瓶颈
* 大量 distractor 会让 retrieval accuracy 明显下降
* 有显式 memory 并不自动等于 preference-aligned response

([arXiv][13])

---

# 13. 还有一个更“狠”的：PerMemBench

2026 年还有：

> **PerMemBench**

论文研究的是：

> **个性化 Memory Policy**

不是单纯问：

> “你能不能记住？”

而是问：

> **“这个东西到底该不该存？”**

这非常接近你说的：

> **记忆仲裁**

因为 Agent 每次都可能面临：

```text
用户说了一句话
      ↓
要不要存？
      ↓
存成什么？
      ↓
和旧记忆冲突怎么办？
      ↓
覆盖还是追加？
```

PerMemBench 就是专门研究 personalized memory policies，并测试 long-horizon、多年度、多领域交互历史。([arXiv][14])

所以你说的“记忆仲裁”已经可以拆成一个非常标准的问题：

```text
Memory Governance
├── Should Store?
├── What to Store?
├── What to Update?
├── What to Delete?
├── Conflict Resolution
└── What to Retrieve?
```

这个方向已经有 benchmark 在研究。

---

# 14. 上下文压缩：也有 benchmark，但目前不像 Retrieval/Memory 那么“统一”

这个地方需要精确一点。

**Context Compression 确实有大量 benchmark 和实验，但暂时不像 BEIR/MTEB 那么统一。**

常见思路是：

```text
Original Context
       ↓
Compression
       ↓
Compressed Context
       ↓
LLM
       ↓
Task Score
```

真正应该衡量的不是：

> “压缩率是多少？”

而是：

> **“我删了 70% token，任务能力掉了多少？”**

所以可以看：

```text
Compression Ratio
Token Reduction
Task Accuracy
Latency
Cost
Information Retention
```

---

# 15. LongLLMLingua 就是非常典型的代表

LongLLMLingua 专门研究：

> **Prompt / Context Compression**

它在多个 long-context 场景测试压缩后的 downstream performance。

例如论文报告：

* NaturalQuestions 上最多提升 21.4%
* token 数约减少 4 倍
* LooGLE 上成本下降 94%
* 10K token prompt 做 2–6× 压缩时，端到端 latency 可提高 1.4–2.6×。([ACL Anthology][15])

注意这里特别重要：

> **它并不是拿“压缩文本像不像原文本”作为唯一标准。**

而是：

> **压缩之后，下游任务还能不能做好。**

这就是你自己的 ContextGuard 应该采用的基本思想。

---

# 16. RULER / NoLiMa 可以评估“压缩后丢没丢关键上下文”

还有两个 benchmark 很值得你用：

## RULER

RULER 不只是普通 Needle-in-a-Haystack。

它加入：

```text
multi-needle
multi-hop tracing
aggregation
```

并可以控制：

```text
context length
task complexity
```

所以它可以检测：

> Context 越长以后，模型还能不能从中找出正确的信息。([arXiv][16])

---

## NoLiMa

这个更厉害。

传统 NIAH 很容易被模型利用：

> 相同词 → 找相同词。

NoLiMa 故意降低 query 和 needle 的 lexical overlap，要求模型根据**隐式关系**找到真正的信息。

结果发现很多长上下文模型在 32K 以后性能大幅下降。([arXiv][17])

因此你可以用：

```text
Original Context
vs
Compressed Context
```

分别跑：

```text
RULER
NoLiMa
```

比较：

```text
ΔAccuracy
```

来衡量：

> **我的压缩器到底损失了多少“真实可用信息”。**

---

# 17. 甚至已经出现了专门的 Context Compaction Benchmark

这个特别新。

我搜到了一个叫：

> **CompactBench**

的开源 benchmark。

它的定义非常直接：

> **你的 Agent 在长对话里做 context compaction 后，原来重要的信息到底还剩多少？**

它甚至专门测试：

```text
Multi-cycle drift
Hidden ranked set
```

也就是说：

```text
历史
 ↓
Compact
 ↓
继续对话
 ↓
再次 Compact
 ↓
继续对话
 ↓
再次 Compact
 ↓
...
```

测试**多轮压缩以后信息会不会逐渐漂移**。

这跟你自己做的：

```text
truncate
 ↓
estimate
 ↓
LLM summary
```

简直是高度相关。([GitHub][18])

---

# 18. 所以你现在可以把自己 Agent 的 Benchmark 做成这样

假设你自己的 Runtime 有：

```text
                    Agent
                      │
       ┌──────────────┼──────────────┐
       ↓              ↓              ↓
    Retrieval       Memory        Context
       │              │              │
      RAG        preference      compression
                    │
                    ↓
                 Planner
                    ↓
                Tool / Agent
                    ↓
                 Answer
```

你的测试矩阵可以直接这么设计：

| 模块                    | 评估问题           | Benchmark / 指标                  |
| --------------------- | -------------- | ------------------------------- |
| Retrieval             | 能不能找到正确文档      | BEIR / MTEB / Recall@K / NDCG   |
| RAG                   | 找到的内容是否有用      | RAGAS / ARES                    |
| Long-term Memory      | 能不能记住长期信息      | LongMemEval / LoCoMo            |
| Memory Conflict       | 新旧记忆冲突怎么办      | BEAM / contradiction resolution |
| Preference Extraction | 能否提取用户偏好       | PrefEval / AlpsBench            |
| Preference Following  | 提取后是否真的用上      | PrefEval                        |
| Memory Policy         | 该不该存、更新什么      | PerMemBench                     |
| Context Compression   | 压缩后能力掉多少       | LongLLMLingua 类 downstream eval |
| Long Context          | 长上下文还能否找到关键内容  | RULER / NoLiMa                  |
| Context Compaction    | 多次压缩是否发生 drift | CompactBench                    |

---

# 19. 最关键的一点：不要把这些 Benchmark 和“Agent Benchmark”混在一起

这是你面试时最容易说错的地方。

比如 SWE-bench：

```text
Agent
 ↓
理解 issue
 ↓
修改代码
 ↓
运行测试
 ↓
提交 patch
```

它测的是：

> **End-to-End Agent Capability**

但 BEIR：

```text
Query
 ↓
Retriever
 ↓
Documents
```

它测的是：

> **Retrieval Capability**

LongMemEval：

```text
Long history
 ↓
Memory system
 ↓
Answer
```

主要测：

> **Long-term Memory Capability**

CompactBench：

```text
Conversation
 ↓
Compaction
 ↓
Conversation continues
```

测：

> **Context Compaction Capability**

所以：

```text
           Agent Benchmark
                │
       ┌────────┼────────┐
       ↓        ↓        ↓
   End-to-End Components Runtime
                │
     ┌──────────┼──────────┐
     ↓          ↓          ↓
 Retrieval    Memory    Compression
```

**成熟的 Agent 系统一般是 component benchmark + end-to-end benchmark 双层评估。**

---

# 20. 这对你自己的 Agent 项目特别有价值

你之前做的 ContextGuard：

```text
truncate
   ↓
estimate
   ↓
LLM summary
```

现在你完全可以问一个科研/工程上都很漂亮的问题：

> **“ContextGuard 相比不压缩，Token 减少多少，同时在 LongMemEval / RULER / 自建 Agent Task 上造成多少性能损失？”**

例如最终画：

```text
Compression Ratio
        ↑
  4x    │       ●
  3x    │    ●
  2x    │ ●
  1x    │
        └────────────────→
           Task Accuracy
```

或者：

```text
Method         Token ↓      Task Success
-----------------------------------------
No compression    1x            94%
Truncate          4x            71%
Summary            4x            89%
Hybrid            4x            93%
```

这就从：

> **“我写了一个 ContextGuard 防止 token 爆炸。”**

升级成：

> **“我有一个可验证的 context management strategy，并通过 benchmark 衡量 compression-quality trade-off。”**

---

# 21. 而你的“记忆仲裁”尤其值得做 benchmark

假设你的 Memory Pipeline：

```text
Conversation
      ↓
Preference Extraction
      ↓
Memory Candidate
      ↓
Memory Arbitration
      ↓
ADD / UPDATE / DELETE / IGNORE
      ↓
Long-term Memory
```

那么完全可以拆成四个 benchmark：

```text
① Extraction Accuracy
   有没有提取正确？

② Storage Decision Accuracy
   这个东西该不该存？

③ Conflict Resolution Accuracy
   新旧冲突应该保留谁？

④ Retrieval / Utilization Accuracy
   最后该用的时候能不能拿对？
```

**AlpsBench / LongMemEval / BEAM / PrefEval 已经分别覆盖了其中的大部分问题。** ([arXiv][13])

---

## 最后，我给你一个面试里非常好用的回答

面试官如果问：

> **“你怎么评估 Agent 的 Memory、Retrieval、Context Compression？”**

你可以说：

> **“我会做分层评估，而不是只看最终 Agent 成功率。Retrieval 层可以用 BEIR、MTEB 以及针对 Deep Research Retriever 的 BrowseComp-Plus，指标主要是 Recall@K、NDCG@K；如果是 RAG，还会进一步评估 Context Precision、Context Recall、Faithfulness。Memory 层可以用 LongMemEval、LoCoMo、MemBench 和 BEAM，分别评估长期记忆、跨 session reasoning、knowledge update、temporal reasoning、abstention 以及 contradiction resolution。我的偏好提取属于 personalization memory，可以用 PrefEval、AlpsBench 评估 preference extraction、retrieval 和最终 preference following。Context Compression 则不能只看 compression ratio，而应该看压缩以后 downstream task 的性能保持率，可以参考 LongLLMLingua、RULER、NoLiMa，针对 Agent 的连续 compaction 还可以看 CompactBench。最后再把这些 component-level metrics 和 SWE-bench 这类 end-to-end task success 结合起来。”**

这套回答的关键不是你背了多少 benchmark，而是你已经建立了：

> **“每个 Agent Component 都应该有自己的 Evaluation Contract。”**

这是非常成熟的 Agent Runtime 思路。

而且以你现在这个项目来看，**最值得你马上补的不是更多 Agent Framework，而是自己做一份 `Agent Evaluation Matrix`：Retrieval / Memory Arbitration / Preference Extraction / ContextGuard / SubAgent Routing / Tool Calling 各自的 benchmark、指标、数据集和 ablation。**这会让你面试时从“我实现了 Agent”变成“我知道如何证明我的 Agent 为什么好”。

[1]: https://docs.mteb.org/overview/available_tasks/retrieval/?utm_source=chatgpt.com "Retrieval - Massive Text Embedding Benchmark"
[2]: https://github.com/vibrantlabsai/ragas/blob/main/docs/concepts/metrics/available_metrics/context_precision.md?utm_source=chatgpt.com "ragas/docs/concepts/metrics/available_metrics/context_precision.md at main · vibrantlabsai/ragas · GitHub"
[3]: https://github.com/vibrantlabsai/ragas/blob/main/docs/concepts/metrics/available_metrics/context_recall.md?utm_source=chatgpt.com "ragas/docs/concepts/metrics/available_metrics/context_recall.md at main · vibrantlabsai/ragas · GitHub"
[4]: https://aclanthology.org/2024.naacl-long.20/?utm_source=chatgpt.com "ARES: An Automated Evaluation Framework for Retrieval-Augmented Generation Systems - ACL Anthology"
[5]: https://github.com/xiaowu0162/longmemeval?utm_source=chatgpt.com "GitHub - xiaowu0162/LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory (ICLR 2025) · GitHub"
[6]: https://arxiv.org/abs/2410.10813?utm_source=chatgpt.com "LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory"
[7]: https://github.com/snap-research/locomo?utm_source=chatgpt.com "GitHub - snap-research/locomo · GitHub"
[8]: https://aclanthology.org/2025.findings-acl.989/?utm_source=chatgpt.com "MemBench: Towards More Comprehensive Evaluation on the Memory of LLM-based Agents - ACL Anthology"
[9]: https://github.com/mem0ai/memory-benchmarks?utm_source=chatgpt.com "GitHub - mem0ai/memory-benchmarks: Open-source evaluation suite for memory-augmented LLM systems · GitHub"
[10]: https://docs.mem0.ai/core-concepts/memory-evaluation?utm_source=chatgpt.com "Memory Evaluation - Mem0"
[11]: https://arxiv.org/abs/2502.09597?utm_source=chatgpt.com "Do LLMs Recognize Your Preferences? Evaluating Personalized Preference Following in LLMs"
[12]: https://prefeval.github.io/?utm_source=chatgpt.com "PrefEval: Do LLMs Recognize Your Preferences? Evaluating Personalized Preference Following in LLMs"
[13]: https://arxiv.org/abs/2603.26680?utm_source=chatgpt.com "AlpsBench: An LLM Personalization Benchmark for Real-Dialogue Memorization and Preference Alignment"
[14]: https://arxiv.org/abs/2605.25535?utm_source=chatgpt.com "Personalize-then-Store: Benchmarking and Learning Personalized Memory for Long-horizon Agents"
[15]: https://aclanthology.org/2024.acl-long.91/?utm_source=chatgpt.com "LongLLMLingua: Accelerating and Enhancing LLMs in Long Context Scenarios via Prompt Compression - ACL Anthology"
[16]: https://arxiv.org/abs/2404.06654?utm_source=chatgpt.com "RULER: What's the Real Context Size of Your Long-Context Language Models?"
[17]: https://arxiv.org/abs/2502.05167?utm_source=chatgpt.com "NoLiMa: Long-Context Evaluation Beyond Literal Matching"
[18]: https://github.com/compactbench/compactbench?utm_source=chatgpt.com "GitHub - compactbench/compactbench: Open benchmark for LLM context compaction methods — measures what survives when you replace conversation history with a compacted artifact. Multi-cycle drift, hidden ranked set. · GitHub"
