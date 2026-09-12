"""Phase 7 — LongMemEval harness 纯函数测试（无网络、不调 LLM）。

覆盖：judge 模板选择（官方契约逐字移植）、时间戳归一化、分层抽样
确定性、answer prompt 组装、evidence 提取、resume 加载、聚合指标。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from evals.longmemeval_judge import (
    aggregate,
    empty_hypothesis_verdict,
    judge_prompt,
    judge_request_kwargs,
)
from evals.run_longmemeval_eval import (
    _ingest_instance,
    _wipe_target,
    answer_prompt,
    apply_sparse_model,
    eval_collection_for_mode,
    evidence_text,
    instance_filter,
    load_done_ids,
    non_thinking_overrides,
    normalize_ts,
    sample_instances,
)


def test_eval_collection_per_mode_isolation():
    """memory/chunks 必须各用独立集合（2026-09-07 跨模式污染事故回归）。"""
    assert eval_collection_for_mode("memory") == "ant_memory_lmeval_memory"
    assert eval_collection_for_mode("chunks") == "ant_memory_lmeval_chunks"
    assert eval_collection_for_mode("baseline") == "ant_memory_lmeval"
    assert eval_collection_for_mode("oracle") == "ant_memory_lmeval"


# ── judge 非思考模式（2026-09-07 推理模型吃光 max_tokens 事故回归） ─────────


def test_judge_request_kwargs_deepseek_gets_thinking_disabled():
    """DeepSeek 思考模型必须用 extra_body 直传 thinking=disabled——
    litellm 1.89.3 transformer 只转发 enabled（disabled/none 静默丢弃）。"""
    kwargs = judge_request_kwargs("deepseek/deepseek-v4-flash")
    assert kwargs["max_tokens"] == 10
    assert kwargs["temperature"] == 0
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


def test_judge_request_kwargs_non_deepseek_no_extra_body():
    kwargs = judge_request_kwargs("openai/gpt-4o")
    assert kwargs["max_tokens"] == 10
    assert "extra_body" not in kwargs


def test_judge_request_kwargs_none_model_no_extra_body():
    assert "extra_body" not in judge_request_kwargs(None)


# ── 退化回答确定性短路（2026-09-07 非思考 judge 对空回答误判 yes 事故回归） ──


def test_empty_hypothesis_verdict_false():
    assert empty_hypothesis_verdict("") is False
    assert empty_hypothesis_verdict("   \n\t") is False


def test_empty_hypothesis_verdict_non_empty_defers_to_llm():
    assert empty_hypothesis_verdict("I don't know.") is None
    assert empty_hypothesis_verdict("Four") is None


# ── 评测全链非思考（协议 v2：对齐官方 gpt-4o 非思考协议） ─────────────────


def test_non_thinking_overrides_deepseek_extra_body():
    assert non_thinking_overrides("deepseek/deepseek-v4-flash") == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }


def test_non_thinking_overrides_non_deepseek_empty():
    assert non_thinking_overrides("openai/gpt-4o") == {}
    assert non_thinking_overrides(None) == {}


# ── wipe 按模式 + 提取幂等（2026-09-11 wipe 未随隔离修复更新的回归） ─────


def test_wipe_target_uses_per_mode_collection():
    """fresh 跑必须清按模式隔离的集合，不是旧共享名 EVAL_COLLECTION。"""
    assert _wipe_target("memory") == "ant_memory_lmeval_memory"
    assert _wipe_target("chunks") == "ant_memory_lmeval_chunks"
    assert _wipe_target("memory") != "ant_memory_lmeval"  # 事故断言


async def test_ingest_instance_precleans_own_points():
    """提取前先 delete_by_filter 本实例旧点（中断续跑不产生重复记忆）。"""
    calls: list = []

    class FakeStore:
        async def add(self, documents=None, metadatas=None, ids=None):
            calls.append(("add", len(documents or [])))

        async def delete_by_filter(self, where):
            calls.append(("delete", where))

    class FakeGuard:
        async def extract_memories(self, messages, where=None, max_tokens=None):
            return [{
                "content": "fact", "category": "fact", "importance": 5,
                "keywords": [], "memory_id": "m1",
            }]

    ctx = SimpleNamespace(
        memory_guard=FakeGuard(), vector_store=FakeStore(), graph=None
    )
    inst = {
        "haystack_sessions": [[{"role": "user", "content": "hi"}]],
        "haystack_dates": ["2023/01/01 (Sun) 00:00"],
    }
    n = await _ingest_instance(ctx, inst, idx=7, batch_size=12)
    assert n == 1
    assert calls[0] == ("delete", {"session_id": "lmeval-7"})
    assert calls[1][0] == "add" and calls[1][1] == 1


# ── 评测稀疏模型覆写（2026-09-11 fastembed BM25 依赖 GCS 国内不可达回归） ──


def test_apply_sparse_model_patches_memory_config():
    cfg = SimpleNamespace(memory=SimpleNamespace(sparse_model="fastembed"))
    apply_sparse_model(cfg, "jieba")
    assert cfg.memory.sparse_model == "jieba"


def test_apply_sparse_model_none_is_noop():
    cfg = SimpleNamespace(memory=SimpleNamespace(sparse_model="fastembed"))
    apply_sparse_model(cfg, None)
    assert cfg.memory.sparse_model == "fastembed"


# ── judge 模板（官方契约） ──────────────────────────────────────────────────


def test_judge_prompt_base_tasks():
    for qtype in ("single-session-user", "single-session-assistant", "multi-session"):
        prompt = judge_prompt(qtype, "Q", "A", "H")
        assert "subset of the information" in prompt
        assert "Q" in prompt and "A" in prompt and "H" in prompt
        assert "off-by-one" not in prompt


def test_judge_prompt_temporal_reasoning_has_offbyone_rule():
    prompt = judge_prompt("temporal-reasoning", "Q", "A", "H")
    assert "off-by-one" in prompt


def test_judge_prompt_knowledge_update_allows_old_plus_new():
    prompt = judge_prompt("knowledge-update", "Q", "A", "H")
    assert "previous information along with an updated answer" in prompt


def test_judge_prompt_preference_uses_rubric_wording():
    prompt = judge_prompt("single-session-preference", "Q", "A", "H")
    assert "Rubric" in prompt and "recalls and utilizes" in prompt


def test_judge_prompt_abstention_overrides_type():
    prompt = judge_prompt("multi-session", "Q", "A", "H", abstention=True)
    assert "unanswerable" in prompt
    assert "subset of the information" not in prompt


def test_judge_prompt_unknown_type_raises():
    with pytest.raises(ValueError):
        judge_prompt("no-such-type", "Q", "A", "H")


def test_aggregate_math():
    judged = [
        {"question_type": "a", "autoeval_label": {"label": True}},
        {"question_type": "a", "autoeval_label": {"label": False}},
        {"question_type": "b", "autoeval_label": {"label": True}},
    ]
    m = aggregate(judged)
    assert m["overall"] == pytest.approx(2 / 3)
    assert m["per_type"] == {"a": 0.5, "b": 1.0}
    assert m["counts"] == {"a": 2, "b": 1}


def test_aggregate_empty():
    m = aggregate([])
    assert m["overall"] == 0.0 and m["per_type"] == {}


# ── runner 纯函数 ───────────────────────────────────────────────────────────


def test_normalize_ts_iso_sortable():
    assert normalize_ts("2023/05/30 (Tue) 23:40") == "2023-05-30T23:40"


def test_normalize_ts_fallback_on_garbage():
    assert normalize_ts("not-a-date") == "not-a-date"
    # 字典序 = 时间序（图冲突检测按字符串比较）
    assert normalize_ts("2023/01/02 (Mon) 09:00") < normalize_ts("2023/12/31 (Sun) 09:00")


def test_instance_filter_uses_indexed_session_id():
    assert instance_filter(7) == {"session_id": "lmeval-7"}


def test_answer_prompt_contains_date_context_and_question():
    prompt = answer_prompt("What is my name?", "2023/05/30 (Tue) 23:40", "name is Bob")
    assert "2023/05/30 (Tue) 23:40" in prompt
    assert "name is Bob" in prompt
    assert "What is my name?" in prompt
    assert "I don't know" in prompt  # 弃答纪律（abstention 友好）


def test_answer_prompt_empty_context_placeholder():
    prompt = answer_prompt("Q?", "D", "")
    assert "(no retrieved memory)" in prompt


def _inst(qid, qtype):
    return {
        "question_id": qid,
        "question_type": qtype,
        "question": f"Q {qid}",
        "answer": "A",
        "question_date": "2023/01/01 (Sun) 00:00",
        "haystack_session_ids": ["s1", "s2"],
        "haystack_dates": ["2023/01/01 (Sun) 00:00", "2023/01/02 (Mon) 00:00"],
        "haystack_sessions": [
            [{"role": "user", "content": "hello"}],
            [{"role": "assistant", "content": "hi there"}],
        ],
        "answer_session_ids": ["s1"],
    }


def test_sample_instances_stratified_covers_all_types():
    data = (
        [_inst(f"a{i}", "single-session-user") for i in range(10)]
        + [_inst(f"b{i}", "multi-session") for i in range(10)]
        + [_inst(f"c{i}", "temporal-reasoning") for i in range(10)]
        + [_inst(f"d{i}", "knowledge-update") for i in range(10)]
    )
    picked = sample_instances(data, n=8, seed=42)
    assert len(picked) == 8
    types = {e["question_type"] for e in picked}
    assert types == {
        "single-session-user",
        "multi-session",
        "temporal-reasoning",
        "knowledge-update",
    }


def test_sample_instances_deterministic_per_seed():
    data = [_inst(f"q{i}", "multi-session") for i in range(50)]
    assert [e["question_id"] for e in sample_instances(data, 10, seed=1)] == [
        e["question_id"] for e in sample_instances(data, 10, seed=1)
    ]


def test_sample_instances_full_when_n_exceeds_size():
    data = [_inst(f"q{i}", "multi-session") for i in range(5)]
    assert len(sample_instances(data, n=500, seed=42)) == 5


def test_sample_instances_tiny_n_falls_back_to_random_sample():
    data = (
        [_inst(f"a{i}", "single-session-user") for i in range(5)]
        + [_inst(f"b{i}", "multi-session") for i in range(5)]
        + [_inst(f"c{i}", "knowledge-update") for i in range(5)]
    )
    picked = sample_instances(data, n=2, seed=42)
    assert len(picked) == 2


def test_evidence_text_joins_answer_sessions():
    inst = _inst("q1", "multi-session")
    text = evidence_text(inst)
    assert "user: hello" in text
    assert "assistant: hi there" not in text  # s2 不是 evidence


def test_load_done_ids_resume(tmp_path):
    hyp = tmp_path / "hypotheses.jsonl"
    hyp.write_text(
        '{"question_id": "a", "hypothesis": "x"}\n\n{"bad json\n',
        encoding="utf-8",
    )
    assert load_done_ids(hyp) == {"a"}
    assert load_done_ids(tmp_path / "missing.jsonl") == set()
