"""LongMemEval runner (Phase 7) — open-ant 记忆管线跑公开 benchmark。

LongMemEval（ICLR 2025, xiaowu0162/LongMemEval, MIT）：500 道 QA 考长期
交互记忆，分 6 题型（single-session-user / single-session-assistant /
single-session-preference / multi-session / temporal-reasoning /
knowledge-update）+ 30 道 abstention。每题自带 40+ 会话的 haystack
（LongMemEval_S，总 ~115k tokens），答案只能来自对历史"记得住"的信息。

模式（同一套 500 题做消融，报告横向对比）：

  baseline  无记忆地板：只给问题和日期（衡量"题本身好不好答"）
  oracle    上限：把 evidence 会话原文注入（官方 oracle context 口径）
  memory    完整生产记忆管线：逐实例提取→语义去重+图仲裁→向量+图双写
            → 检索（hybrid+图扩展）→ 注入作答（本项目的核心数字）
  chunks    消融：不提取，会话原文切块直入向量库 → 检索作答
            （隔离"LLM 提取/仲裁"的价值 vs 纯 chunk 检索）

隔离（不污染用户生产记忆）：
  * Qdrant：专用集合 ``ant_memory_lmeval``（QDRANT_COLLECTION 环境变量
    覆盖，跑前重建），每实例 payload ``session_id=lmeval-<idx>`` 作为
    where 过滤（session_id 有 KEYWORD 索引，云上可 filter）。
  * Neo4j（--graph on）：实体名加 ``lmeval-<idx>::`` 前缀做实例级命名
    空间；节点 source=longmemeval；跑完用 evals/cleanup_longmemeval_graph.py
    清理（不清理会留在共享图里）。

已知诚实边界（写进报告）：
  * 提取 prompt 只取用户消息（生产策略）——single-session-assistant
    类题目的证据在助手侧，预期显著偏低；--extract-assistant 可跑对照。
  * 批量提取时时间戳取批内最大会话日期（批内先后近似）。
  * judge 默认用 workspace 配置的模型（官方用 gpt-4o；自评偏差在
    报告中披露，可 --judge-model 换更强的模型复评）。

用法（repo ``src/`` 目录下）：

    python -m evals.run_longmemeval_eval --mode baseline --n 60
    python -m evals.run_longmemeval_eval --mode memory --n 60 --graph off
    python -m evals.run_longmemeval_eval --mode oracle --n 60
    python -m evals.run_longmemeval_eval --mode chunks --n 60

输出：``<out-dir>/<mode>/hypotheses.jsonl``（官方契约 question_id +
hypothesis；--resume 跳过已完成）；judge 用
``python -m evals.longmemeval_judge --hyp <file> --ref <s_cleaned>``。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import random
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("evals.longmemeval")

ROOT = Path(__file__).resolve().parents[2]  # open-ant/ 根（repo 是 src/）
DEFAULT_WS = ROOT / "workspace"
DEFAULT_DATA = (
    DEFAULT_WS / "evals" / "longmemeval" / "LongMemEval" / "data"
    / "longmemeval_s_cleaned.json"
)
DEFAULT_OUT = DEFAULT_WS / "evals" / "longmemeval" / "out"
EVAL_COLLECTION = "ant_memory_lmeval"

ANSWER_INSTRUCTION = (
    "Answer the user's question. Use the memory context below if it contains "
    "relevant information. If the context does not contain enough information "
    "to answer the question, reply exactly: I don't know."
)


# ── 纯函数（可单测） ────────────────────────────────────────────────────────


def normalize_ts(raw: str) -> str:
    """"2023/05/30 (Tue) 23:40" → "2023-05-30T23:40"（字典序=时间序）。

    解析失败时原样返回（图冲突检测按字符串比较 updated_at，需可排序）。
    """
    try:
        date_part, _, rest = raw.partition(" (")
        time_part = rest.partition(") ")[2].strip() or rest.strip()
        d = datetime.strptime(date_part.strip(), "%Y/%m/%d")
        return f"{d.date().isoformat()}T{time_part}"
    except (ValueError, AttributeError):
        return raw


def instance_filter(idx: int) -> dict:
    """Per-instance payload filter（session_id 有 KEYWORD 索引）。"""
    return {"session_id": f"lmeval-{idx}"}


def answer_prompt(
    question: str, question_date: str, context_block: str
) -> str:
    """QA prompt：日期 + 检索上下文 + 弃答纪律（abstention 友好）。"""
    ctx = context_block.strip() or "(no retrieved memory)"
    return (
        f"Today is {question_date}.\n\n"
        f"{ANSWER_INSTRUCTION}\n\n"
        f"Memory context:\n{ctx}\n\n"
        f"Question: {question}"
    )


def sample_instances(data: list[dict], n: int, seed: int) -> list[dict]:
    """按题型分层抽样 n 题（每类至少 1 题，seed 决定，可复现）。

    n >= len(data) 时返回全量；n 小于题型数时退化为普通随机抽样。
    """
    if n >= len(data):
        return list(data)
    rng = random.Random(seed)
    by_type: dict[str, list[dict]] = {}
    for e in data:
        by_type.setdefault(e["question_type"], []).append(e)
    if n < len(by_type):
        return rng.sample(data, n)

    picked: list[dict] = []
    # 每类至少 1 题
    for entries in by_type.values():
        picked.append(rng.choice(entries))
    # 剩余名额按题型占比分配
    remaining = n - len(picked)
    quotas = {
        t: int(remaining * len(v) / len(data)) for t, v in by_type.items()
    }
    for t, quota in quotas.items():
        pool = [e for e in by_type[t] if e not in picked]
        picked.extend(rng.sample(pool, min(quota, len(pool))))
    # 配额舍入不足则从全局随机补足
    if len(picked) < n:
        rest = [e for e in data if e not in picked]
        picked.extend(rng.sample(rest, n - len(picked)))
    return picked


def evidence_text(inst: dict) -> str:
    """oracle 模式：evidence 会话原文拼接（answer_session_ids 指向的会话）。"""
    by_id = {
        sid: sess for sid, sess in zip(inst["haystack_session_ids"],
                                       inst["haystack_sessions"])
    }
    parts = []
    for sid in inst["answer_session_ids"]:
        sess = by_id.get(sid)
        if sess is None:
            continue
        for turn in sess:
            role = turn.get("role", "unknown")
            parts.append(f"{role}: {turn.get('content', '')}")
    return "\n".join(parts)


def load_done_ids(out_path: Path) -> set[str]:
    """Resume 支持：已产出的 question_id 集合。"""
    if not out_path.exists():
        return set()
    done: set[str] = set()
    for line in out_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            done.add(json.loads(line)["question_id"])
        except (json.JSONDecodeError, KeyError):
            continue
    return done


def _turn_messages(session: list[dict]) -> list[dict]:
    """会话 turns → {role, content}（丢弃 has_answer 等标注字段）。"""
    return [
        {"role": t.get("role", "user"), "content": t.get("content", "")}
        for t in session
    ]


def _batch_max_ts(dates: list[str]) -> str:
    """批量提取的时间戳：批内最大会话日期（ISO，字典序=时间序）。"""
    normalized = [normalize_ts(d) for d in dates]
    return max(normalized) if normalized else datetime.now().isoformat()


# ── 模式实现 ────────────────────────────────────────────────────────────────


async def _answer_baseline(llm, inst: dict) -> str:
    prompt = (
        f"Today is {inst['question_date']}.\n\n"
        f"{ANSWER_INSTRUCTION}\n\nMemory context:\n(no retrieved memory)\n\n"
        f"Question: {inst['question']}"
    )
    response, _, _ = await llm.chat(
        [{"role": "user", "content": prompt}], [], temperature=0.0, max_tokens=512
    )
    return response or ""


async def _answer_oracle(llm, inst: dict) -> str:
    prompt = (
        f"Today is {inst['question_date']}.\n\n"
        f"{ANSWER_INSTRUCTION}\n\nMemory context:\n{evidence_text(inst)}\n\n"
        f"Question: {inst['question']}"
    )
    response, _, _ = await llm.chat(
        [{"role": "user", "content": prompt}], [], temperature=0.0, max_tokens=512
    )
    return response or ""


async def _ingest_instance(
    ctx, inst: dict, idx: int, batch_size: int
) -> int:
    """逐批提取（guard：约束 JSON + 语义去重 + 图仲裁）→ 向量 + 图双写。

    Returns: 入库的记忆条数。
    """
    guard = ctx.memory_guard
    vector_store = ctx.vector_store
    graph = getattr(ctx, "graph", None)
    where = instance_filter(idx)
    total = 0

    sessions = inst["haystack_sessions"]
    dates = inst["haystack_dates"]
    for start in range(0, len(sessions), batch_size):
        batch = sessions[start : start + batch_size]
        batch_dates = dates[start : start + batch_size]
        messages = [m for sess in batch for m in _turn_messages(sess)]
        if not messages:
            continue
        memories = await guard.extract_memories(messages, where=where)
        if not memories:
            continue
        ts = _batch_max_ts(batch_dates)
        for mem in memories:
            memory_id = mem.get("memory_id") or hashlib.sha256(
                mem["content"].encode("utf-8")
            ).hexdigest()[:24]
            meta = {
                "category": mem.get("category", "fact"),
                "importance": mem.get("importance", 5),
                "keywords": ",".join(mem.get("keywords", [])),
                "session_id": f"lmeval-{idx}",
                "created_at": ts,
                "updated_at": ts,
                "source": "longmemeval",
            }
            await vector_store.add(
                documents=[mem["content"]], metadatas=[meta], ids=[memory_id]
            )
            if graph is not None:
                # 实体名加实例前缀做命名空间隔离（共享 Neo4j 上的多用户互不串扰）
                namespaced = [
                    {"name": f"lmeval-{idx}::{e['name']}", "type": e.get("type", "fact")}
                    for e in mem.get("entities", [])
                    if e.get("name")
                ]
                try:
                    await graph.ingest(
                        {
                            "memory_id": memory_id,
                            "content": mem["content"],
                            "category": meta["category"],
                            "importance": meta["importance"],
                            "created_at": ts,
                            "updated_at": ts,
                            "source": "longmemeval",
                            "session_id": f"lmeval-{idx}",
                            "entities": namespaced,
                        }
                    )
                except Exception as exc:  # noqa: BLE001 — 图失败降级（原则 11）
                    logger.warning(
                        "lmeval graph ingest failed (degraded): %s", type(exc).__name__
                    )
            total += 1
    return total


async def _answer_memory(ctx, llm, inst: dict, idx: int) -> str:
    retriever = ctx.memory_retriever
    docs = await retriever.retrieve(
        inst["question"], top_k=ctx.config.memory.top_k, where=instance_filter(idx)
    )
    block = retriever.format_for_prompt(docs)
    prompt = answer_prompt(inst["question"], inst["question_date"], block)
    response, _, _ = await llm.chat(
        [{"role": "user", "content": prompt}], [], temperature=0.0, max_tokens=512
    )
    return response or ""


async def _index_chunks(ctx, inst: dict, idx: int) -> int:
    """chunks 消融：会话原文切块直入向量库（不做 LLM 提取/仲裁）。"""
    vector_store = ctx.vector_store
    total = 0
    for sid, sess, date in zip(
        inst["haystack_session_ids"], inst["haystack_sessions"], inst["haystack_dates"]
    ):
        ts = normalize_ts(date)
        for turn in sess:
            content = (turn.get("content") or "").strip()
            if not content:
                continue
            chunk_id = hashlib.sha256(
                f"{sid}:{content}".encode("utf-8")
            ).hexdigest()[:24]
            await vector_store.add(
                documents=[content],
                metadatas=[
                    {
                        "category": "chunk",
                        "importance": 5,
                        "keywords": "",
                        "session_id": f"lmeval-{idx}",
                        "created_at": ts,
                        "updated_at": ts,
                        "source": "longmemeval-chunk",
                    }
                ],
                ids=[chunk_id],
            )
            total += 1
    return total


# ── 主流程 ──────────────────────────────────────────────────────────────────


def _build_llm(config):
    from ant.provider.llm.base import LLMProvider

    return LLMProvider.from_config(config.llm)


async def _wipe_eval_collection() -> None:
    """重建专用 Qdrant 集合（幂等；忽略不存在）。"""
    from ant.utils.settings import InfraSettings

    infra = InfraSettings()
    if not (infra.qdrant_url() and infra.qdrant_api_key()):
        logger.warning("Qdrant 凭据缺失 — 跳过集合重建（后续 add 会报错）")
        return
    from qdrant_client import AsyncQdrantClient

    client = AsyncQdrantClient(
        url=infra.qdrant_url(), api_key=infra.qdrant_api_key(),
        timeout=infra.qdrant_timeout,
    )
    try:
        await client.delete_collection(EVAL_COLLECTION)
        logger.info("已删除旧评测集合 %s", EVAL_COLLECTION)
    except Exception:  # noqa: BLE001 — not found is fine
        pass
    await client.close()


def _build_context(workspace: Path, graph_on: bool):
    from ant.core.context import SharedContext
    from ant.utils.config import Config

    os.environ["QDRANT_COLLECTION"] = EVAL_COLLECTION  # 必须早于 Context 构造
    config = Config.load(workspace)
    ctx = SharedContext(config)
    if not graph_on:
        ctx.graph = None  # 关图：仲裁/扩展全部跳过（消融对照）
    return ctx, config


async def _run_mode(args, mode: str) -> int:
    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    instances = sample_instances(data, args.n, args.seed)
    out_dir = Path(args.out_dir) / mode
    out_dir.mkdir(parents=True, exist_ok=True)
    hyp_path = out_dir / "hypotheses.jsonl"
    # --resume 才续跑；全新运行覆盖旧 hypotheses（与集合重建语义一致）
    done = load_done_ids(hyp_path) if args.resume else set()
    todo = [inst for inst in instances if inst["question_id"] not in done]
    logger.info(
        "mode=%s: %d 题（全量 %d），跳过已完成 %d，剩余 %d",
        mode, len(instances), len(data), len(done), len(todo),
    )
    if not todo:
        logger.info("无待跑题目（--resume 已全部完成）")
        return 0

    if args.extract_assistant:
        from ant.memory import extraction as ex

        ex.EXTRACTION_PROMPT = ex.EXTRACTION_PROMPT.replace(
            "Only extract information from the **USER's** messages. Ignore all "
            "assistant (AI) responses, as they often contain information "
            "already stored in documents or general knowledge.",
            "Extract information from BOTH the user's and the assistant's "
            "messages.",
        )
        logger.info("提取口径: 用户+助手消息（对照实验）")

    ctx, config = _build_context(Path(args.workspace), graph_on=args.graph == "on")
    llm = _build_llm(config)
    logger.info(
        "模型: %s / vector_backend=%s / graph=%s",
        config.llm.model,
        getattr(config.memory, "vector_backend", "?"),
        args.graph,
    )
    if mode in ("memory", "chunks") and ctx.vector_store is not None:
        # 预热 client + 建集合，避免 6 个并发实例首写时竞争创建集合
        await ctx.vector_store._client_async()

    sem = asyncio.Semaphore(args.concurrency)
    out_file = open(hyp_path, "a" if args.resume else "w", encoding="utf-8")
    stats = {"memories": 0, "chunks": 0}

    async def _one(idx: int, inst: dict):
        async with sem:
            hypothesis = ""
            extra = ""
            try:
                if mode == "baseline":
                    hypothesis = await _answer_baseline(llm, inst)
                elif mode == "oracle":
                    hypothesis = await _answer_oracle(llm, inst)
                elif mode == "memory":
                    n_mem = await _ingest_instance(ctx, inst, idx, args.batch_size)
                    stats["memories"] += n_mem
                    hypothesis = await _answer_memory(ctx, llm, inst, idx)
                    extra = f"memories={n_mem}"
                elif mode == "chunks":
                    n_ch = await _index_chunks(ctx, inst, idx)
                    stats["chunks"] += n_ch
                    hypothesis = await _answer_memory(ctx, llm, inst, idx)
                    extra = f"chunks={n_ch}"
                else:
                    raise ValueError(f"unknown mode {mode!r}")
            except Exception as exc:  # noqa: BLE001 — 单题失败不连坐整批
                logger.error("instance %s failed: %s", inst["question_id"], exc)
                hypothesis = ""
                extra = f"ERROR: {type(exc).__name__}"
            return {
                "question_id": inst["question_id"],
                "hypothesis": hypothesis,
                "mode": mode,
                "detail": extra,
            }

    results = []
    for coro in asyncio.as_completed(
        [_one(i, inst) for i, inst in enumerate(todo)]
    ):
        results.append(await coro)

    for entry in results:
        out_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
    out_file.flush()
    out_file.close()
    logger.info(
        "mode=%s 完成：写入 %d 条 → %s（记忆 %d / 块 %d）",
        mode, len(results), hyp_path, stats["memories"], stats["chunks"],
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LongMemEval 评测 runner（Phase 7）")
    parser.add_argument("--mode", default="memory",
                        choices=["baseline", "oracle", "memory", "chunks"])
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    parser.add_argument("--workspace", default=str(DEFAULT_WS))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--n", type=int, default=500, help="题数（<=500；分层抽样）")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--graph", default="off", choices=["on", "off"],
                        help="memory 模式是否启用 Neo4j 图（仲裁+扩展）")
    parser.add_argument("--batch-size", type=int, default=5,
                        help="每批提取的会话数（时间戳取批内最大日期）")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--extract-assistant", action="store_true",
                        help="对照实验：提取用户+助手消息（默认仅用户，生产口径）")
    parser.add_argument("--resume", action="store_true",
                        help="跳过 hypotheses.jsonl 中已完成的 question_id")
    args = parser.parse_args(argv)

    # memory/chunks 需要向量库：全新运行时重建专用集合。
    # --resume 时保留集合（已入库实例的记忆是续跑的前提）。
    if args.mode in ("memory", "chunks") and not args.resume:
        asyncio.run(_wipe_eval_collection())
    return asyncio.run(_run_mode(args, args.mode))


if __name__ == "__main__":
    sys.exit(main())
