"""Open-Ant evaluation suite (Phase 3D).

Components
----------
- ``metrics``            — retrieval metrics (recall@k / MRR / NDCG@k), pure & testable
- ``dataset_retrieval``  — hand-written Chinese retrieval corpus (20 docs) +
                          30 annotated queries with ground truth
- ``dataset_memory_tasks`` — 10 multi-turn memory tasks (remember → distract → probe)
- ``run_retrieval_eval`` — CLI that runs the retrieval eval against Qdrant and
                          writes ``report_retrieval.md``

This package deliberately keeps its data modules dependency-free so tests
(``ant/tests/test_eval_metrics.py``) can import them without pulling in
Qdrant / sentence-transformers / litellm.
"""

__all__ = [
    "metrics",
    "dataset_retrieval",
    "dataset_memory_tasks",
    "run_retrieval_eval",
]
