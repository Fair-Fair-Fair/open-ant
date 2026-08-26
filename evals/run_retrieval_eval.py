"""Retrieval evaluation runner (Phase 3D) — standalone CLI, not an open-ant command.

Run (from the repo ``src/`` directory, which is the pytest/pythonpath root):

    python -m evals.run_retrieval_eval
    python -m evals.run_retrieval_eval --top-k 5 --collection my_eval_collection
    python -m evals.run_retrieval_eval --embedder hash --no-rerank

What it does
------------
1. Loads credentials from ``.env`` (through ``ant.utils.settings.InfraSettings``;
   Qdrant fields are read via a small subclass so no prod code is touched).
2. (Re)creates a dedicated Qdrant collection — never touches the production
   collection name in .env — so every run starts from a clean, identical state
   (repeatability requirement).
3. Chunks the 20-doc Chinese corpus, embeds it, upserts it, and answers the
   30 annotated queries with three pipelines:
       dense-only   — pure vector search
       hybrid (RRF) — vector + BM25 fused by reciprocal rank fusion (k=60,
                      same algorithm as ant.provider.memory.hybrid_store)
       + rerank     — hybrid output re-scored by a cross-encoder (N/A column
                      when the model is unavailable)
4. Scores each pipeline with recall@5 / MRR / NDCG@10 (evals.metrics) and
   writes the full comparison + per-query hit details to
   ``evals/report_retrieval.md`` (timestamped header).

Degradation paths (all loud, all graceful)
------------------------------------------
- ``qdrant_client`` missing            → clear install hint, exit code 3.
- Qdrant unreachable                   → clear connection error, exit code 3.
- Real embedding unavailable (no local
  sentence-transformers model / no API
  key)                                 → deterministic hash pseudo-vectors with a
                                        prominent WARNING; pipeline still runs
                                        end-to-end (numbers will be near chance).
- Cross-encoder rerank model missing   → rerank column shows N/A.

Phase 3A switch-over note
-------------------------
When ``ant/provider/memory/qdrant_store.py`` lands (QdrantStore implementing
the ``VectorStore`` interface), replace the direct ``qdrant_client`` calls in
``_QdrantBackend`` with the store — the interface expected here is tiny and
documented in the class docstring.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# 1. .env 加载：经由 InfraSettings（含 .env 搜索逻辑），子类补 Qdrant 字段。
#    pydantic-settings 的 env_file 由基类配置继承，字段自动映射 QDRANT_*。
# ---------------------------------------------------------------------------
from ant.provider.memory.bm25_index import BM25Index
from ant.utils.settings import InfraSettings

try:
    from evals.dataset_retrieval import RETRIEVAL_DOCS, RETRIEVAL_QUERIES
    from evals.metrics import mrr, ndcg_at_k, recall_at_k
except ImportError:  # running as a bare script from src/
    from dataset_retrieval import RETRIEVAL_DOCS, RETRIEVAL_QUERIES
    from metrics import mrr, ndcg_at_k, recall_at_k

RRF_K = 60  # same constant as ant.provider.memory.hybrid_store
OVERFETCH = 4
DEFAULT_COLLECTION = "open_ant_retrieval_eval"
REPORT_PATH = Path(__file__).resolve().parent / "report_retrieval.md"


class _EvalSettings(InfraSettings):
    """InfraSettings + Qdrant credentials (QDRANT_URL / QDRANT_API_KEY).

    Kept inside the eval package on purpose: extending the production
    settings class is out of scope for Phase 3D.
    """

    qdrant_url: str | None = None
    qdrant_api_key: str | None = None
    qdrant_timeout: int = 30


# ---------------------------------------------------------------------------
# 2. Qdrant backend — minimal compat path until Phase 3A QdrantStore merges.
# ---------------------------------------------------------------------------

def _point_id(chunk_id: str) -> int:
    """Deterministic Qdrant point id for a chunk id.

    Qdrant point ids must be unsigned ints (or UUIDs) — a bare string like
    ``doc_01#0`` is rejected.  Hash to a stable int so repeated runs
    upsert into the same points (idempotent by construction).
    """
    return int.from_bytes(hashlib.sha256(chunk_id.encode("utf-8")).digest()[:8], "big")


class _QdrantBackend:
    """Tiny Qdrant adapter used by the eval only.

    Interface expected by this script:
        ensure_collection(name, dim) -> None   (recreate → repeatable runs)
        upsert_vectors(collection, ids, vectors, payloads) -> None
        search(collection, vector, limit) -> list[(id, score)]
    ``ant.provider.memory.qdrant_store.QdrantStore`` will satisfy the same
    needs once Phase 3A lands; the switch is a drop-in at call sites below.
    """

    def __init__(self, url: str | None, api_key: str | None, timeout: int = 30) -> None:
        if url is None:
            raise RuntimeError(
                "QDRANT_URL 未配置：请在 .env 中设置 QDRANT_URL / QDRANT_API_KEY "
                "（或本地起一个 Qdrant 容器）。"
            )
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.http import models
        except ImportError as exc:  # pragma: no cover — env-dependent
            raise RuntimeError(
                "缺少 qdrant-client：pip install qdrant-client\n"
                "（Phase 3A 之后该依赖会进入项目正式依赖，届时可移除本条提示）"
            ) from exc
        self._models = models
        self._client = QdrantClient(url=url, api_key=api_key, timeout=timeout)

    def ensure_collection(self, name: str, dim: int) -> None:
        m = self._models
        try:
            self._client.delete_collection(name)
        except Exception:  # noqa: BLE001 — collection may not exist yet
            pass
        self._client.create_collection(
            collection_name=name,
            vectors_config=m.VectorParams(size=dim, distance=m.Distance.COSINE),
        )

    def upsert_vectors(
        self,
        collection: str,
        ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict],
    ) -> None:
        m = self._models
        self._client.upsert(
            collection_name=collection,
            points=[
                m.PointStruct(id=_point_id(pid), vector=vec, payload=payload)
                for pid, vec, payload in zip(ids, vectors, payloads)
            ],
        )

    def search(self, collection: str, vector: list[float], limit: int) -> list[tuple[str, float]]:
        # query_points is the modern API; older clients expose .search().
        query = getattr(self._client, "query_points", None)
        if query is not None:
            hits = query(
                collection_name=collection,
                query=vector,
                limit=limit,
                with_payload=True,
            ).points
        else:
            hits = self._client.search(
                collection_name=collection,
                query_vector=vector,
                limit=limit,
                with_payload=True,
            )
        return [(h.payload.get("doc_id", h.id), h.score) for h in hits]


# ---------------------------------------------------------------------------
# 3. Embedding — real when available, deterministic hash fallback otherwise.
# ---------------------------------------------------------------------------

class _HashEmbedder:
    """Deterministic pseudo-vectors — pipeline plumbing only, NO semantics.

    Same text always yields the same normalized vector (stable dim 384, the
    QDRANT_VECTOR_SIZE default) so the harness can run end-to-end before the
    Phase 3A embedding layer is ready.  Retrieval numbers under this backend
    are expected to be near chance — that is fine, it is a smoke test.
    """

    dim: int = 384

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs = []
        for text in texts:
            vec: list[float] = []
            for i in range(self.dim):
                digest = hashlib.sha256(f"{text}::{i}".encode("utf-8")).digest()
                vec.append(int.from_bytes(digest[:4], "big") / (2**32 - 1) * 2 - 1)
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vecs.append([v / norm for v in vec])
        return vecs


class _SentenceTransformersEmbedder:
    """Local bge model (EMBED_MODEL_NAME from .env, default bge-small-zh-v1.5).

    Loads with ``local_files_only=True``: if the model is not in the HF
    cache the eval degrades to the hash backend instead of hanging on a
    multi-GB download.
    """

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name, device="cpu", local_files_only=True)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._model.encode(texts, normalize_embeddings=True).tolist()


def _make_embedder(
    embedder: str, model_name: str
) -> tuple[Callable[[list[str]], list[list[float]]], str, bool]:
    """Return (embed_fn, label, was_fallback).  Never raises for the embedding step."""
    if embedder == "hash":
        return _HashEmbedder().embed, "hash (forced)", True
    try:
        st = _SentenceTransformersEmbedder(model_name)
        dim = st._model.get_sentence_embedding_dimension()
        return st.embed, f"sentence-transformers {model_name} (dim={dim})", False
    except Exception as exc:  # noqa: BLE001 — model missing / package missing
        print(f"WARNING: 真实 embedding 不可用（{exc.__class__.__name__}: {exc}），"
              f"降级为 hash 伪向量。待 Phase 3A embedding 层就绪后自动切回真 embedding。")
        return _HashEmbedder().embed, "hash (degraded)", True


# ---------------------------------------------------------------------------
# 4. Chunking — langchain-text-splitters when available, sentence split else.
# ---------------------------------------------------------------------------

def _chunk_docs() -> list[tuple[str, str, dict]]:
    """Chunk the 20 docs → [(chunk_id, text, payload)].  Chinese punctuation
    aware; chunk id deterministic: ``<doc_id>#<idx>``."""
    chunks: list[tuple[str, str, dict]] = []
    for doc in RETRIEVAL_DOCS:
        parts: list[str] = []
        try:
            from langchain_text_splitters import RecursiveCharacterTextSplitter

            splitter = RecursiveCharacterTextSplitter(
                chunk_size=300,
                chunk_overlap=30,
                separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],
            )
            parts = splitter.split_text(doc.text)
        except ImportError:
            # 简单按句切分（中文标点），每段 ≤ 200 字
            sentence = ""
            for char in doc.text:
                sentence += char
                if char in "。！？" and len(sentence) >= 80:
                    parts.append(sentence)
                    sentence = ""
            if sentence:
                parts.append(sentence)
        for idx, part in enumerate(parts):
            chunk_id = f"{doc.doc_id}#{idx}"
            chunks.append(
                (
                    chunk_id,
                    part,
                    {
                        "chunk_id": chunk_id,
                        "doc_id": doc.doc_id,
                        "chunk_index": idx,
                        "keywords": doc.keywords,
                    },
                )
            )
    return chunks


# ---------------------------------------------------------------------------
# 5. Retrieval pipelines
# ---------------------------------------------------------------------------

def _rrf_fuse(
    dense: list[tuple[str, float]], bm25: list[tuple[str, float]]
) -> list[str]:
    """Reciprocal Rank Fusion (k=60) → doc ids best-first.  Same algorithm
    as ant.provider.memory.hybrid_store._rrf_fuse."""
    ranks: dict[str, float] = {}
    for rank, (doc_id, _) in enumerate(dense, start=1):
        ranks[doc_id] = ranks.get(doc_id, 0.0) + 1.0 / (RRF_K + rank)
    for rank, (doc_id, _) in enumerate(bm25, start=1):
        ranks[doc_id] = ranks.get(doc_id, 0.0) + 1.0 / (RRF_K + rank)
    return [doc_id for doc_id, _ in sorted(ranks.items(), key=lambda kv: kv[1], reverse=True)]


def _dedupe_to_docs(ranked_chunk_hits: list[tuple[str, float]]) -> list[str]:
    """Chunk-level hits → doc-level ids, keeping each doc's best rank."""
    seen: set[str] = set()
    docs: list[str] = []
    for chunk_id, _score in ranked_chunk_hits:
        doc_id = chunk_id.split("#")[0]
        if doc_id not in seen:
            seen.add(doc_id)
            docs.append(doc_id)
    return docs


class _Reranker:
    """Cross-encoder reranker; None when the model is unavailable (Phase 3C)."""

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(model_name, device="cpu", max_length=512)

    def score(self, query: str, candidates: list[str]) -> list[float]:
        return [float(s) for s in self._model.predict([(query, c) for c in candidates])]


def _build_reranker(model_name: str) -> _Reranker | None:
    try:
        return _Reranker(model_name)
    except Exception as exc:  # noqa: BLE001 — model missing / package missing
        print(f"WARNING: cross-encoder rerank 不可用（{exc.__class__.__name__}: {exc}），"
              f"rerank 列将显示 N/A。")
        return None


# ---------------------------------------------------------------------------
# 6. Evaluation flow
# ---------------------------------------------------------------------------

@dataclass
class _MethodScores:
    method: str
    recall_5: float
    mrr_score: float
    ndcg_10: float
    per_query: list[tuple[str, str, str, str]]  # q_id, dense_top5, hybrid_top5, rerank_top5


def _evaluate(
    backend: _QdrantBackend,
    collection: str,
    embed_fn: Callable[[list[str]], list[list[float]]],
    bm25: BM25Index | None,
    reranker: _Reranker | None,
    top_k: int,
) -> list[_MethodScores]:
    """Run all 30 queries through the three pipelines, collect scores + details."""
    # ── per-query ranked doc lists (chunk-level → doc-level) ──
    dense_results: list[list[str]] = []
    hybrid_results: list[list[str]] = []
    rerank_results: list[list[str] | None] = []
    for q in RETRIEVAL_QUERIES:
        [q_vec] = embed_fn([q.query])
        # 注意：backend.search 的 payload 带 doc_id，返回即 doc 级 id
        dense = backend.search(collection, q_vec, top_k * OVERFETCH)
        dense_docs = _dedupe_to_docs(dense)[:top_k]
        dense_results.append(dense_docs)

        # BM25 侧索引的是 chunk id，融合前统一映射回 doc id（每个 doc 保留最优名次）
        bm25_hits: list[tuple[str, float]] = (
            bm25.search(q.query, top_k * OVERFETCH) if bm25 is not None else []
        )
        seen_bm25: set[str] = set()
        bm25_doc_hits: list[tuple[str, float]] = []
        for chunk_id, score in bm25_hits:
            doc_id = chunk_id.split("#")[0]
            if doc_id not in seen_bm25:
                seen_bm25.add(doc_id)
                bm25_doc_hits.append((doc_id, score))
        hybrid_docs = _rrf_fuse(dense, bm25_doc_hits)[:top_k]
        hybrid_results.append(hybrid_docs)

        if reranker is not None:
            try:
                # 重排必须用 doc 级候选 + 真实正文打分——旧实现把 doc_id 字符串
                # 当作文本喂给 cross-encoder，分数无意义（recall 崩到 0.17）。
                doc_texts = {d.doc_id: d.text for d in RETRIEVAL_DOCS}
                pool = _dedupe_to_docs(dense)[: top_k * 2]
                texts = [doc_texts[c] for c in pool if c in doc_texts]
                pool = [c for c in pool if c in doc_texts]
                scores = reranker.score(q.query, texts)
                reranked = [
                    cid
                    for cid, _ in sorted(
                        zip(pool, scores), key=lambda p: p[1], reverse=True
                    )
                ]
                rerank_results.append(reranked[:top_k])
            except Exception:  # noqa: BLE001 — rerank failure degrades to N/A
                rerank_results.append(None)
        else:
            rerank_results.append(None)

    # ── aggregate per method ──
    def aggregate(results: list[list[str]]) -> _MethodScores:
        queries = RETRIEVAL_QUERIES
        r5 = sum(
            recall_at_k(r, q.ground_truth, top_k) for r, q in zip(results, queries)
        ) / len(queries)
        m = sum(mrr(r, q.ground_truth) for r, q in zip(results, queries)) / len(queries)
        n = sum(ndcg_at_k(r, q.ground_truth, k=10) for r, q in zip(results, queries))
        n /= len(queries)
        return _MethodScores("", r5, m, n, [])

    dense_scores = aggregate(dense_results)
    hybrid_scores = aggregate(hybrid_results)
    rerank_avail = any(r is not None for r in rerank_results)
    if rerank_avail:
        rerank_scores = aggregate([r for r in rerank_results if r is not None])
    else:
        rerank_scores = None

    detail = [
        (q.query_id, q.query, " ".join(dense_results[i]), " ".join(hybrid_results[i]),
         " ".join(rerank_results[i]) if rerank_results[i] is not None else "N/A")
        for i, q in enumerate(RETRIEVAL_QUERIES)
    ]
    methods = [
        _MethodScores(
            "dense-only", dense_scores.recall_5, dense_scores.mrr_score,
            dense_scores.ndcg_10, detail,
        ),
        _MethodScores(
            "hybrid (RRF)", hybrid_scores.recall_5, hybrid_scores.mrr_score,
            hybrid_scores.ndcg_10, detail,
        ),
    ]
    if rerank_scores is not None:
        methods.append(
            _MethodScores(
                "hybrid + rerank", rerank_scores.recall_5,
                rerank_scores.mrr_score, rerank_scores.ndcg_10, detail,
            )
        )
    else:
        methods.append(
            _MethodScores("hybrid + rerank", float("nan"), float("nan"), float("nan"), detail)
        )
    return methods


# ---------------------------------------------------------------------------
# 7. Report
# ---------------------------------------------------------------------------

def _write_report(
    collection: str,
    embed_label: str,
    dim: int,
    embed_fallback: bool,
    rerank_available: bool,
    n_chunks: int,
    methods: list[_MethodScores],
) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [
        "# Retrieval Eval Report — open-ant (Phase 3D)",
        "",
        f"- 生成时间: {now}",
        f"- 集合: `{collection}`（每次运行重建，可复现）",
        f"- 语料: 20 篇 / {n_chunks} chunks（`evals/dataset_retrieval.py`）",
        f"- 查询: {len(RETRIEVAL_QUERIES)} 条标注 query（ground truth doc 级）",
        f"- 向量维度: {dim}",
        f"- Embedding: {embed_label}",
        f"- 降级状态: {'⚠️ 使用 hash 伪向量，数字仅供管线冒烟，无语义' if embed_fallback else '真实 embedding，数字有语义意义'}",
        f"- Rerank (Phase 3C): {'可用' if rerank_available else 'N/A（模型不可用）'}",
        f"- 指标口径: recall@5 / MRR / NDCG@10，doc 级去重后计算",
        "",
        "## 汇总对照",
        "",
        "| 方法 | recall@5 | MRR | NDCG@10 |",
        "|---|---|---|---|",
    ]
    for m in methods:
        if math.isnan(m.recall_5):
            lines.append(f"| {m.method} | N/A | N/A | N/A |")
        else:
            lines.append(
                f"| {m.method} | {m.recall_5:.4f} | {m.mrr_score:.4f} | {m.ndcg_10:.4f} |"
            )
    lines += [
        "",
        "## 逐查询明细（top-5 命中，doc id）",
        "",
        "| query_id | query | gt | dense-only | hybrid(RRF) | +rerank |",
        "|---|---|---|---|---|---|",
    ]
    for q_id, query, dense, hybrid, rerank in methods[0].per_query:
        gt = " ".join(next(q.ground_truth for q in RETRIEVAL_QUERIES if q.query_id == q_id))
        lines.append(f"| {q_id} | {query} | {gt} | {dense} | {hybrid} | {rerank} |")
    lines += [
        "",
        "> 说明: dense-only 与 hybrid 数字来自同一次 30-query 运行；rerank 不可用时该列记 N/A。",
        "> 数据集扩展方式见 `evals/README.md`；Phase 5 将把本报告接入 CI 与 Agent 任务集、guardrail 评估并列。",
        "",
    ]
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已写入: {REPORT_PATH}")


def main() -> int:
    global REPORT_PATH  # 必须在任何对 REPORT_PATH 的引用之前声明
    parser = argparse.ArgumentParser(
        description="open-ant 检索评测（Phase 3D）。读取 .env 连接 Qdrant，"
                    "跑 20 篇语料 × 30 条查询，输出对照报告到 evals/report_retrieval.md。"
    )
    parser.add_argument(
        "--collection",
        default=DEFAULT_COLLECTION,
        help="Qdrant 集合名（默认独立评测集合，不碰生产集合）",
    )
    parser.add_argument("--top-k", type=int, default=5, help="指标口径 top-k（默认 5）")
    parser.add_argument("--embedder", choices=["auto", "sentence_transformers", "hash"], default="auto",
                        help="embedding 后端：auto 优先真实模型，不可用则 hash 降级")
    parser.add_argument("--rerank-model", default="BAAI/bge-reranker-base",
                        help="cross-encoder 重排模型（Phase 3C），不可用则 N/A")
    parser.add_argument("--no-rerank", action="store_true", help="跳过重排列")
    parser.add_argument("--report", default=str(REPORT_PATH), help="报告输出路径")
    args = parser.parse_args()

    REPORT_PATH = Path(args.report)

    # ── settings + backend ──
    try:
        settings = _EvalSettings()
        backend = _QdrantBackend(
            settings.qdrant_url, settings.qdrant_api_key, settings.qdrant_timeout
        )
    except Exception as exc:  # noqa: BLE001 — friendly CLI error
        print(f"Qdrant 连接失败: {exc}")
        return 3

    # ── chunk + embed ──
    chunks = _chunk_docs()
    chunk_ids = [c[0] for c in chunks]
    chunk_texts = [c[1] for c in chunks]
    chunk_payloads = [c[2] for c in chunks]

    model_name = "BAAI/bge-small-zh-v1.5"
    embed_fn, embed_label, embed_fallback = _make_embedder(args.embedder, model_name)
    sample_dim = len(embed_fn(["测试"])[0])
    if sample_dim != 384:
        print(f"NOTE: 实际向量维度 {sample_dim} ≠ QDRANT_VECTOR_SIZE=384，"
              f"以实际维度建集合（评测集合独立，不影响生产）。")

    backend.ensure_collection(args.collection, sample_dim)
    embeddings = embed_fn(chunk_texts)
    backend.upsert_vectors(args.collection, chunk_ids, embeddings, chunk_payloads)
    print(
        f"已入库 {len(chunks)} chunks（{len(RETRIEVAL_DOCS)} 篇语料），"
        f"开始跑 {len(RETRIEVAL_QUERIES)} 条查询…"
    )

    # ── BM25 关键词侧：复用 ant 自研索引（与 hybrid_store 同一实现）──
    tmp_dir = Path(tempfile.mkdtemp(prefix="eval_bm25_"))
    bm25 = BM25Index(tmp_dir / "bm25.json")
    for cid, text, _ in chunks:
        bm25.add(cid, text)

    # ── rerank ──
    reranker = None if args.no_rerank else _build_reranker(args.rerank_model)

    # ── evaluate ──
    methods = _evaluate(backend, args.collection, embed_fn, bm25, reranker, args.top_k)
    for m in methods:
        r5 = f"{m.recall_5:.4f}" if not math.isnan(m.recall_5) else "N/A"
        mr = f"{m.mrr_score:.4f}" if not math.isnan(m.mrr_score) else "N/A"
        nd = f"{m.ndcg_10:.4f}" if not math.isnan(m.ndcg_10) else "N/A"
        print(f"  {m.method:>18}: recall@{args.top_k}={r5}  MRR={mr}  NDCG@10={nd}")

    _write_report(
        args.collection,
        embed_label,
        sample_dim,
        embed_fallback,
        reranker is not None,
        len(chunks),
        methods,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
