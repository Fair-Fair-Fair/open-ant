"""Storage layer unit tests (Phase 1 foundation).

Covers:
  (a) MysqlHistoryRepository core logic on an in-memory SQLite
      (aiosqlite) engine — same SQLAlchemy code path as MySQL.
  (b) JsonlHistoryRepository parity with the legacy JSONL backend
      (tmp_path), including on-disk format compatibility.
  (c) Schema compatibility: ant.core.history re-exports, from_message /
      to_message round trips, EventSource coercion.
  (d) InfraSettings credential discipline: masked/repr output never
      leaks passwords.
"""

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from ant.core.events import CliEventSource
from ant.storage.models import Base
from ant.storage.repository import JsonlHistoryRepository, MysqlHistoryRepository
from ant.storage.schemas import HistoryMessage, HistorySession

# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
async def mysql_repo():
    """MysqlHistoryRepository over an in-memory SQLite database."""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    repo = MysqlHistoryRepository("sqlite+aiosqlite://", engine=engine)
    yield repo
    await engine.dispose()


# ── (a) MysqlHistoryRepository core logic ───────────────────────────────


async def test_mysql_repo_round_trip(mysql_repo):
    await mysql_repo.create_session("a1", "s1", CliEventSource())
    await mysql_repo.save_message("s1", HistoryMessage(role="user", content="hello"))
    await mysql_repo.save_message("s1", HistoryMessage(role="assistant", content="hi back"))

    info = await mysql_repo.get_session_info("s1")
    assert info is not None
    assert info.agent_id == "a1"
    assert info.source == "platform-cli:cli-user"
    assert info.message_count == 2

    msgs = await mysql_repo.get_messages("s1")
    assert [m.content for m in msgs] == ["hello", "hi back"]

    sessions = await mysql_repo.list_sessions()
    assert [s.id for s in sessions] == ["s1"]


async def test_mysql_repo_save_unknown_session_raises(mysql_repo):
    with pytest.raises(ValueError):
        await mysql_repo.save_message("nope", HistoryMessage(role="user", content="x"))


async def test_mysql_repo_get_messages_unknown_session_empty(mysql_repo):
    assert await mysql_repo.get_messages("nope") == []
    assert await mysql_repo.get_session_info("nope") is None


async def test_mysql_repo_create_session_idempotent(mysql_repo):
    await mysql_repo.create_session("a1", "s1", CliEventSource())
    # duplicate create must not raise (resume race)
    await mysql_repo.create_session("a1", "s1", CliEventSource())
    info = await mysql_repo.get_session_info("s1")
    assert info is not None
    assert info.agent_id == "a1"


async def test_mysql_repo_title_auto_generated(mysql_repo):
    await mysql_repo.create_session("a1", "s1", CliEventSource())
    await mysql_repo.save_message("s1", HistoryMessage(role="user", content="t" * 60))
    info = await mysql_repo.get_session_info("s1")
    assert info.title == "t" * 50 + "..."
    # non-user first message must not set a title
    await mysql_repo.save_message("s1", HistoryMessage(role="assistant", content="reply"))
    info2 = await mysql_repo.get_session_info("s1")
    assert info2.title == "t" * 50 + "..."


async def test_mysql_repo_list_sessions_most_recent_first(mysql_repo):
    await mysql_repo.create_session("a1", "s1", CliEventSource())
    await mysql_repo.create_session("a2", "s2", CliEventSource())
    await mysql_repo.save_message("s1", HistoryMessage(role="user", content="newest"))
    sessions = await mysql_repo.list_sessions()
    assert sessions[0].id == "s1"
    assert sessions[1].id == "s2"


async def test_mysql_repo_tool_calls_json_round_trip(mysql_repo):
    await mysql_repo.create_session("a1", "s1", CliEventSource())
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "bash", "arguments": '{"cmd": "ls"}'},
        }
    ]
    await mysql_repo.save_message(
        "s1", HistoryMessage(role="assistant", content="", tool_calls=tool_calls)
    )
    await mysql_repo.save_message(
        "s1",
        HistoryMessage(role="tool", content="ok", tool_call_id="call_1"),
    )
    msgs = await mysql_repo.get_messages("s1")
    assert msgs[0].tool_calls == tool_calls
    assert msgs[1].tool_call_id == "call_1"


# ── (b) JsonlHistoryRepository parity + legacy format ───────────────────


async def test_jsonl_repo_round_trip(tmp_path: Path):
    repo = JsonlHistoryRepository(tmp_path)
    await repo.create_session("a1", "s1", CliEventSource())
    await repo.save_message("s1", HistoryMessage(role="user", content="hello"))
    await repo.save_message("s1", HistoryMessage(role="assistant", content="hi"))

    assert (await repo.get_session_info("s1")).message_count == 2
    msgs = await repo.get_messages("s1")
    assert [m.content for m in msgs] == ["hello", "hi"]
    assert [s.id for s in await repo.list_sessions()] == ["s1"]

    with pytest.raises(ValueError):
        await repo.save_message("nope", HistoryMessage(role="user", content="x"))


async def test_jsonl_repo_legacy_file_format_unchanged(tmp_path: Path):
    """JsonlHistoryRepository writes files the legacy HistoryStore reads."""
    repo = JsonlHistoryRepository(tmp_path)
    await repo.create_session("a1", "s1", CliEventSource())
    await repo.save_message("s1", HistoryMessage(role="user", content="legacy fmt"))

    from ant.core.history import HistoryStore

    store = HistoryStore(tmp_path)  # legacy synchronous reader
    assert store.list_sessions()[0].id == "s1"
    assert store.get_messages("s1")[0].content == "legacy fmt"
    assert store.get_session_info("s1").message_count == 1
    # index.jsonl + sessions/<id>.jsonl layout is preserved
    assert (tmp_path / "index.jsonl").exists()
    assert (tmp_path / "sessions" / "s1.jsonl").exists()


# ── (c) schema compatibility ────────────────────────────────────────────


def test_schemas_reexported_from_core_history():
    from ant.core import history as core_history
    from ant.storage import schemas as storage_schemas

    assert core_history.HistorySession is storage_schemas.HistorySession
    assert core_history.HistoryMessage is storage_schemas.HistoryMessage
    assert core_history.MAX_PERSISTED_TOOL_CHARS == 500


def test_history_message_from_message_round_trip():
    msg = HistoryMessage.from_message({"role": "user", "content": "hi"})
    assert msg.to_message() == {"role": "user", "content": "hi"}


def test_tool_result_truncated_on_persist():
    long_content = "x" * 600
    msg = HistoryMessage.from_message(
        {"role": "tool", "content": long_content, "tool_call_id": "tc1"}
    )
    assert len(msg.content) == 500 + 1  # 500 chars + ellipsis
    assert msg.content.endswith("…")
    assert msg.tool_call_id == "tc1"


def test_history_session_source_accepts_eventsource():
    sess = HistorySession(
        id="s1",
        agent_id="a1",
        source=CliEventSource(),
        created_at="t",
        updated_at="t",
    )
    assert sess.source == "platform-cli:cli-user"
    assert sess.get_source().platform_name == "cli"


# ── (d) InfraSettings credential discipline ─────────────────────────────


def test_infra_settings_masked_output_never_leaks_password():
    from ant.utils.settings import InfraSettings

    s = InfraSettings(
        mysql_username="root",
        mysql_password="sup3r-secret",
        mysql_host="127.0.0.1",
        mysql_port=3306,
        mysql_database="open_ant",
        rabbitmq_username="guest",
        rabbitmq_password="s3cret-rmq",
        redis_url="redis://:s3cret-redis@127.0.0.1:6379/0",
    )
    dsn = s.mysql_dsn()
    assert dsn is not None
    assert "sup3r-secret" in dsn  # the real DSN must carry the password
    assert "sup3r-secret" not in s.masked_mysql_dsn()
    assert "sup3r-secret" not in s.masked_mysql_server_dsn()
    assert "s3cret-rmq" not in s.masked_rabbitmq_url()
    assert "s3cret-redis" not in s.masked_redis_url()
    assert "sup3r-secret" not in repr(s)
    assert "s3cret-rmq" not in str(s)


def test_infra_settings_dsn_none_when_credentials_incomplete():
    from ant.utils.settings import InfraSettings

    s = InfraSettings(
        mysql_username=None,
        mysql_password=None,
        rabbitmq_username=None,
        rabbitmq_password=None,
    )
    assert s.mysql_dsn() is None
    assert s.mysql_server_dsn() is None
    assert s.rabbitmq_url() is None

    s2 = InfraSettings(mysql_username="root", mysql_password=None)
    assert s2.mysql_dsn() is None

    s3 = InfraSettings(rabbitmq_username="guest", rabbitmq_password=None)
    assert s3.rabbitmq_url() is None
