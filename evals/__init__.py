"""Open-Ant evaluation suite.

Components
----------
- ``run_guardrail_eval`` + ``dataset_guardrail`` — injection-guardrail eval
  (20 malicious + 20 benign samples, CI threshold gate)
- ``run_longmemeval_eval`` + ``longmemeval_judge`` — LongMemEval public
  benchmark (ICLR 2025), five-mode ablation under protocol v2 (non-thinking)

Self-built small-sample evals (retrieval corpus / memory tasks / sparse
experiment) were removed on 2026-09-12; historical conclusions are archived
in ``workspace/code.md``.
"""

__all__ = [
    "dataset_guardrail",
    "run_guardrail_eval",
    "run_longmemeval_eval",
    "longmemeval_judge",
]
