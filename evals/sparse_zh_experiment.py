"""Phase 5E sparse-zh experiment — jieba vs fastembed sparse for Chinese.

Standalone CLI (mirrors evals/run_retrieval_eval.py conventions):

    python -m evals.sparse_zh_experiment

Background: the Phase 3D eval found hybrid retrieval *underperforming*
pure dense on the 20-doc Chinese corpus (hybrid RRF recall@5 0.9167 <
dense-only 0.9833) — the fastembed BM25 sparse model is English-centric.
This experiment swaps the sparse generator to jieba word segmentation
(fixed 1M hashed index space) and re-runs the same 30 queries through the
REAL QdrantStore path — one dedicated collection per generator, both
recreated per run:

    open_ant_sparse_exp_fastembed  — sparse_model=fastembed (baseline)
    open_ant_sparse_exp_jieba      — sparse_model=jieba     (candidate)

Scores: recall@5 / MRR / NDCG@10 (evals.metrics), doc-level dedup.
Report: evals/report_sparse_zh.md.  Credentials from .env (QDRANT_URL /
QDRANT_API_KEY); missing → clear error, exit 3.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from ant.provider.memory.qdrant_store import QdrantStore, QdrantStoreError
from ant.utils.settings import InfraSettings

try:
    from evals.dataset_retrieval import RETRIEVAL_DOCS, RETRIEVAL_QUERIES
    from evals.metrics import mrr, ndcg_at_k, recall_at_k
    from evals.run_retrieval_eval import _chunk_docs, _make_embedder
except ImportError:  # running as a bare script from src/
    from dataset_retrieval import RETRIEVAL_DOCS, RETRIEVAL_QUERIES
    from metrics import mrr, ndcg_at_k, recall_at_k
    from run_retrieval_eval import _chunk_docs, _make_embedder

TOP_K = 5
MODEL_NAME = "BAAI/bge-small-zh-v1.5"
COLLECTIONS = {
    "fastembed": "open_ant_sparse_exp_fastembed",
    "jieba": "open_ant_sparse_exp_jieba",
}
REPORT_PATH = Path(__file__).resolve().parent / "report_sparse_zh.md"
# Phase 3D 基线（同一语料/查询，run_retrieval_eval 实测）
BASELINE_DENSE_RECALL_5 = 0.9833
BASELINE_FASTEMBED_HYBRID_RECALL_5 = 0.9167


class _SyncEmbedToProvider:
    """Adapt a sync ``embed_fn`` to the store's EmbeddingProvider protocol."""

    def __init__(self, embed_fn: Callable[[list[str]], list[list[float]]]):
        self._embed_fn = embed_fn

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return self._embed_fn(texts)


def _reset_collection(settings: InfraSettings, name: str) -> None:
    """Recreate the collection so every run starts from a clean state."""
    from qdrant_client import QdrantClient

    client = QdrantClient(
        url=settings.qdrant_url(),
        api_key=settings.qdrant_api_key(),
        timeout=settings.qdrant_timeout,
    )
    try:
        client.delete_collection(name)
    except Exception:  # noqa: BLE001 — collection may not exist yet
        pass


def _doc_ids(docs: list[Any]) -> list[str]:
    """Chunk-level hits → doc-level ids, first occurrence wins."""
    seen: set[str] = set()
    out: list[str] = []
    for doc in docs:
        doc_id = doc.metadata.get("doc_id") or doc.id.split("#")[0]
        if doc_id not in seen:
            seen.add(doc_id)
            out.append(doc_id)
    return out


def _aggregate(hits: list[list[str]]) -> tuple[float, float, float]:
    pairs = list(zip(hits, RETRIEVAL_QUERIES))
    n = len(pairs)
    r5 = sum(recall_at_k(r, q.ground_truth, TOP_K) for r, q in pairs) / n
    m = sum(mrr(r, q.ground_truth) for r, q in pairs) / n
    nd = sum(ndcg_at_k(r, q.ground_truth, k=10) for r, q in pairs) / n
    return r5, m, nd


async def _run_scenario(
    sparse_model: str,
    collection: str,
    settings: InfraSettings,
    embed_fn: Callable[[list[str]], list[list[float]]],
    chunks: list[tuple[str, str, dict]],
) -> tuple[list[list[str]], list[list[str]]]:
    """One generator: build the collection via the REAL QdrantStore, run 30 queries."""
    settings.qdrant_collection = collection
    store = QdrantStore(
        config=SimpleNamespace(memory=SimpleNamespace(sparse_model=sparse_model)),
        embedding_provider=_SyncEmbedToProvider(embed_fn),
        settings=settings,
    )
    chunk_ids = [c[0] for c in chunks]
    chunk_texts = [c[1] for c in chunks]
    chunk_payloads = [c[2] for c in chunks]
    await store.add(documents=chunk_texts, metadatas=chunk_payloads, ids=chunk_ids)
    print(f"[{sparse_model}] 已入库 {len(chunks)} chunks → {collection}，跑查询…")

    dense_hits: list[list[str]] = []
    hybrid_hits: list[list[str]] = []
    for q in RETRIEVAL_QUERIES:
        dense = await store.query(q.query, top_k=TOP_K, prefer_hybrid=False)
        hybrid = await store.query(q.query, top_k=TOP_K, prefer_hybrid=True)
        dense_hits.append(_doc_ids(dense))
        hybrid_hits.append(_doc_ids(hybrid))
    return dense_hits, hybrid_hits


def _write_report(
    rows: list[dict[str, Any]],
    embed_label: str,
    dim: int,
    embed_fallback: bool,
    jieba_version: str,
    n_chunks: int,
) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [
        "# Sparse-zh Experiment Report — open-ant (Phase 5E)",
        "",
        f"- 生成时间: {now}",
        f"- 集合: `{COLLECTIONS['fastembed']}` / `{COLLECTIONS['jieba']}`（每次运行重建）",
        f"- 语料: {len(RETRIEVAL_DOCS)} 篇 / {n_chunks} chunks（`evals/dataset_retrieval.py`）",
        f"- 查询: {len(RETRIEVAL_QUERIES)} 条标注 query（ground truth doc 级）",
        f"- Dense embedding: {embed_label}（dim={dim}）",
        f"- Sparse: fastembed Qdrant/bm25 vs jieba lcut（版本 {jieba_version}）",
        f"- 指标口径: recall@{TOP_K} / MRR / NDCG@10，doc 级去重（`evals/metrics.py`）",
        f"- 降级状态: {'⚠️ hash 伪向量，数字仅作管线冒烟' if embed_fallback else '真实 embedding'}",
        f"- Phase 3D 基线（run_retrieval_eval）: dense recall@5={BASELINE_DENSE_RECALL_5} / "
        f"hybrid(RRF) recall@5={BASELINE_FASTEMBED_HYBRID_RECALL_5}",
        "",
        "## 汇总对照",
        "",
        "| sparse_model | 模式 | recall@5 | MRR | NDCG@10 |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['model']} | {row['mode']} | {row['recall_5']:.4f} | "
            f"{row['mrr']:.4f} | {row['ndcg_10']:.4f} |"
        )

    dense_row = next(r for r in rows if r["mode"] == "dense-only")
    f_hyb = next(r for r in rows if r["model"] == "fastembed" and r["mode"] != "dense-only")
    j_hyb = next(r for r in rows if r["model"] == "jieba" and r["mode"] != "dense-only")
    if j_hyb["recall_5"] >= f_hyb["recall_5"] and j_hyb["recall_5"] >= dense_row["recall_5"] - 1e-9:
        verdict = "jieba 中文分词 sparse 反超/追平 dense——hybrid 不再拖后腿，建议切换 sparse_model=jieba（并重建集合）。"
    elif j_hyb["recall_5"] > f_hyb["recall_5"]:
        verdict = "jieba 明显优于 fastembed 但仍未追平 dense；hybrid 提升有限，可评估加大 sparse 权重。"
    else:
        verdict = "jieba 未跑赢 fastembed——需复查索引空间/分词质量（见 _sparse_vectors docstring 的重建注意事项）。"

    lines += [
        "",
        "## 结论",
        "",
        f"- fastembed hybrid vs jieba hybrid（recall@5）: {f_hyb['recall_5']:.4f} → "
        f"{j_hyb['recall_5']:.4f}（Δ {j_hyb['recall_5'] - f_hyb['recall_5']:+.4f}）",
        f"- jieba hybrid vs dense-only: recall@5 Δ {j_hyb['recall_5'] - dense_row['recall_5']:+.4f}，"
        f"MRR Δ {j_hyb['mrr'] - dense_row['mrr']:+.4f}，NDCG@10 Δ {j_hyb['ndcg_10'] - dense_row['ndcg_10']:+.4f}",
        f"- 判定: {verdict}",
        "",
        "> 说明: 两集合 dense-only 数字一致属预期（同一 dense 向量）；切换 sparse_model 后必须",
        "> 重建集合（delete_by_filter 或 recreate），fastembed 与 jieba 的索引空间不兼容。",
        "> jieba 模式不加载 fastembed ONNX 模型（省内存），且索引空间固定为 1M 的 sha256 哈希。",
        "",
    ]
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已写入: {REPORT_PATH}")


def main() -> int:
    settings = InfraSettings()
    url, api_key = settings.qdrant_url(), settings.qdrant_api_key()
    if not url or not api_key:
        print(
            "Qdrant 凭据缺失：请在 .env 设置 QDRANT_URL / QDRANT_API_KEY "
            f"(url={'set' if url else 'missing'}, api_key={'set' if api_key else 'missing'})"
        )
        return 3

    chunks = _chunk_docs()
    embed_fn, embed_label, embed_fallback = _make_embedder("auto", MODEL_NAME)
    dim = len(embed_fn(["测试"])[0])
    # 评测集合按实际 embedding 维度建（独立于生产 QDRANT_VECTOR_SIZE）
    settings.qdrant_vector_size = dim
    try:
        import jieba

        jieba_version = jieba.__version__
    except Exception:  # noqa: BLE001 — version string is cosmetic
        jieba_version = "unknown"

    rows: list[dict[str, Any]] = []
    for sparse_model in ("fastembed", "jieba"):
        if sparse_model == "jieba":
            try:
                import jieba  # noqa: F401
            except ImportError:
                print("jieba 未安装：pip install -e src（或 pip install jieba）后重试。")
                return 3
        collection = COLLECTIONS[sparse_model]
        _reset_collection(settings, collection)
        try:
            dense_hits, hybrid_hits = asyncio.run(
                _run_scenario(sparse_model, collection, settings, embed_fn, chunks)
            )
        except QdrantStoreError as exc:
            print(f"[{sparse_model}] Qdrant 不可用: {exc}")
            return 3
        d_r5, d_m, d_n = _aggregate(dense_hits)
        h_r5, h_m, h_n = _aggregate(hybrid_hits)
        rows.append(
            {"model": sparse_model, "mode": "dense-only",
             "recall_5": d_r5, "mrr": d_m, "ndcg_10": d_n}
        )
        rows.append(
            {"model": sparse_model, "mode": "hybrid (RRF)",
             "recall_5": h_r5, "mrr": h_m, "ndcg_10": h_n}
        )
        print(
            f"[{sparse_model}] dense-only : recall@{TOP_K}={d_r5:.4f}  "
            f"MRR={d_m:.4f}  NDCG@10={d_n:.4f}"
        )
        print(
            f"[{sparse_model}] hybrid(RRF): recall@{TOP_K}={h_r5:.4f}  "
            f"MRR={h_m:.4f}  NDCG@10={h_n:.4f}"
        )

    _write_report(rows, embed_label, dim, embed_fallback, jieba_version, len(chunks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
