"""Memory task eval runner (Phase 5D) — offline skeleton scoring by default.

Run (from the repo ``src/`` directory):

    python -m evals.agent_task_runner               # offline（默认）
    python -m evals.agent_task_runner --live        # 真实管线（需 workspace + LLM）

Offline mode（检索侧离线评分）
------------------------------
Reads ``evals.dataset_memory_tasks.MEMORY_TASKS`` (10 tasks × 3 turns:
陈述事实 → 干扰 → 追问).  Without an LLM we cannot score the *answer*, so
the offline score measures the retrieval-side skeleton: could the memory
layer even *find* the facts the probe turn needs?

Per ``ExpectedHit``:
  * doc_exists           — hit.doc_id resolves in the retrieval corpus      0/1
  * fact_alignment       — character-bigram coverage of ``hit.fact`` inside
                            that doc's text (0..1; paraphrase-tolerant)     0..1
  * probe_retrievability — does the probe turn retrieve the doc?
                            BM25 (ant.provider.memory.bm25_index, zero
                            deps) when importable, else bigram-overlap      0/1

score(hit) = 0.4*doc_exists + 0.4*fact_alignment + 0.2*probe_retrievability
task score  = mean over expected_hits.  Skeleton by design: a perfect
offline score means the ground truth is *consistent and retrievable* — the
LLM-side answer quality is measured by ``--live``.

Output: ``evals/report_agent_tasks.md`` (timestamped header); repeatable.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from evals import dataset_retrieval as dr
from evals.dataset_memory_tasks import MEMORY_TASKS, MemoryTask

REPORT_PATH = Path(__file__).resolve().parent / "report_agent_tasks.md"

# 离线骨架得分的权重（解释见模块 docstring）
W_DOC_EXISTS = 0.4
W_ALIGNMENT = 0.4
W_PROBE = 0.2
PROBE_TOP_N = 3


def _bigrams(text: str) -> set[str]:
    """Character bigrams over the whitespace-stripped, lowercased text."""
    norm = re.sub(r"\s+", "", text).lower()
    return {norm[i : i + 2] for i in range(len(norm) - 1)}


def fact_alignment(fact: str, doc_text: str) -> float:
    """Fraction of the fact's character bigrams found in the doc text.

    Paraphrase-tolerant lexical alignment: a fact written in different
    words still shares key bigrams (人名/术语/数字), so 0.3–0.7 is a healthy
    skeleton score; 1.0 means the doc text literally contains the fact.
    """
    fb = _bigrams(fact)
    if not fb:
        return 1.0
    db = _bigrams(doc_text)
    return len(fb & db) / len(fb)


def _bigram_rank(probe: str, docs: list[dr.RetrievalDoc]) -> list[str]:
    """Rank corpus docs by shared bigram count with the probe (fallback)."""
    pb = _bigrams(probe)
    scored = sorted(
        ((doc.doc_id, len(pb & _bigrams(doc.text))) for doc in docs),
        key=lambda kv: kv[1],
        reverse=True,
    )
    return [doc_id for doc_id, _ in scored]


def _bm25_rank(probe: str, docs: list[dr.RetrievalDoc], top_n: int) -> list[str]:
    """Rank via the project's zero-dependency BM25 index when importable."""
    try:
        from ant.provider.memory.bm25_index import BM25Index
    except ImportError:
        return _bigram_rank(probe, docs)[:top_n]
    with tempfile.TemporaryDirectory(prefix="eval_agent_bm25_") as tmp:
        index = BM25Index(Path(tmp) / "bm25.json")
        for doc in docs:
            index.add(doc.doc_id, doc.text)
        hits = index.search(probe, top_n)
        return [doc_id for doc_id, _ in hits]


@dataclass(frozen=True)
class OfflineHitScore:
    """One expected hit's offline skeleton score."""

    doc_id: str
    fact: str
    doc_exists: bool
    alignment: float
    probe_retrieved: bool
    score: float


@dataclass(frozen=True)
class OfflineTaskScore:
    """One task's offline skeleton score."""

    task_id: str
    title: str
    difficulty: str
    hit_scores: list[OfflineHitScore]
    task_score: float


def score_task_offline(
    task: MemoryTask,
    corpus_docs: list[dr.RetrievalDoc],
    probe_top_n: int = PROBE_TOP_N,
) -> OfflineTaskScore:
    """Score one task's expected hits against the retrieval corpus."""
    doc_text = {d.doc_id: d.text for d in corpus_docs}
    probe = task.turns[2].text
    retrieved = _bm25_rank(probe, corpus_docs, probe_top_n)

    hits: list[OfflineHitScore] = []
    for hit in task.expected_hits:
        text = doc_text.get(hit.doc_id)
        if text is None:
            hits.append(
                OfflineHitScore(hit.doc_id, hit.fact, False, 0.0, False, 0.0)
            )
            continue
        alignment = fact_alignment(hit.fact, text)
        retrieved_flag = hit.doc_id in retrieved
        score = (
            W_DOC_EXISTS * 1.0 + W_ALIGNMENT * alignment + W_PROBE * float(retrieved_flag)
        )
        hits.append(
            OfflineHitScore(hit.doc_id, hit.fact, True, alignment, retrieved_flag, score)
        )

    task_score = sum(h.score for h in hits) / len(hits) if hits else 0.0
    return OfflineTaskScore(task.task_id, task.title, task.difficulty, hits, task_score)


def score_all_offline(
    tasks: list[MemoryTask] | None = None,
    corpus_docs: list[dr.RetrievalDoc] | None = None,
) -> list[OfflineTaskScore]:
    """Offline-score every task; defaults to the real dataset/corpus."""
    tasks = tasks or MEMORY_TASKS
    corpus_docs = corpus_docs or dr.RETRIEVAL_DOCS
    return [score_task_offline(t, corpus_docs) for t in tasks]


def _write_offline_report(results: list[OfflineTaskScore], report_path: Path) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [
        "# Agent Memory Task Eval Report — open-ant (Phase 5D)",
        "",
        f"- 生成时间: {now}",
        f"- 模式: offline（检索侧骨架评分，不调 LLM）",
        f"- 任务数: {len(results)}（`evals/dataset_memory_tasks.py`）",
        f"- 语料: {len(dr.RETRIEVAL_DOCS)} 篇（`evals/dataset_retrieval.py`）",
        "- 评分口径: score(hit) = 0.4*doc_exists + 0.4*fact_bigram_alignment "
        "+ 0.2*probe_retrievability(BM25 top-3)；task = 各 hit 均值",
        "",
        "## 汇总",
        "",
        "| task | difficulty | 命中数 | task 得分 |",
        "|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.task_id} | {r.difficulty} | {len(r.hit_scores)} | {r.task_score:.3f} |"
        )
    lines += ["", "## 逐任务明细", ""]
    for r in results:
        lines.append(f"### {r.task_id} — {r.title}（{r.difficulty}）")
        lines.append("")
        lines.append("| expected_hit (doc) | doc_exists | 事实对齐度 | 追问可检索 | 得分 |")
        lines.append("|---|---|---|---|---|")
        for h in r.hit_scores:
            lines.append(
                f"| {h.fact[:28]}… (`{h.doc_id}`) | {h.doc_exists} | "
                f"{h.alignment:.2f} | {h.probe_retrieved} | {h.score:.3f} |"
            )
        lines.append("")
    lines += [
        "> 解读: 离线得分衡量 ground truth 与语料的『一致性 + 可检索性』，"
        "不是回答质量。满分的含义是：记忆层只要把该 doc 注入上下文，第 3 轮"
        "追问就能答对。回答侧质量用 --live 模式（真实管线 + LLM）评估。",
        "",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已写入: {report_path}")


def _run_live(workspace: Path | None, agent_id: str | None) -> int:
    """Drive the real pipeline per task; exit 5 with a clear error when unavailable."""
    try:
        from ant.core.agent import Agent
        from ant.core.context import SharedContext
        from ant.core.events import CliEventSource
        from ant.utils.config import Config
    except ImportError as exc:
        print(f"ERROR: 无法导入 ant 运行时（{exc.__class__.__name__}: {exc}）。")
        print("live 模式需要：在 repo src/ 下运行、依赖已安装（pip install -e .）。")
        return 5

    ws = (workspace or Path.cwd() / "workspace").resolve()
    try:
        config = Config.load(ws)
        context = SharedContext(config)
    except Exception as exc:  # noqa: BLE001 — friendly CLI error
        print(f"ERROR: 无法从 {ws} 构建运行时（{exc.__class__.__name__}: {exc}）。")
        print("live 模式需要完整 workspace：config.user.yaml（llm 段完整、api_key "
              "有效）+ 可访问网络；与 `open-ant chat --workspace <ws>` 的要求一致。")
        print("离线评估请用默认 offline 模式（python -m evals.agent_task_runner）。")
        return 5

    try:
        agent_def = context.agent_loader.load(agent_id or config.default_agent)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: 加载 agent 失败（{exc.__class__.__name__}: {exc}）。")
        return 5

    async def _score_all() -> list[tuple[str, float]]:
        agent = Agent(agent_def, context)
        results: list[tuple[str, float]] = []
        for task in MEMORY_TASKS:
            session = await agent.new_session(CliEventSource())
            answers: list[str] = []
            for turn in task.turns:
                chunks: list[str] = []
                async for event in session.harness_stream_chat(turn.text):
                    if event.get("type") == "token":
                        chunks.append(event.get("data", ""))
                answers.append("".join(chunks))
            probe_answer = answers[-1]
            mem_ctx = getattr(session.state, "memory_context", "") or ""
            combined = probe_answer + " " + mem_ctx
            coverages = [
                fact_alignment(hit.fact, combined) for hit in task.expected_hits
            ]
            results.append((task.task_id, sum(coverages) / len(coverages)))
        return results

    import asyncio

    print("live 模式：逐任务驱动真实会话（需要网络与有效 LLM 凭据）…")
    try:
        results = asyncio.run(_score_all())
    except Exception as exc:  # noqa: BLE001 — friendly CLI error
        print(f"ERROR: live 运行失败（{exc.__class__.__name__}: {exc}）。")
        print("常见原因：api_key 无效 / 网络不可达 / 模型名错误。")
        return 5
    for task_id, coverage in results:
        print(f"  {task_id}: 事实覆盖 {coverage:.2f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="open-ant 记忆任务评测（Phase 5D）：默认离线骨架评分，"
        "--live 接真实管线。输出 evals/report_agent_tasks.md。"
    )
    parser.add_argument("--live", action="store_true",
                        help="接真实管线跑 10 个任务（需 workspace + LLM + 网络），"
                             "不可用时清晰报错并 exit 5")
    parser.add_argument("--workspace", default=None, help="--live 时读取的 workspace 目录")
    parser.add_argument("--agent", default=None, help="--live 时使用的 agent id（默认 config.default_agent）")
    parser.add_argument("--report", default=str(REPORT_PATH), help="报告输出路径")
    args = parser.parse_args(argv)

    if args.live:
        return _run_live(
            Path(args.workspace) if args.workspace else None, args.agent
        )

    results = score_all_offline()
    for r in results:
        print(f"  {r.task_id:15s} {r.title:12s} 得分 {r.task_score:.3f}")
    _write_offline_report(results, Path(args.report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
