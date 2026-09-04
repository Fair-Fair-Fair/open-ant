"""LongMemEval 官方 judge 协议（Phase 7）。

Prompt 模板逐字移植自官方评测脚本
``xiaowu0162/LongMemEval`` 的 ``src/evaluation/evaluate_qa.py``（MIT
License, Copyright (c) 2024 Di Wu）——judge 契约属于 benchmark 本身，
不是我们的设计选择；换 prompt 等于换尺子，数字就不对外可比了。

用法（repo ``src/`` 目录下）：

    python -m evals.longmemeval_judge \
        --hyp ../workspace/evals/longmemeval/out/memory/hypotheses.jsonl \
        --ref ../workspace/evals/longmemeval/LongMemEval/data/longmemeval_s_cleaned.json \
        --judge-model deepseek/deepseek-v4-flash \
        --workspace ../workspace

输出：``<hyp>.judge.jsonl``（每行附 autoeval_label）+ 总体/分题型准确率。
hyp 文件契约与官方一致：JSONL，每行 ``{"question_id", "hypothesis"}``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# 官方模板（逐字，见模块 docstring 的出处说明）
_ANSCHECK_BASE = (
    "I will give you a question, a correct answer, and a response from a "
    "model. Please answer yes if the response contains the correct answer. "
    "Otherwise, answer no. If the response is equivalent to the correct "
    "answer or contains all the intermediate steps to get the correct "
    "answer, you should also answer yes. If the response only contains a "
    "subset of the information required by the answer, answer no. "
    "\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\n"
    "Is the model response correct? Answer yes or no only."
)

_ANSCHECK_TEMPORAL = (
    "I will give you a question, a correct answer, and a response from a "
    "model. Please answer yes if the response contains the correct answer. "
    "Otherwise, answer no. If the response is equivalent to the correct "
    "answer or contains all the intermediate steps to get the correct "
    "answer, you should also answer yes. If the response only contains a "
    "subset of the information required by the answer, answer no. In "
    "addition, do not penalize off-by-one errors for the number of days. "
    "If the question asks for the number of days/weeks/months, etc., and "
    "the model makes off-by-one errors (e.g., predicting 19 days when the "
    "answer is 18), the model's response is still correct. "
    "\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\n"
    "Is the model response correct? Answer yes or no only."
)

_ANSCHECK_KNOWLEDGE_UPDATE = (
    "I will give you a question, a correct answer, and a response from a "
    "model. Please answer yes if the response contains the correct answer. "
    "Otherwise, answer no. If the response contains some previous "
    "information along with an updated answer, the response should be "
    "considered as correct as long as the updated answer is the required "
    "answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\n"
    "Is the model response correct? Answer yes or no only."
)

_ANSCHECK_PREFERENCE = (
    "I will give you a question, a rubric for desired personalized response, "
    "and a response from a model. Please answer yes if the response "
    "satisfies the desired response. Otherwise, answer no. The model does "
    "not need to reflect all the points in the rubric. The response is "
    "correct as long as it recalls and utilizes the user's personal "
    "information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel "
    "Response: {}\n\nIs the model response correct? Answer yes or no only."
)

_ANSCHECK_ABSTENTION = (
    "I will give you an unanswerable question, an explanation, and a "
    "response from a model. Please answer yes if the model correctly "
    "identifies the question as unanswerable. The model could say that the "
    "information is incomplete, or some other information is given but the "
    "asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel "
    "Response: {}\n\nDoes the model correctly identify the question as "
    "unanswerable? Answer yes or no only."
)

_BASE_TASKS = {"single-session-user", "single-session-assistant", "multi-session"}


def judge_prompt(
    question_type: str,
    question: str,
    answer: str,
    hypothesis: str,
    abstention: bool = False,
) -> str:
    """Build the judge prompt — mirrors the official ``get_anscheck_prompt``."""
    if abstention:
        return _ANSCHECK_ABSTENTION.format(question, answer, hypothesis)
    if question_type in _BASE_TASKS:
        return _ANSCHECK_BASE.format(question, answer, hypothesis)
    if question_type == "temporal-reasoning":
        return _ANSCHECK_TEMPORAL.format(question, answer, hypothesis)
    if question_type == "knowledge-update":
        return _ANSCHECK_KNOWLEDGE_UPDATE.format(question, answer, hypothesis)
    if question_type == "single-session-preference":
        return _ANSCHECK_PREFERENCE.format(question, answer, hypothesis)
    raise ValueError(f"unknown question_type: {question_type!r}")


async def judge_one(llm, entry: dict, hypothesis: str) -> bool:
    """Run the judge for one (ref entry, hypothesis) pair."""
    prompt = judge_prompt(
        entry["question_type"],
        entry["question"],
        entry["answer"],
        hypothesis,
        abstention=entry["question_id"].endswith("_abs"),
    )
    # 官方脚本用 max_tokens=10（gpt-4o 非推理模型）。本项目默认模型是
    # 推理模型（deepseek-v4-flash）：10 token 预算会被隐藏 reasoning 吃光、
    # content 恒为空 → 全判 False。256 让 reasoning + "yes/no" 都有空间
    # （实测 reasoning ~210 token + 1-2 token 结论；chat() 只回传最终 content）。
    response, _, _ = await llm.chat(
        [{"role": "user", "content": prompt}], [], temperature=0, max_tokens=256
    )
    return "yes" in (response or "").lower()


def aggregate(judged: list[dict]) -> dict:
    """Overall + per-question-type accuracy over judged entries."""
    overall = (
        sum(1 for e in judged if e["autoeval_label"]["label"]) / len(judged)
        if judged
        else 0.0
    )
    per_type: dict[str, list[int]] = {}
    for e in judged:
        per_type.setdefault(e["question_type"], []).append(
            1 if e["autoeval_label"]["label"] else 0
        )
    per_type_acc = {
        t: sum(v) / len(v) for t, v in sorted(per_type.items())
    }
    return {"overall": overall, "per_type": per_type_acc, "counts": {
        t: len(v) for t, v in sorted(per_type.items())
    }}


def _load_ref(ref_path: Path) -> dict[str, dict]:
    data = json.loads(ref_path.read_text(encoding="utf-8"))
    return {e["question_id"]: e for e in data}


def _load_hyps(hyp_path: Path) -> list[dict]:
    lines = hyp_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="LongMemEval 官方 judge 协议（prompt 逐字移植，MIT）"
    )
    parser.add_argument("--hyp", required=True, help="hypotheses JSONL 路径")
    parser.add_argument("--ref", required=True, help="reference JSON（s_cleaned）路径")
    parser.add_argument(
        "--judge-model", default=None, help="litellm 模型 id，默认用 workspace 配置模型"
    )
    parser.add_argument("--workspace", default=None, help="workspace 目录（读 llm 配置）")
    parser.add_argument("--concurrency", type=int, default=8, help="judge 并发数")
    args = parser.parse_args(argv)

    from ant.provider.llm.base import LLMProvider
    from ant.utils.config import Config

    ws = (
        Path(args.workspace)
        if args.workspace
        else (Path(__file__).resolve().parents[2] / "workspace")
    ).resolve()
    config = Config.load(ws)
    if args.judge_model:
        config.llm.model = args.judge_model
    llm = LLMProvider.from_config(config.llm)

    hyps = _load_hyps(Path(args.hyp))
    refs = _load_ref(Path(args.ref))

    sem = asyncio.Semaphore(args.concurrency)

    async def _judge_one_gated(entry, ref):
        async with sem:
            return entry, await judge_one(llm, ref, entry["hypothesis"])

    async def _all():
        out: list[dict] = []
        tasks = []
        for entry in hyps:
            ref = refs.get(entry["question_id"])
            if ref is None:
                print(f"Warning: skipping {entry['question_id']} (not in reference)")
                continue
            tasks.append(_judge_one_gated(entry, ref))
        for entry, label in await asyncio.gather(*tasks):
            out.append(
                {
                    **entry,
                    "question_type": refs[entry["question_id"]]["question_type"],
                    "autoeval_label": {
                        "model": args.judge_model or config.llm.model,
                        "label": label,
                    },
                }
            )
        return out

    judged = asyncio.run(_all())
    metrics = aggregate(judged)
    print(f"Overall accuracy: {metrics['overall']:.4f} ({len(judged)} judged)")
    for t, acc in metrics["per_type"].items():
        print(f"  {t}: {acc:.4f} ({metrics['counts'][t]})")

    out_path = Path(str(args.hyp) + ".judge.jsonl")
    with open(out_path, "w", encoding="utf-8") as f:
        for e in judged:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"Saved to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
