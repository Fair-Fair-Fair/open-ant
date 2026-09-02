"""Phase 7 — LongMemEval harness 纯函数测试（无网络、不调 LLM）。

覆盖：judge 模板选择（官方契约逐字移植）、时间戳归一化、分层抽样
确定性、answer prompt 组装、evidence 提取、resume 加载、聚合指标。
"""

from __future__ import annotations

import pytest

from evals.longmemeval_judge import aggregate, judge_prompt
from evals.run_longmemeval_eval import (
    answer_prompt,
    evidence_text,
    instance_filter,
    load_done_ids,
    normalize_ts,
    sample_instances,
)

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
