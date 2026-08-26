"""Phase 2 llm-layer tests: litellm Router provider, usage recorder,
dynamic token threshold, context-guard compaction fallback.

No real LLM network traffic anywhere: ``ant.provider.llm.base.Router`` is
monkeypatched with a fake, ``litellm.get_model_info`` is stubbed, and the
UsageRecorder runs against SQLite+aiosqlite.
"""

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

# 根 pyproject.toml 的 pytest pythonpath=["."] 已覆盖 src 导入；
# 此兜底与既有测试文件保持一致，保证任意根目录下可跑。
_SRC = Path(__file__).resolve().parents[2]  # src/ant/tests -> src
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import litellm  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from ant.core.agent import Agent  # noqa: E402
from ant.core.context_guard import ContextGuard  # noqa: E402
from ant.provider.llm import LLMProvider  # noqa: E402
from ant.provider.llm.usage import UsageRecorder  # noqa: E402
from ant.storage.models import Base, UsageRecord  # noqa: E402
from ant.utils.config import Config, LLMConfig, PipelineConfig, ToolConfig  # noqa: E402

# ---------------------------------------------------------------------------
# Fakes: litellm Router / streaming response / chat response
# ---------------------------------------------------------------------------

class FakeRouter:
    """Minimal litellm.Router stand-in; records init kwargs and calls.

    ``response`` is returned by ``acompletion`` unless ``acompletion_impl``
    is set (then it is awaited with the request kwargs).
    """

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.calls: list = []
        self.response = None
        self.acompletion_impl = None

    def _capture(self, kw):
        """记录被 patch 后 Router 调用时收到的真实构造参数。"""
        self.init_kwargs = kw
        return self

    async def acompletion(self, **kwargs):
        self.calls.append(kwargs)
        if self.acompletion_impl is not None:
            return await self.acompletion_impl(**kwargs)
        return self.response


class FakeStream:
    """Awaitable + async-iterable stand-in for a litellm streaming response."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __await__(self):
        async def _resolve():
            return self

        return _resolve().__await__()

    def __aiter__(self):
        async def _iter():
            for chunk in self._chunks:
                yield chunk

        return _iter()


def _delta(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _choice(delta, finish_reason=None):
    return SimpleNamespace(delta=delta, finish_reason=finish_reason)


def _chunk(delta, finish_reason=None, usage=None, hidden=None):
    return SimpleNamespace(
        choices=[_choice(delta, finish_reason)],
        usage=usage,
        _hidden_params=hidden or {},
    )


def _tc_delta(index, id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index,
        id=id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _message(content="", tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _chat_response(message, finish_reason="stop", usage=None, hidden=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=usage,
        _hidden_params=hidden or {},
    )


def _patch_router(monkeypatch, fake):
    # 用 lambda 直连 fake 会把 Router(**router_kwargs) 的 kwargs 丢掉，
    # 导致 init_kwargs 永远为空——通过 _capture 把真实构造参数记录进 fake。
    monkeypatch.setattr(
        "ant.provider.llm.base.Router",
        lambda **kw: fake._capture(kw),
    )
    return fake


# ---------------------------------------------------------------------------
# stream_chat event protocol
# ---------------------------------------------------------------------------

async def test_stream_chat_event_order_token_tool_usage_done(monkeypatch) -> None:
    """token → tool_calls → usage → done, in order; usage carries cost."""
    chunks = [
        _chunk(_delta(content="Hello")),
        _chunk(_delta(content=" world")),
        _chunk(
            _delta(
                tool_calls=[
                    _tc_delta(0, id="call_1", name="read_file", arguments='{"path": "x"}')
                ]
            )
        ),
        _chunk(
            _delta(),
            finish_reason="tool_calls",
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
            hidden={"response_cost": 0.42},
        ),
    ]
    fake = _patch_router(monkeypatch, FakeRouter())
    fake.response = FakeStream(chunks)

    provider = LLMProvider(model="fake-model", api_key="sk-test")
    events = [
        ev async for ev in provider.stream_chat([{"role": "user", "content": "hi"}])
    ]

    assert [ev["type"] for ev in events] == ["token", "token", "tool_calls", "usage", "done"]
    assert events[0]["data"] == "Hello"
    assert events[1]["data"] == " world"

    tc = events[2]["data"][0]
    assert tc.id == "call_1"
    assert tc.name == "read_file"
    assert tc.arguments == '{"path": "x"}'

    usage = events[3]["data"]
    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 20
    assert usage["model"] == "fake-model"
    assert usage["cost"] == 0.42

    assert events[4]["finish_reason"] == "tool_calls"


async def test_stream_chat_omits_usage_when_provider_sends_none(monkeypatch) -> None:
    """Backward compat: no usage chunk → no usage event, only token/done."""
    fake = _patch_router(monkeypatch, FakeRouter())
    fake.response = FakeStream([_chunk(_delta(content="hi"))])

    provider = LLMProvider(model="fake-model", api_key="sk-test")
    events = [
        ev async for ev in provider.stream_chat([{"role": "user", "content": "hi"}])
    ]
    assert [ev["type"] for ev in events] == ["token", "done"]
    assert events[0]["data"] == "hi"


async def test_stream_chat_error_event_then_generator_ends_normally(monkeypatch) -> None:
    """except branch yields an error event and RETURNS (no re-raise)."""
    fake = _patch_router(monkeypatch, FakeRouter())

    async def _boom(**kwargs):
        raise RuntimeError("provider exploded")

    fake.acompletion_impl = _boom

    provider = LLMProvider(model="fake-model", api_key="sk-test")
    events = [
        ev async for ev in provider.stream_chat([{"role": "user", "content": "hi"}])
    ]
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "provider exploded" in events[0]["data"]


async def test_stream_chat_accumulates_multi_chunk_tool_call(monkeypatch) -> None:
    """Tool-call arguments split across chunks are accumulated correctly."""
    chunks = [
        _chunk(_delta(tool_calls=[_tc_delta(0, id="c1", name="edit_file")])),
        _chunk(_delta(tool_calls=[_tc_delta(0, arguments='{"path"')])),
        _chunk(_delta(tool_calls=[_tc_delta(0, arguments=': "f"}' )])),
        _chunk(_delta(), finish_reason="tool_calls"),
    ]
    fake = _patch_router(monkeypatch, FakeRouter())
    fake.response = FakeStream(chunks)

    provider = LLMProvider(model="fake-model", api_key="sk-test")
    events = [
        ev async for ev in provider.stream_chat([{"role": "user", "content": "hi"}])
    ]
    tc_event = next(ev for ev in events if ev["type"] == "tool_calls")
    tc = tc_event["data"][0]
    assert tc.id == "c1"
    assert tc.name == "edit_file"
    assert tc.arguments == '{"path": "f"}'


# ---------------------------------------------------------------------------
# chat() — tool_calls .get tolerance + usage_callback
# ---------------------------------------------------------------------------

async def test_chat_tool_call_missing_id_does_not_keyerror(monkeypatch) -> None:
    """message.tool_calls element without an ``id`` → empty id, no KeyError."""
    fake = _patch_router(monkeypatch, FakeRouter())
    fake.response = _chat_response(
        _message(
            tool_calls=[{"function": {"name": "read_file", "arguments": "{}"}}]
        ),
        finish_reason="tool_calls",
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3),
        hidden={"response_cost": 0.1},
    )

    provider = LLMProvider(model="fake-model", api_key="sk-test")
    content, tool_calls, stop = await provider.chat(
        [{"role": "user", "content": "hi"}]
    )
    assert content == ""
    assert len(tool_calls) == 1
    assert tool_calls[0].id == ""  # .get("id", "") — 不 KeyError
    assert tool_calls[0].name == "read_file"
    assert stop == "tool_calls"


async def test_chat_invokes_usage_callback(monkeypatch) -> None:
    """Non-streaming usage is forwarded to the injected usage_callback."""
    recorded: list = []

    async def cb(data):
        recorded.append(data)

    fake = _patch_router(monkeypatch, FakeRouter())
    fake.response = _chat_response(
        _message("ok"),
        usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2),
        hidden={"response_cost": 0.05},
    )

    provider = LLMProvider(model="fake-model", api_key="sk-test", usage_callback=cb)
    await provider.chat([{"role": "user", "content": "hi"}])

    assert len(recorded) == 1
    assert recorded[0]["prompt_tokens"] == 7
    assert recorded[0]["completion_tokens"] == 2
    assert recorded[0]["model"] == "fake-model"
    assert recorded[0]["cost"] == 0.05


async def test_chat_usage_callback_failure_does_not_break_chat(monkeypatch) -> None:
    """A broken usage_callback never propagates into the chat result."""

    async def _broken(data):
        raise RuntimeError("recorder down")

    fake = _patch_router(monkeypatch, FakeRouter())
    fake.response = _chat_response(
        _message("ok"),
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        hidden={"response_cost": 0.01},
    )

    provider = LLMProvider(model="fake-model", api_key="sk-test", usage_callback=_broken)
    content, _, _ = await provider.chat([{"role": "user", "content": "hi"}])
    assert content == "ok"


# ---------------------------------------------------------------------------
# Router wiring + from_config
# ---------------------------------------------------------------------------

def test_provider_builds_router_with_resilience_kwargs(monkeypatch) -> None:
    """fallbacks / num_retries / timeout reach the litellm Router."""
    captured: dict = {}

    class CaptureRouter:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def acompletion(self, **kwargs):
            raise AssertionError("not called")

    monkeypatch.setattr("ant.provider.llm.base.Router", CaptureRouter)

    LLMProvider(
        model="m1", api_key="sk-test", num_retries=3, timeout=30.0,
        fallbacks=["m2", "m3"],
    )

    assert captured["num_retries"] == 3
    assert captured["timeout"] == 30.0
    assert captured["fallbacks"] == [{"m1": ["m2", "m3"]}]
    assert captured["model_list"][0]["model_name"] == "m1"
    assert captured["model_list"][0]["litellm_params"]["model"] == "m1"
    # retry_policy is best-effort across litellm versions: present when the
    # installed version accepts either construction form, absent when both
    # raise — never an error.  Only assert that construction succeeded.


def test_provider_no_fallbacks_omits_fallback_key(monkeypatch) -> None:
    captured: dict = {}

    class CaptureRouter:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def acompletion(self, **kwargs):
            raise AssertionError("not called")

    monkeypatch.setattr("ant.provider.llm.base.Router", CaptureRouter)
    LLMProvider(model="m1", api_key="sk-test")
    assert "fallbacks" not in captured


def test_from_config_reads_new_fields(monkeypatch) -> None:
    monkeypatch.setattr("ant.provider.llm.base.Router", FakeRouter)
    cfg = LLMConfig(
        provider="fake",
        model="m",
        api_key="sk-test",
        timeout=55.0,
        num_retries=4,
        fallbacks=["f1", "f2"],
        max_tokens=8192,
        summarize_model="small-summarizer",
    )
    provider = LLMProvider.from_config(cfg)
    assert provider.timeout == 55.0
    assert provider.num_retries == 4
    assert provider.fallbacks == ["f1", "f2"]
    assert provider.max_tokens == 8192
    assert provider.summarize_model == "small-summarizer"


# ---------------------------------------------------------------------------
# Config defaults (contract for parallel agents)
# ---------------------------------------------------------------------------

def test_llm_config_new_field_defaults() -> None:
    cfg = LLMConfig(provider="fake", model="m", api_key="sk-test")
    assert cfg.max_tokens == 4096
    assert cfg.timeout == 120.0
    assert cfg.num_retries == 2
    assert cfg.fallbacks == []
    assert cfg.summarize_model is None


def test_tool_and_pipeline_config_defaults() -> None:
    tools = ToolConfig()
    assert tools.default_timeout == 120
    assert tools.parallel_writes is False

    pipeline = PipelineConfig()
    assert pipeline.max_iterations == 10
    assert pipeline.max_parallel_tools == 8

    cfg = Config(
        workspace=Path("."),
        llm=LLMConfig(provider="fake", model="m", api_key="sk-test"),
        default_agent="a",
    )
    assert cfg.tools.default_timeout == 120
    assert cfg.pipeline.max_iterations == 10
    assert cfg.pipeline.max_parallel_tools == 8


# ---------------------------------------------------------------------------
# UsageRecorder
# ---------------------------------------------------------------------------

async def test_usage_recorder_writes_row() -> None:
    """record_usage inserts a row into usage_records (SQLite+aiosqlite)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        recorder = UsageRecorder(session_factory=factory)
        await recorder.record_usage(
            session_id="s-1", model="model-x",
            prompt_tokens=10, completion_tokens=5, cost=0.03,
        )

        async with factory() as session:
            rows = (await session.execute(select(UsageRecord))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.session_id == "s-1"
        assert row.model == "model-x"
        assert row.input_tokens == 10
        assert row.output_tokens == 5
        assert row.cost == 0.03
        assert row.created_at is not None
    finally:
        await engine.dispose()


async def test_usage_recorder_noop_without_factory(caplog) -> None:
    """session_factory=None (jsonl backend) → warning, no exception."""
    recorder = UsageRecorder(session_factory=None)
    with caplog.at_level(logging.WARNING, logger="ant.provider.llm.usage"):
        await recorder.record_usage(
            session_id="s", model="m", prompt_tokens=1, completion_tokens=1, cost=0.1,
        )
    assert any("no session factory" in r.message for r in caplog.records)


async def test_usage_recorder_write_failure_only_warns(caplog) -> None:
    """A failing session factory never raises out of record_usage."""

    class BrokenFactory:
        def __call__(self):
            raise RuntimeError("db down")

    recorder = UsageRecorder(session_factory=BrokenFactory())
    with caplog.at_level(logging.WARNING, logger="ant.provider.llm.usage"):
        await recorder.record_usage(
            session_id="s", model="m", prompt_tokens=1, completion_tokens=1, cost=0.1,
        )
    assert any("usage recording failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _get_token_threshold
# ---------------------------------------------------------------------------

class _FakeAgentDef:
    def __init__(self, llm):
        self.llm = llm


def _make_agent(monkeypatch, model: str) -> Agent:
    monkeypatch.setattr("ant.provider.llm.base.Router", FakeRouter)
    llm_cfg = LLMConfig(provider="fake", model=model, api_key="sk-test")
    return Agent(_FakeAgentDef(llm_cfg), context=object())


def test_token_threshold_dynamic_from_model_info(monkeypatch) -> None:
    """80% of max_input_tokens when the model is in litellm's registry."""
    monkeypatch.setattr(
        litellm, "get_model_info", lambda model: {"max_input_tokens": 128000}
    )
    agent = _make_agent(monkeypatch, "gpt-128k")
    assert agent._get_token_threshold() == 102400


def test_token_threshold_unknown_model_falls_back(caplog, monkeypatch) -> None:
    """get_model_info raising (custom model name) → 160000 + warning."""
    def _unknown(model):
        raise KeyError(f"unknown model: {model}")

    monkeypatch.setattr(litellm, "get_model_info", _unknown)
    agent = _make_agent(monkeypatch, "custom-local-model")
    with caplog.at_level(logging.WARNING, logger="ant.core.agent"):
        assert agent._get_token_threshold() == 160000
    assert any("falling back to threshold 160000" in r.message for r in caplog.records)


def test_token_threshold_missing_registry_entry_falls_back(caplog, monkeypatch) -> None:
    """Registry entry without max_input_tokens → 160000 + warning."""
    monkeypatch.setattr(
        litellm, "get_model_info", lambda model: {"max_output_tokens": 8000}
    )
    agent = _make_agent(monkeypatch, "no-input-limit-model")
    with caplog.at_level(logging.WARNING, logger="ant.core.agent"):
        assert agent._get_token_threshold() == 160000
    assert any("not in litellm model registry" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Context guard compaction fallback
# ---------------------------------------------------------------------------

class _FakeLLM:
    def __init__(self, response="summary text", error=None):
        self.response = response
        self.error = error
        self.chat_calls: list = []

    async def chat(self, messages, tools=None, **kwargs):
        self.chat_calls.append(messages)
        if self.error is not None:
            raise self.error
        return (self.response, [], "stop")


def _messages(n: int = 10) -> list:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
        for i in range(n)
    ]


def _make_guard(monkeypatch, summarize_model=None, llm=None):
    if llm is None:
        llm = _FakeLLM()
    state = SimpleNamespace(messages=_messages(10), agent=SimpleNamespace(llm=llm))
    cfg = SimpleNamespace(
        llm=LLMConfig(
            provider="fake", model="main-model", api_key="sk-test",
            summarize_model=summarize_model,
        )
    )
    guard = ContextGuard(
        shared_context=SimpleNamespace(config=cfg), token_threshold=1
    )
    if summarize_model:
        # the dedicated provider is a real LLMProvider — fake its Router
        _patch_router(monkeypatch, FakeRouter())
    return guard, state


async def test_compact_summary_success_path() -> None:
    """10 messages: 5 oldest summarized, summary + ack + 5 newest kept."""
    llm = _FakeLLM(response="the summary")
    guard, state = _make_guard(monkeypatch=None, llm=llm)

    result = await guard._compact_messages(state)
    assert result is state
    assert len(llm.chat_calls) == 1
    assert len(state.messages) == 7  # 1 summary + 1 ack + 5 remaining
    assert state.messages[0]["content"].startswith("[Previous conversation summary]")
    assert state.messages[1]["role"] == "assistant"
    assert state.messages[2]["content"] == "msg 5"
    assert state.messages[-1]["content"] == "msg 9"


async def test_compact_summary_failure_hard_truncates(caplog, monkeypatch) -> None:
    """LLM summary failure → keep newest 4 messages, no summary, warning."""
    llm = _FakeLLM(error=RuntimeError("summarizer down"))
    guard, state = _make_guard(monkeypatch=monkeypatch, llm=llm)

    with caplog.at_level(logging.WARNING, logger="ant.core.context_guard"):
        result = await guard._compact_messages(state)
    assert result is state
    assert state.messages == _messages(10)[-4:]
    assert any("hard truncation" in r.message for r in caplog.records)


async def test_compact_uses_dedicated_summarize_model(monkeypatch) -> None:
    """summarize_model set → main LLM untouched, small model does the job."""
    llm = _FakeLLM()  # main LLM — must NOT be called
    guard, state = _make_guard(
        monkeypatch=monkeypatch, summarize_model="small-model", llm=llm
    )
    # The summary LLM is constructed lazily inside _compact_messages —
    # patch the Router AFTER _make_guard so this fake is the one used.
    fake = _patch_router(monkeypatch, FakeRouter())
    fake.response = _chat_response(_message("small summary"))

    await guard._compact_messages(state)

    assert llm.chat_calls == []
    assert fake.calls  # dedicated provider was invoked
    params = fake.init_kwargs["model_list"][0]["litellm_params"]
    assert params["model"] == "small-model"
    assert state.messages[0]["content"].startswith("[Previous conversation summary]")
