"""Phase 4E 审计落库测试（workspace/plan.md Phase 4E governance）。

覆盖：
  (a) ToolGovernance.set_audit_sink：sink 被 fire-and-forget 调用；
      sink 抛异常不影响 record_call（原则 11：审计永不打断工具调用）；
  (b) args 脱敏：疑似密钥值（长度>20 且含 = 或 token 字样）在落库前替换为
      "[REDACTED]"（内存 _audit_log 保持原始值）；
  (c) jsonl 模式（无 session_factory）：governance 不设 sink，行为不变；
  (d) Agent._build_tools 在 session_factory 存在时注入 sink——用
      sqlite+aiosqlite（与 MySQL 同一套 SQLAlchemy 模型）建 audit_log 表，
      真写一行并断言脱敏 / 截断 / session_id 绑定。
"""

import asyncio
import logging
import sys
import time
import types
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# 根 pyproject.toml 的 pytest pythonpath=src 配置由并行代理负责，
# 目前尚未写入，这里临时把 src 加入 sys.path 以保证 `import ant.*` 可用。
_SRC = Path(__file__).resolve().parents[2]  # src/ant/tests -> src
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ant.core.agent import _looks_like_secret, _redact_args  # noqa: E402
from ant.storage.models import AuditLogRecord, Base  # noqa: E402
from ant.tools.base import FunctionTool  # noqa: E402
from ant.tools.policy import ToolGovernance, ToolPolicy  # noqa: E402

_SECRET = "sk-verylongsecretvalue-over-20-chars=abc"


# ── 假对象 ───────────────────────────────────────────────────────────────


def _make_agent(session_factory):
    """Agent 桩：跳过 __init__（不拉起 LLMProvider），只暴露 _build_tools
    所需字段。"""
    from ant.core.agent import Agent

    agent = Agent.__new__(Agent)
    agent.agent_def = types.SimpleNamespace(
        id="main-agent",
        tool_policy={"allowed_tools": None},  # truthy → 创建 governance
        allow_skills=False,
        llm=None,
    )
    agent.context = types.SimpleNamespace(
        _session_factory=session_factory,
        skill_loader=types.SimpleNamespace(discover_skills=lambda: []),
        config=types.SimpleNamespace(websearch=None, webread=None),
        doc_ingester=None,
        agent_loader=types.SimpleNamespace(discover_agents=lambda: []),
    )
    return agent


@pytest.fixture
async def sqlite_factory(tmp_path):
    """文件 SQLite + NullPool 建全量表（含 audit_log），返回 session 工厂。

    必须用文件库 + NullPool：内存库 + StaticPool 共享同一连接，轮询 session
    退出时的 ROLLBACK 会卷走 sink 未提交的事务（INSERT 已执行但被回滚，
    SELECT 永远空——真云验收时发现，与 test_outbox.py 同理）。
    """
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'audit_test.db'}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _wait_for_audit_row(factory, timeout: float = 2.0) -> AuditLogRecord | None:
    """轮询 audit_log 表直到 sink 的 fire-and-forget 任务落库。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with factory() as session:
            row = await session.scalar(select(AuditLogRecord))
        if row is not None:
            return row
        await asyncio.sleep(0.01)
    return None


# ── (a) sink 调用 + 异常吞噬 ─────────────────────────────────────────────


async def test_sink_called_with_entry_and_exception_swallowed() -> None:
    """sink 收到与内存审计一致的 entry；sink 抛异常不影响 record_call。"""
    governance = ToolGovernance(ToolPolicy())
    received: list[dict] = []

    async def bad_sink(entry: dict) -> None:
        received.append(entry)
        raise RuntimeError("sink boom")

    governance.set_audit_sink(bad_sink)

    # record_call 不得因 sink 抛异常而炸（原则 11）
    governance.record_call("bash", {"cmd": "ls"}, "ok", 0.5)

    for _ in range(200):
        if received:
            break
        await asyncio.sleep(0.01)
    assert len(received) == 1
    entry = received[0]
    assert entry is governance._audit_log[0]  # 与内存审计是同一个 entry 对象
    assert entry["tool"] == "bash"
    assert entry["args"] == {"cmd": "ls"}
    assert entry["result_preview"] == "ok"
    assert entry["elapsed"] == 0.5

    # 调用计数与内存审计不受 sink 影响
    summary = governance.get_audit_summary()
    assert summary["total_calls"] == 1
    assert summary["calls_by_tool"] == {"bash": 1}


async def test_sink_exception_is_logged_not_raised(caplog) -> None:
    """sink 任务异常被 done callback 吞掉并记 warning。"""
    governance = ToolGovernance()

    async def bad_sink(entry: dict) -> None:
        raise RuntimeError("sink boom")

    governance.set_audit_sink(bad_sink)

    with caplog.at_level(logging.WARNING, logger="ant.tools.policy"):
        governance.record_call("bash", {}, "ok", 0.1)
        for _ in range(200):
            if any("Audit sink" in r.getMessage() for r in caplog.records):
                break
            await asyncio.sleep(0.01)

    assert any("Audit sink" in r.getMessage() for r in caplog.records)
    assert governance.get_audit_summary()["total_calls"] == 1


async def test_sync_raising_sink_does_not_break_record_call() -> None:
    """sink 不是协程函数、同步抛异常时，record_call 同样不受影响。"""
    governance = ToolGovernance()

    def bad_sink(entry: dict) -> None:  # noqa: ARG001
        raise RuntimeError("sync sink boom")

    governance.set_audit_sink(bad_sink)  # type: ignore[arg-type]
    governance.record_call("bash", {}, "ok", 0.1)  # 不得 raise
    assert governance.get_audit_summary()["total_calls"] == 1


# ── (b) args 脱敏 ────────────────────────────────────────────────────────


def test_looks_like_secret_heuristic() -> None:
    """脱敏启发式：长度>20 且含 = 或 token 字样。"""
    assert _looks_like_secret(_SECRET)
    assert _looks_like_secret("x" * 21 + "TOKEN")
    assert _looks_like_secret("x" * 21 + "token")
    assert not _looks_like_secret("short=value")  # 短
    assert not _looks_like_secret("x" * 30)  # 长但没有 = / token
    assert not _looks_like_secret("token")  # 短


def test_redact_args_replaces_secret_values() -> None:
    """疑似密钥值替换为 [REDACTED]，其余原样保留。"""
    args = {
        "api_key": _SECRET,
        "password": "x" * 21 + "token",
        "query": "hello world",
        "count": 3,
        "nested": {"token": "x" * 21 + "token"},
    }
    redacted = _redact_args(args)
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["password"] == "[REDACTED]"
    assert redacted["query"] == "hello world"
    assert redacted["count"] == 3
    # 嵌套结构不递归脱敏（顶层启发式策略，注释说明）
    assert redacted["nested"] == {"token": "x" * 21 + "token"}


# ── (c) jsonl 模式：无 sink，行为不变 ───────────────────────────────────


async def test_governance_without_sink_behaviour_unchanged() -> None:
    """governance 无 sink 时 record_call 行为与改动前一致。"""
    governance = ToolGovernance(ToolPolicy())
    assert governance._audit_sink is None

    governance.record_call("bash", {"cmd": "ls"}, "ok", 0.1)
    await asyncio.sleep(0.05)  # 若误建 sink task，这里也不会抛异常

    summary = governance.get_audit_summary()
    assert summary["total_calls"] == 1
    assert summary["calls_by_tool"] == {"bash": 1}
    assert summary["recent_log"][0]["result_preview"] == "ok"


async def test_build_tools_jsonl_mode_sets_no_sink() -> None:
    """jsonl 模式（context._session_factory 为 None）：_build_tools 不设 sink。"""
    agent = _make_agent(None)
    tools = agent._build_tools(False, session_id="s1")
    assert tools._governance is not None
    assert tools._governance._audit_sink is None


# ── (d) mysql 模式：sink 注入 + 真写一行 ────────────────────────────────


async def test_build_tools_injects_sink_and_writes_audit_row(sqlite_factory) -> None:
    """session_factory 存在 → sink 注入；record_call 真写一行 audit_log。

    验证写入行的脱敏 / result_preview 200 截断 / session_id 绑定。
    """
    agent = _make_agent(sqlite_factory)
    tools = agent._build_tools(False, session_id="sess-1")
    governance = tools._governance
    assert governance is not None
    assert governance._audit_sink is not None

    governance.record_call(
        "bash",
        {"cmd": "ls -la", "api_key": _SECRET},
        "ok" * 300,
        0.5,
    )

    row = await _wait_for_audit_row(sqlite_factory)
    assert row is not None, "audit_log 应写入一行"
    assert row.session_id == "sess-1"
    assert row.event_type == "tool_call"
    payload = row.payload
    assert payload["tool"] == "bash"
    assert payload["args"]["cmd"] == "ls -la"
    assert payload["args"]["api_key"] == "[REDACTED]"
    assert payload["result_preview"] == ("ok" * 300)[:200]
    assert payload["elapsed"] == 0.5


async def test_tool_execution_persists_redacted_audit_row(sqlite_factory) -> None:
    """execute_tool 全链路：registry → governance → sink → audit_log 落库。"""
    agent = _make_agent(sqlite_factory)
    tools = agent._build_tools(False, session_id="sess-2")

    async def _probe(session, api_key: str = "") -> str:
        return "probe done"

    tools.register(
        FunctionTool(
            "audit_probe",
            "probe tool",
            {
                "type": "object",
                "properties": {"api_key": {"type": "string"}},
                "required": [],
            },
            _probe,
        )
    )

    result = await tools.execute_tool(
        "audit_probe",
        session=types.SimpleNamespace(),
        api_key=_SECRET,
    )
    assert result == "probe done"

    row = await _wait_for_audit_row(sqlite_factory)
    assert row is not None, "audit_log 应写入一行"
    assert row.session_id == "sess-2"
    assert row.payload["tool"] == "audit_probe"
    assert row.payload["args"]["api_key"] == "[REDACTED]"
