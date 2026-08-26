"""Retrieval evaluation metrics — pure, dependency-free functions.

These are the numbers behind the Phase 3D eval report (workspace/plan.md
评测 row): recall@k / MRR / NDCG@k.  Relevance is *binary* for the current
dataset (a doc is either in the ground truth or not), so the NDCG gain is
``2**rel - 1`` which collapses to 1.0 for hits and 0.0 for misses — the
formula is written in the general form anyway so a graded dataset can be
dropped in later without changing the call sites.

Conventions
-----------
- ``ranked_ids`` is the retrieval result, best first; ids are doc-level
  (callers dedupe chunk hits to their source doc before calling these).
- Empty ground truth is treated as *no signal*: recall/NDCG return 0.0
  and MRR returns 0.0 (same as sklearn's ``zero_division`` convention),
  so aggregate means never divide by zero.
- Duplicates in ``ranked_ids`` are ignored (first occurrence wins).
- ``k <= 0`` raises ``ValueError`` — callers must clamp first.
"""

from __future__ import annotations

import math

__all__ = ["recall_at_k", "mrr", "ndcg_at_k"]


def _clamp_k(k: int) -> None:
    if k <= 0:
        raise ValueError(f"k must be a positive integer, got {k!r}")


def recall_at_k(ranked_ids: list[str], ground_truth_ids: list[str], k: int) -> float:
    """Fraction of ground-truth docs that appear in the top ``k`` results.

    ``recall@k = |{gt} ∩ ranked[:k]| / |gt|``.  A hit that appears twice in
    ``ranked_ids`` still counts once (set semantics).

    Examples
    --------
    >>> recall_at_k(["a", "b", "c"], ["c"], 1)
    0.0
    >>> recall_at_k(["a", "b", "c"], ["c"], 3)
    1.0
    >>> recall_at_k(["a", "b", "c"], ["b", "c"], 2)
    0.5
    """
    _clamp_k(k)
    if not ground_truth_ids:
        return 0.0
    gt = set(ground_truth_ids)
    # 集合语义：同一 doc 在 top-k 里出现多次只计一次命中
    hits = len({doc_id for doc_id in ranked_ids[:k] if doc_id in gt})
    return hits / len(gt)


def mrr(ranked_ids: list[str], ground_truth_ids: list[str]) -> float:
    """Mean Reciprocal Rank: reciprocal of the rank of the first hit.

    ``mrr = 1 / rank(first_gt_hit)``, or 0.0 when nothing matches.  A hit at
    rank 1 is a perfect score (1.0); the value drops off quickly for deep
    hits, which is why it rewards ranking quality rather than raw recall.

    Examples
    --------
    >>> mrr(["a", "b", "c"], ["b"])
    0.5
    >>> mrr(["a", "b", "c"], ["x"])
    0.0
    """
    if not ground_truth_ids:
        return 0.0
    gt = set(ground_truth_ids)
    for rank, doc_id in enumerate(ranked_ids, start=1):
        if doc_id in gt:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(
    ranked_ids: list[str], ground_truth_ids: list[str], k: int = 10
) -> float:
    """Normalized Discounted Cumulative Gain at ``k`` (binary relevance).

    Gain per hit is ``2**rel - 1`` = 1.0 (binary), discounted by
    ``1 / log2(rank + 1)``:

        DCG@k  = Σ_{i=1..k} rel_i / log2(i + 1)
        NDCG@k = DCG@k / IDCG@k      (IDCG = DCG of the ideal ordering)

    NDCG is 1.0 when all ground-truth docs sit in the top ``k`` in any
    order (perfect), < 1.0 when hits are buried deep or missing.

    Examples
    --------
    >>> round(ndcg_at_k(["a", "b", "c"], ["a", "c"]), 4)  # hits at ranks 1,3
    0.9197
    >>> ndcg_at_k(["a", "b", "c"], ["a", "b", "c"], k=3)
    1.0
    """
    _clamp_k(k)
    if not ground_truth_ids:
        return 0.0
    gt = set(ground_truth_ids)

    dcg = 0.0
    for rank, doc_id in enumerate(ranked_ids[:k], start=1):
        if doc_id in gt:
            dcg += (2 ** 1 - 1) / math.log2(rank + 1)  # binary gain = 1.0

    # Ideal ordering: all hits on top (gain 1 each, discount by position).
    ideal_hits = min(len(gt), k)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0
