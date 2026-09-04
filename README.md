# 🐜 OpenAnt-MemoryArk

> **When a mind begins to forget the world, the world should not forget the person.**

**OpenAnt-MemoryArk is an always-on personal AI agent runtime**: an event-driven multi-agent kernel, graph-augmented long-term memory, and defense-in-depth security — every component integration-tested against real infrastructure (MySQL / RabbitMQ / Redis / Qdrant / Neo4j), with **431 automated tests + CI gates**.

Its first application scenario is the **Memory Ark** — a cognitive-assistance prototype for Alzheimer's family caregiving: preserving the fading external world of the elderly (relationships, life events, daily habits), gently re-grounding them in reality when they are confused, and signaling family members when memory anomalies appear.

> ⚠️ Honest statement: the Memory Ark is a **technical proof of concept**, not a medical product — it makes no diagnoses, gives no medication advice, claims no therapeutic effect, and never impersonates a real relative. Real caregiving deployment requires medical compliance and human supervision. See [`memory-ark.md`](./memory-ark.md) (Chinese) for the scenario design.

> Production-grade release on PyPI: `pip install open-ant-harness` (v0.2.0)

> 中文文档：[`README_zh.md`](./README_zh.md)。叙事与面试文档多为中文（`memory-ark.md` / `interview.md` / `eval.md`）。

---

## A Day on the Memory Ark

```text
09:00  Xiao'an (voice): "Good morning, Grandma Chen. It's Wednesday — time for breakfast."
10:30  Grandma: "Who are you?"
       Xiao'an: "I'm Xiao'an, the companion who has talked with you every day
                for three years. Your daughter is Emily — she used to take you
                to the park when she was little. She's coming to see you this afternoon."
       Grandma: "I can't remember…"
       Xiao'an: "That's all right. I'll remember for you."
19:00  Family receives a daily report (Telegram): "'Where is my daughter?' was asked
       14 times this week, +40% vs. last week; medication reminders completed 6/7.
       Consider mentioning this at the next doctor visit."
```

One scenario, three layers of technology: **memory** (a life archive = Neo4j memory graph + hybrid retrieval), **conversation** (gentle re-grounding = persona config + retrieval injection), and **guardianship** (signal reports = cron jobs + multi-channel delivery). Every step is backed by evaluation: memory QA runs on LongMemEval (ICLR 2025, 500 questions), "admit what you don't know" is an enforced abstention discipline, high-risk actions require human confirmation, and injection guardrails have measured detection/false-positive numbers.

## Why OpenAnt: this category needs a production-grade answer

In 2026, always-on agents became a recognized industry category (sparked by OpenClaw, followed by Gemini Spark and Microsoft Scout). But handing your computer and account permissions to a model raises trust problems: security vendors publicly warned about OpenClaw (CVE-2026-25253 gateway hijack, CVSS 8.8), a malicious-skill marketplace, and real prompt-injection incidents. In the Memory Ark scenario, these are not "compliance items" — they are the bottom line: **an assistant that misremembers your loved ones is not a bug, it is harm.**

OpenAnt is built to answer that trust problem, and every promise is backed by tests and numbers (details below):

- **Messages are never lost**: RabbitMQ durable queues + 5-level DLX retry ladder + transactional outbox + consumer idempotency
- **Permissions are controllable**: 3-layer sandbox / human-in-the-loop confirmation / full audit trail
- **Memory is arbitrable**: Neo4j conflict detection with LLM arbitration + LongMemEval benchmark
- **Fully observable**: OpenTelemetry tracing across agents + Prometheus metrics

## Architecture

```text
                     CLI │ Telegram │ Discord │ WebSocket(auth)
                        │            │          │
                        ▼            ▼          ▼
                ┌───────────────────────────────────────┐
                │          CompositeBus                  │
                │   persistent events ──► RabbitMQ/Outbox│
                │   (durable queues + DLX retries + DLQ) │
                │   transient events ──► in-process      │
                │   (streaming tokens/confirmations)     │
                └──────────────┬────────────────────────┘
                               │ consumed (idempotent dedup)
        ┌──────────────┬───────┴────────┬──────────────┐
        ▼              ▼                ▼              ▼
  AgentWorker    DeliveryWorker   ChannelWorker   CronWorker
        │
        ▼
┌────────────────────────────────────────────────────┐
│         StreamPipeline (9-stage onion middleware)   │
│  Validation → InputGuard(regex+LLM-judge) →        │
│  Observability → ContextBuild → ContextGuard →     │
│  LLMCall(Router+StreamRedactor) → ToolExecution    │
│  (confirm-first→writes serial→reads parallel) →    │
│  OutputGuard → Terminal (stream-loss fallback)     │
└───────────────┬────────────────────────────────────┘
                │
   ┌────────────┼────────────────┬────────────────┐
   ▼            ▼                ▼                ▼
 MySQL      RabbitMQ          Qdrant(dense+     Neo4j(memory graph:
 (history/  (event bus)        sparse hybrid)    conflict arbitration)
 audit/cost/outbox)             │                │
   │            │               ▼                ▼
   └──── Redis ─┘    retrieval: rewrite→hybrid→graph-expand→rerank
    (embedding cache/rate limit)    │
                                   ▼
                    Prometheus /metrics · /healthz · /readyz
```

## Quick Start

```bash
# 1. Install (production release v0.2.0)
pip install open-ant-harness
# Or install from source (latest):
git clone https://github.com/Fair-Fair-Fair/open-ant.git && cd open-ant
pip install -e src

# 2. Initialize a workspace (generates config.user.yaml and a default agent)
open-ant init --workspace ./workspace

# 3. Configure credentials (open-ant/.env — variable names below; fill in the values
#    yourself, the code never prints credentials)
#    LLM: DEEPSEEK_API_KEY / LLM_MODEL_ID / BASE_URL
#    Infrastructure: MYSQL_USERNAME / MYSQL_PASSWORD / RABBITMQ_USERNAME / RABBITMQ_PASSWORD
#    Memory: QDRANT_URL / QDRANT_API_KEY / QDRANT_COLLECTION / QDRANT_VECTOR_SIZE
#            NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD / NEO4J_DATABASE
#    Redis defaults to redis://127.0.0.1:6379/0 with no password locally
#    Note: missing MySQL/RabbitMQ credentials fall back to JSONL/in-memory bus
#    and are reported as ERROR by `doctor`

# 4. Startup self-check (nine items: config/routing/agent/docker/history/mysql/rabbitmq/disk)
open-ant doctor --workspace ./workspace

# 5. Chat
open-ant chat --workspace ./workspace              # CLI chat
open-ant chat --workspace ./workspace --agent pickle
open-ant server --workspace ./workspace            # 24/7 server (WebSocket/Telegram/Discord/cron)
open-ant ingest ./docs/myfile.pdf --workspace ./workspace   # ingest documents
open-ant migrate-chroma --workspace ./workspace    # Chroma → Qdrant migration
```

**Infrastructure dependencies**: MySQL / RabbitMQ / Redis are recommended to run locally or in containers (default ports 3306/5672/6379); Qdrant / Neo4j use the cloud services or self-hosted instances configured in `.env`. Missing credentials do not block startup — the corresponding capabilities degrade gracefully with clear warnings, and `doctor` tells you what is missing.

## Core Capabilities

### Reliability (messages are never lost, failures recover)
- **RabbitMQ**: durable queues + manual ack + **5-level DLX TTL retry ladder** (5s→30min) + dead-letter queue; delivery failures are nacked and retried, and the consumer side deduplicates via `processed_messages` (**idempotency**, at-least-once semantics, verified by real-broker integration tests)
- **Outbox pattern**: events are written to MySQL in the same transaction as business state and marked after publisher confirm — a process crash loses no messages
- **LLM layer**: litellm Router with retries/timeouts/model fallback chains; per-session token/cost accounting (queryable `usage_records`); context thresholds computed dynamically per model, with hard-truncation fallback when compression fails
- **Graceful shutdown**: publisher→workers→bus→uvicorn→storage engines drained in order; 15s worker shutdown timeout fallback; crashed workers restart with exponential backoff (5s→120s)

### Graph-Augmented Memory (more than a vector store)
- **Qdrant**: dense + BM25 sparse named vectors, server-side prefetch + RRF fusion, payload filters, automatic payload index creation
- **Neo4j memory graph**: entity/relationship modeling, **conflict detection with LLM arbitration** (SUPERSEDES edges), soft-archiving of low-importance memories with TTL
- **Retrieval pipeline**: query rewrite → hybrid → subgraph expansion → cross-encoder rerank → `<retrieved>` delimiter injection defense
- **Extraction layer**: tool-call-constrained JSON (one bad record does not poison the whole batch)
- **Bundled evals**: `python -m evals.run_retrieval_eval` — 20 Chinese corpora × 30 annotated queries, dense **0.983** / hybrid(RRF) **0.983** (matching dense under bge-small-zh) / +rerank **0.967** (recall@5, reproducible report); long-term memory QA benchmarked on LongMemEval (ICLR 2025, see `eval.md`); Chinese sparse-model comparison in `evals/report_sparse_zh.md`

### Security
- Three-layer sandbox: paths (blocks config/secrets) · Docker commands (`--user` non-root, hard memory/CPU limits, read-only root filesystem) · network (SSRF defense, domain allow/deny lists + private-IP blocking)
- Input guardrails (NFKC normalization / mixed-script detection / regex injection + **LLM-judge semantic review**) + output guardrails (**streaming redaction**: review-before-emit sliding buffer) + tool-result injection scanning
- WS/API token auth (constant-time compare, 4401 rejection), fail-closed confirmation bindings, Redis sliding-window rate limiting (fail-open)
- **Guardrails are evaluated**: 20 malicious + 20 benign samples, measured **detection rate 85%, false-positive rate 0%** (`python -m evals.run_guardrail_eval --ci`, CI gate ≥60% and ≤20%)
- Credential discipline: secrets live only in `.env`; zero leakage in logs/tests/docs (enforced by the check_publish leak-scan gate)

### Engineering
- **431 automated tests** (pytest) + ruff + GitHub Actions CI, including real MySQL / RabbitMQ / cloud Qdrant / Neo4j Aura integration tests (auto-skipped without credentials)
- **Publish gate** (`check_publish.py`): secret-pattern scanning + filename blocklist — a mandatory process after the 0.1.0 key-leak incident; a non-zero exit forbids upload
- Traceable evolution: 26.1 toy → 27.0 hardening+tests → 28.0 storage/messaging → 29.0 LLM/tools → 30.0 memory → 31.0 security/observability → 32.0 wrap-up → 35.x evals/tracing (every step reproducible via `git log`)

## Directory Structure

```
src/                      # git repo root
├── ant/
│   ├── core/             # pipeline/guards/routing/context/FSM/tracing
│   ├── server/           # workers/auth/rate-limiting/observability/app
│   ├── bus/              # EventBus protocol + InMemory/RabbitMQ/Composite/Outbox
│   ├── storage/          # SQLAlchemy models/repositories/Alembic migrations
│   ├── memory/           # Neo4j memory graph/constrained extraction/rerank
│   ├── provider/         # LLM Router/Qdrant/embedding(Redis cache)/retrieval
│   ├── tools/            # built-in tools/policy governance/audit
│   ├── channel/ cli/ utils/
│   └── tests/            # 431 tests
├── evals/                # retrieval/guardrail/LongMemEval evals (corpora/metrics/runners/reports)
└── pyproject.toml        # packaging/test/lint config (sdist allowlist prevents key leaks)
```

## Testing & Evals

```bash
cd src
python -m pytest -q                        # 431 passed
ruff check ant                             # 0 errors
python -m evals.run_retrieval_eval         # three retrieval variants compared → evals/report_retrieval.md
python -m evals.run_guardrail_eval --ci    # injection guardrail detection/false-positive (same gate as CI)
python -m evals.agent_task_runner          # 10 memory-task offline skeleton scoring
python -m evals.sparse_zh_experiment       # real-cloud Chinese sparse model comparison
python check_publish.py                    # pre-publish secret-scan gate (non-zero forbids upload)
```

## Docs & Interview Prep

[`interview.md`](./interview.md) (Chinese) collects **project design docs and an interview question bank**:
- **Project section** — a full-system map and complete flow paths for key processes (event flow / Agent Loop & LLM Loop / memory writes / retrieval / context & prompt assembly / tool execution / SubAgent / config & credentials / infrastructure roles / trace propagation), each located to specific files;
- **Interview section** — agent-domain interview questions with answer outlines based on the real implementation (including probeable code locations and honest boundary statements).

Other narrative docs: [`memory-ark.md`](./memory-ark.md) (Memory Ark scenario plan, Chinese), [`eval.md`](./eval.md) (benchmark plan, Chinese), [`trace.md`](./trace.md) (OpenTelemetry tracing prep, Chinese).

## Contributing

If this direction interests you — agent runtimes, graph-augmented memory, evaluation systems, or cognitive-assistance scenarios like the Memory Ark — **welcome aboard as a contributor**. A good start: get through [Quick Start](#quick-start), pick up a small task from the issues, or open a PR directly. The bar is simple: `pytest` all green + `ruff check ant` zero errors.

If you hope this project gets seen by more people, and funded by service providers or research institutions, **please give it a star ⭐** — a star is the most direct signal of recognition for an open-source project, and the first step toward being seen and funded.

## License (dual)

- **Code** (`ant/`, `evals/`, etc.): [MIT License](./LICENSE)
- **Narrative & docs** (README, `memory-ark.md`, `interview.md`, etc.): [CC BY-SA 4.0](./LICENSE-DOCS) (Attribution-ShareAlike) — redistribution and adaptation must preserve attribution and be released under the same license. The license protects the expression of the narrative text (ideas themselves are not copyrightable, but the expression of the "Memory Ark" narrative belongs to this project).

## Current Boundaries (honest statement)

- Single-machine, single-process model: the EventBus is already horizontally scalable via RabbitMQ; multi-replica worker deployment is a future direction
- Multi-user isolation is a single-user model (endpoints are auth-protected; session-level multi-user binding is planned)
- docker-compose full stack provided (with healthcheck-gated startup), awaiting final verification on a real machine
- The Memory Ark scenario is a proof of concept: fictional persona demos, no real patient data, no medical compliance — see `memory-ark.md`
