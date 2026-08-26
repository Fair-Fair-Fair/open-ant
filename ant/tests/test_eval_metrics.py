"""Tests for the Phase 3D eval suite: retrieval metrics + dataset integrity.

Pure-python only (no ant imports) — see conftest.py note: data/utility
tests must not drag in heavy deps.  Covers:

- metrics: hand-computed recall@k / MRR / NDCG@k examples, including the
  edge cases the eval aggregator depends on (empty results, empty ground
  truth, all hits, partial hits, k truncation, duplicates, k<=0).
- dataset_retrieval: corpus size/shape, doc length bound 150–300 chars,
  non-empty keyword sets, 30 queries each with 1–3 ground-truth ids that
  all resolve to existing docs, every doc reachable by at least one query.
- dataset_memory_tasks: 10 tasks × exactly 3 turns, expected hits resolve.
"""

import pytest

from evals.dataset_memory_tasks import MEMORY_TASKS
from evals.dataset_retrieval import (
    DOC_ID_SET,
    RETRIEVAL_DOCS,
    RETRIEVAL_QUERIES,
)
from evals.metrics import mrr, ndcg_at_k, recall_at_k

# ===========================================================================
# recall_at_k
# ===========================================================================

class TestRecallAtK:
    def test_all_hits_within_k(self):
        assert recall_at_k(["a", "b", "c"], ["a", "b", "c"], k=3) == 1.0

    def test_partial_hits(self):
        assert recall_at_k(["a", "b", "c"], ["b", "c"], k=2) == 0.5

    def test_no_hits(self):
        assert recall_at_k(["a", "b", "c"], ["x"], k=3) == 0.0

    def test_k_truncation_cuts_hits(self):
        # 命中的 doc 排在 k 之外 → 不计入
        assert recall_at_k(["a", "b", "c"], ["a", "b", "c"], k=1) == pytest.approx(1 / 3)

    def test_k_larger_than_result_list(self):
        assert recall_at_k(["a"], ["a", "b"], k=5) == 0.5

    def test_empty_ranked_results(self):
        assert recall_at_k([], ["a"], k=5) == 0.0

    def test_empty_ground_truth_is_zero(self):
        # 无标注 → 无信号（聚合均值不除零）
        assert recall_at_k(["a", "b"], [], k=5) == 0.0

    def test_duplicate_hits_count_once(self):
        assert recall_at_k(["a", "a", "b"], ["a"], k=3) == 1.0

    def test_non_positive_k_raises(self):
        with pytest.raises(ValueError):
            recall_at_k(["a"], ["a"], k=0)
        with pytest.raises(ValueError):
            recall_at_k(["a"], ["a"], k=-1)


# ===========================================================================
# mrr
# ===========================================================================

class TestMrr:
    def test_first_hit_at_rank_1(self):
        assert mrr(["a", "b", "c"], ["a"]) == 1.0

    def test_first_hit_at_rank_2(self):
        assert mrr(["a", "b", "c"], ["b"]) == 0.5

    def test_first_hit_at_rank_3(self):
        assert mrr(["a", "b", "c"], ["c"]) == pytest.approx(1 / 3)

    def test_no_hit(self):
        assert mrr(["a", "b", "c"], ["x"]) == 0.0

    def test_empty_ranked_results(self):
        assert mrr([], ["a"]) == 0.0

    def test_empty_ground_truth_is_zero(self):
        assert mrr(["a"], []) == 0.0

    def test_duplicates_first_occurrence_wins(self):
        assert mrr(["a", "a"], ["a"]) == 1.0


# ===========================================================================
# ndcg_at_k（二元相关性，增益 2^rel-1 = 1.0，折扣 1/log2(rank+1)）
# ===========================================================================

class TestNdcgAtK:
    def test_perfect_ordering_is_one(self):
        assert ndcg_at_k(["a", "b", "c"], ["a", "b", "c"], k=3) == 1.0

    def test_hits_at_ranks_1_and_3(self):
        # DCG = 1/log2(2) + 1/log2(4) = 1 + 0.5 = 1.5
        # IDCG = 1/log2(2) + 1/log2(3) = 1 + 0.63093 = 1.63093
        assert ndcg_at_k(["a", "b", "c"], ["a", "c"], k=3) == pytest.approx(1.5 / 1.63093, rel=1e-4)

    def test_all_hits_any_order_is_one(self):
        # 三个命中全在 top3，无论内部顺序 → NDCG = 1
        assert ndcg_at_k(["c", "b", "a"], ["a", "b", "c"], k=3) == 1.0

    def test_no_hits(self):
        assert ndcg_at_k(["a", "b"], ["x"], k=2) == 0.0

    def test_k_truncation_drops_deep_hits(self):
        # 命中在 top1 之外 → 0
        assert ndcg_at_k(["x", "a"], ["a"], k=1) == 0.0

    def test_empty_ranked_results(self):
        assert ndcg_at_k([], ["a"], k=10) == 0.0

    def test_empty_ground_truth_is_zero(self):
        assert ndcg_at_k(["a"], [], k=10) == 0.0

    def test_default_k_is_10(self):
        # 默认参数 k=10 与 runner 的 NDCG@10 口径一致
        assert ndcg_at_k(["a"], ["a"]) == 1.0
        # k=10 无截断时，单个命中即完美 → 1.0
        assert ndcg_at_k(["a", "b"], ["a"]) == 1.0

    def test_non_positive_k_raises(self):
        with pytest.raises(ValueError):
            ndcg_at_k(["a"], ["a"], k=0)


# ===========================================================================
# 数据集完整性：dataset_retrieval.py
# ===========================================================================

class TestRetrievalDataset:
    def test_corpus_has_20_docs_with_sequential_ids(self):
        assert len(RETRIEVAL_DOCS) == 20
        assert [d.doc_id for d in RETRIEVAL_DOCS] == [f"doc_{i:02d}" for i in range(1, 21)]
        assert len(set(d.doc_id for d in RETRIEVAL_DOCS)) == 20

    def test_every_doc_between_150_and_300_chars(self):
        for doc in RETRIEVAL_DOCS:
            n = len(doc.text)
            assert 150 <= n <= 300, f"{doc.doc_id} 长度 {n} 超出 [150, 300]"

    def test_every_doc_has_3_to_5_non_empty_keywords(self):
        for doc in RETRIEVAL_DOCS:
            assert 3 <= len(doc.keywords) <= 5, f"{doc.doc_id} 关键词数量 {len(doc.keywords)}"
            assert all(k.strip() for k in doc.keywords), f"{doc.doc_id} 存在空关键词"
            assert len(set(doc.keywords)) == len(doc.keywords), f"{doc.doc_id} 关键词重复"

    def test_corpus_text_is_meaningful(self):
        # 防占位符：正文不能只有标点/空白，且含中文
        for doc in RETRIEVAL_DOCS:
            assert any("一" <= ch <= "鿿" for ch in doc.text), f"{doc.doc_id} 无中文字符"

    def test_30_queries_with_unique_ids(self):
        assert len(RETRIEVAL_QUERIES) == 30
        assert len(set(q.query_id for q in RETRIEVAL_QUERIES)) == 30

    def test_every_query_has_ground_truth_referencing_existing_docs(self):
        for q in RETRIEVAL_QUERIES:
            assert 1 <= len(q.ground_truth) <= 3, (
                f"{q.query_id} ground truth 数量 {len(q.ground_truth)}"
            )
            unknown = set(q.ground_truth) - DOC_ID_SET
            assert not unknown, f"{q.query_id} 引用了不存在的 doc: {unknown}"

    def test_query_type_is_registered(self):
        allowed = {"specific", "vague", "rewrite", "combined"}
        for q in RETRIEVAL_QUERIES:
            assert q.query_type in allowed, f"{q.query_id} 未知 query_type: {q.query_type}"
            assert q.query.strip(), f"{q.query_id} 查询为空"

    def test_every_doc_is_reachable_by_some_query(self):
        covered = {doc_id for q in RETRIEVAL_QUERIES for doc_id in q.ground_truth}
        assert covered == DOC_ID_SET, f"未覆盖的 doc: {DOC_ID_SET - covered}"

    def test_queries_are_not_verbatim_doc_excerpts(self):
        # 措辞与原文错开是数据集设计约束（测语义检索而非字面匹配）：
        # 任何查询不得包含某篇语料的完整句子（以 ≥12 字连续片段近似判定）。
        for q in RETRIEVAL_QUERIES:
            for doc in RETRIEVAL_DOCS:
                for i in range(len(doc.text) - 11):
                    frag = doc.text[i : i + 12]
                    if frag in q.query:
                        pytest.fail(f"{q.query_id} 与 {doc.doc_id} 存在 12 字连续重叠: {frag!r}")


# ===========================================================================
# 数据集完整性：dataset_memory_tasks.py
# ===========================================================================

class TestMemoryTaskDataset:
    def test_ten_tasks_with_unique_ids(self):
        assert len(MEMORY_TASKS) == 10
        assert len(set(t.task_id for t in MEMORY_TASKS)) == 10

    def test_every_task_has_exactly_3_user_turns(self):
        for task in MEMORY_TASKS:
            assert len(task.turns) == 3, f"{task.task_id} turns 数量 {len(task.turns)}"
            assert all(turn.role == "user" for turn in task.turns), f"{task.task_id} 仅支持 user 轮"
            assert all(turn.text.strip() for turn in task.turns), f"{task.task_id} 存在空消息"

    def test_probe_turn_is_a_question(self):
        # 第 3 轮必须是追问形式（包含疑问词或问号）——任务结构约定
        for task in MEMORY_TASKS:
            probe = task.turns[2].text
            assert ("？" in probe or "?" in probe or "吗" in probe), (
                f"{task.task_id} 第 3 轮不是追问"
            )

    def test_expected_hits_reference_valid_docs(self):
        for task in MEMORY_TASKS:
            assert task.expected_hits, f"{task.task_id} 缺少期望命中"
            for hit in task.expected_hits:
                assert hit.doc_id in DOC_ID_SET, f"{task.task_id} 引用了不存在的 doc: {hit.doc_id}"
                assert hit.fact.strip(), f"{task.task_id} 期望事实描述为空"

    def test_task_ids_are_unique_and_description_present(self):
        for task in MEMORY_TASKS:
            assert task.title.strip(), f"{task.task_id} 缺标题"
            assert task.description.strip(), f"{task.task_id} 缺描述"
