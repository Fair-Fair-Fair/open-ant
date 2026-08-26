"""Phase 2 — StreamPipeline 并行工具执行 / 超时 / 断流 / usage 记账 回归测试。

不依赖 LLM 网络：用假 LLM（yield 固定事件序列的 async gen）与假 SessionState
驱动 StreamPipeline，验证：

  - 只读工具并行执行（事件循环时间 + asyncio.Event 同步起始），结果按原顺序还原
  - 写类工具（write/edit/bash）先串行执行完、再并行只读组（记录执行顺序）
  - 单工具超时 → LLM 可感知的错误串，且不中断其余工具
  - 断流兜底：aclose() / task.cancel() → state 追加 assistant 占位消息
  - usage 事件不转发前端、只进 recorder（假 recorder 计数）
  - max_parallel_tools 并发上限生效（Semaphore）
  - parallel_writes=True 时写类并入并行组
  - 确认审批（require_confirmation）逐工具生效、被确认工具回退串行
"""

import asyncio
import types

import pytest

from ant.core.session_fsm import SessionFSM
from ant.core.stream_pipeline import PipelineContext, StreamPipeline
from ant.core.stream_stages import (
    StreamContextBuildStage,
    StreamLLMCallStage,
    StreamTerminalStage,
    StreamToolExecutionStage,
)
from ant.provider.llm.base import LLMToolCall

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeLLM:
    """每次 stream_chat 消费一个 turn 的假 LLM；turn 用尽后默认停止。"""

    def __init__(self, *turns: list[dict]):
        self._turns = list(turns)

    async def stream_chat(self, messages, tools):
        if self._turns:
            events = self._turns.pop(0)
        else:
            events = [{"type": "done", "finish_reason": "stop"}]
        for event in events:
            yield event


class _FakeState:
    def __init__(self):
        self.messages = []

    async def add_message(self, message):
        self.messages.append(message)

    def build_messages(self):
        return [{"role": "system", "content": "sys"}] + self.messages


class _FakeSession:
    def __init__(self, llm, tool_impls=None, policy=None, broker=None):
        self.agent = types.SimpleNamespace(
            llm=llm,
            agent_def=types.SimpleNamespace(id="a1"),
        )
        self.session_id = "s1"
        self.state = _FakeState()
        self.fsm = SessionFSM()
        self.tools = types.SimpleNamespace(
            _governance=(
                types.SimpleNamespace(policy=policy) if policy is not None else None
            )
        )
        self.shared_context = types.SimpleNamespace(
            guardrails=None,
            confirmation_broker=broker if broker is not None else _AutoApproveBroker(),
        )
        self._tool_impls = tool_impls or {}

    async def _execute_tool_call(self, tool_call):
        impl = self._tool_impls.get(tool_call.name)
        if impl is None:
            return f"result:{tool_call.name}"
        return await impl(tool_call)


class _AutoApproveBroker:
    def __init__(self, approved: bool = True):
        self.approved = approved
        self.requests = []

    async def request_approval(self, **kwargs):
        self.requests.append(kwargs)
        return self.approved


def _make_ctx(session, **kwargs):
    params = dict(
        session=session,
        user_message="hello",
        messages=[],
        tool_schemas=[],
    )
    params.update(kwargs)
    return PipelineContext(**params)


def _build_pipeline(with_context_build: bool = False) -> StreamPipeline:
    pipeline = StreamPipeline()
    if with_context_build:
        pipeline.add_stage(StreamContextBuildStage())
    pipeline.add_stage(StreamLLMCallStage())
    pipeline.add_stage(StreamToolExecutionStage())
    pipeline.add_stage(StreamTerminalStage())
    return pipeline


def _tool_calls_turn(*tool_calls) -> list[dict]:
    return [
        {"type": "tool_calls", "data": list(tool_calls)},
        {"type": "done", "finish_reason": "tool_calls"},
    ]


def _final_turn() -> list[dict]:
    return [
        {"type": "token", "data": "final"},
        {"type": "done", "finish_reason": "stop"},
    ]


async def _collect(gen):
    return [event async for event in gen]


# ---------------------------------------------------------------------------
# 只读组并行 + 顺序还原
# ---------------------------------------------------------------------------

async def test_read_only_tools_run_in_parallel_and_results_keep_order() -> None:
    """两个慢只读工具并行执行（总耗时 < 两者之和）；结果按 LLM 原顺序还原。"""
    start_a = asyncio.Event()
    start_b = asyncio.Event()
    release = asyncio.Event()

    async def slow_a(tc):
        start_a.set()
        await release.wait()
        await asyncio.sleep(0.3)
        return "A"

    async def slow_b(tc):
        start_b.set()
        await release.wait()
        await asyncio.sleep(0.15)  # B 先完成，验证顺序还原
        return "B"

    llm = _FakeLLM(
        _tool_calls_turn(
            LLMToolCall(id="1", name="read_a", arguments="{}"),
            LLMToolCall(id="2", name="read_b", arguments="{}"),
        ),
        _final_turn(),
    )
    session = _FakeSession(llm, tool_impls={"read_a": slow_a, "read_b": slow_b})
    ctx = _make_ctx(session, max_parallel_tools=4)
    pipeline = _build_pipeline()

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    task = asyncio.create_task(_collect(pipeline.run(ctx)))
    # 两个工具都开始执行后才放行：若串行执行，start_b 在 release 前永不 set → 超时
    await asyncio.wait_for(
        asyncio.gather(start_a.wait(), start_b.wait()), timeout=2.0
    )
    release.set()
    await task
    elapsed = loop.time() - t0

    # 并行 ≈ 0.30s；串行 ≥ 0.45s
    assert elapsed < 0.40

    tool_msgs = [m for m in session.state.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["1", "2"]
    assert [m["content"] for m in tool_msgs] == ["A", "B"]


# ---------------------------------------------------------------------------
# 写类串行（先写完写类、再并行只读组）
# ---------------------------------------------------------------------------

async def test_write_tools_run_serially_before_read_only_group() -> None:
    """write/edit 按 LLM 顺序串行执行完，read_file 才并行执行。"""
    order = []

    async def write_impl(tc):
        order.append(f"{tc.name}:start")
        await asyncio.sleep(0.05)
        order.append(f"{tc.name}:end")
        return "wrote"

    async def read_impl(tc):
        order.append("read_file:start")
        order.append("read_file:end")
        return "read"

    llm = _FakeLLM(
        _tool_calls_turn(
            LLMToolCall(id="1", name="write", arguments="{}"),
            LLMToolCall(id="2", name="edit", arguments="{}"),
            LLMToolCall(id="3", name="read_file", arguments="{}"),
        ),
        _final_turn(),
    )
    session = _FakeSession(
        llm,
        tool_impls={
            "write": write_impl,
            "edit": write_impl,
            "read_file": read_impl,
        },
    )
    ctx = _make_ctx(session)
    pipeline = _build_pipeline()

    await _collect(pipeline.run(ctx))

    assert order == [
        "write:start", "write:end",
        "edit:start", "edit:end",
        "read_file:start", "read_file:end",
    ]


# ---------------------------------------------------------------------------
# 超时：错误串 + 不中断其余工具
# ---------------------------------------------------------------------------

async def test_tool_timeout_returns_error_string_and_does_not_block_others() -> None:
    """超时工具（串行写类 + 并行只读各一）返回错误串，其余工具正常执行。"""
    async def slow(tc):
        await asyncio.sleep(0.5)
        return "too late"

    async def fast(tc):
        return f"fast:{tc.name}"

    llm = _FakeLLM(
        _tool_calls_turn(
            LLMToolCall(id="1", name="write_slow", arguments="{}"),
            LLMToolCall(id="2", name="write_fast", arguments="{}"),
            LLMToolCall(id="3", name="read_slow", arguments="{}"),
            LLMToolCall(id="4", name="read_fast", arguments="{}"),
        ),
        _final_turn(),
    )
    session = _FakeSession(
        llm,
        tool_impls={
            "write_slow": slow, "write_fast": fast,
            "read_slow": slow, "read_fast": fast,
        },
    )
    ctx = _make_ctx(session, tool_timeout=0.05)
    pipeline = _build_pipeline()

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await _collect(pipeline.run(ctx))
    elapsed = loop.time() - t0

    # 无超时串行 ≈ 1.05s；有超时 ≈ 0.15s —— 慢工具被 0.05s 掐断
    assert elapsed < 0.4

    tool_msgs = [m for m in session.state.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 4
    assert "write_slow timed out after 0.05s" in tool_msgs[0]["content"]
    assert tool_msgs[1]["content"] == "fast:write_fast"
    assert "read_slow timed out after 0.05s" in tool_msgs[2]["content"]
    assert tool_msgs[3]["content"] == "fast:read_fast"


# ---------------------------------------------------------------------------
# usage 事件：只进 recorder，不转发前端；recorder 失败不打断流
# ---------------------------------------------------------------------------

async def test_usage_event_goes_to_recorder_not_frontend() -> None:
    recorded = []

    async def recorder(data):
        recorded.append(data)

    llm = _FakeLLM([
        {"type": "token", "data": "hi "},
        {"type": "usage", "data": {
            "prompt_tokens": 10, "completion_tokens": 5, "model": "m", "cost": 0.01,
        }},
        {"type": "done", "finish_reason": "stop"},
    ])
    session = _FakeSession(llm)
    ctx = _make_ctx(session, usage_recorder=recorder)
    pipeline = _build_pipeline()

    events = await _collect(pipeline.run(ctx))

    assert not any(e.get("type") == "usage" for e in events)
    assert len(recorded) == 1
    assert recorded[0]["prompt_tokens"] == 10
    assert recorded[0]["completion_tokens"] == 5
    # 正常收尾不受影响
    assert events[-1]["type"] == "done"


async def test_usage_recorder_failure_does_not_break_stream() -> None:
    async def broken_recorder(data):
        raise RuntimeError("usage backend down")

    llm = _FakeLLM([
        {"type": "usage", "data": {"prompt_tokens": 1}},
        {"type": "done", "finish_reason": "stop"},
    ])
    session = _FakeSession(llm)
    ctx = _make_ctx(session, usage_recorder=broken_recorder)
    pipeline = _build_pipeline()

    events = await _collect(pipeline.run(ctx))
    assert events[-1]["type"] == "done"


# ---------------------------------------------------------------------------
# max_parallel_tools 并发上限
# ---------------------------------------------------------------------------

async def test_max_parallel_tools_caps_concurrency() -> None:
    """8 个只读工具、上限 2 → 任意时刻最多 2 个并发，结果全部落库。"""
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def tool_impl(tc):
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.05)
        async with lock:
            active -= 1
        return "ok"

    names = [f"read_{i}" for i in range(8)]
    tool_calls = [
        LLMToolCall(id=str(i), name=n, arguments="{}") for i, n in enumerate(names)
    ]
    llm = _FakeLLM(_tool_calls_turn(*tool_calls), _final_turn())
    session = _FakeSession(llm, tool_impls={n: tool_impl for n in names})
    ctx = _make_ctx(session, max_parallel_tools=2)
    pipeline = _build_pipeline()

    await _collect(pipeline.run(ctx))

    assert peak == 2
    tool_msgs = [m for m in session.state.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 8


# ---------------------------------------------------------------------------
# parallel_writes=True：写类并入并行组
# ---------------------------------------------------------------------------

async def test_parallel_writes_runs_write_tools_concurrently() -> None:
    start_a = asyncio.Event()
    start_b = asyncio.Event()
    release = asyncio.Event()

    async def write_impl(tc):
        (start_a if tc.name == "write" else start_b).set()
        await release.wait()
        await asyncio.sleep(0.1)
        return "wrote"

    llm = _FakeLLM(
        _tool_calls_turn(
            LLMToolCall(id="1", name="write", arguments="{}"),
            LLMToolCall(id="2", name="edit", arguments="{}"),
        ),
        _final_turn(),
    )
    session = _FakeSession(llm, tool_impls={"write": write_impl, "edit": write_impl})
    ctx = _make_ctx(session, parallel_writes=True, max_parallel_tools=2)
    pipeline = _build_pipeline()

    task = asyncio.create_task(_collect(pipeline.run(ctx)))
    # 若写类仍串行，start_b 在 release 前永不 set → wait_for 超时
    await asyncio.wait_for(
        asyncio.gather(start_a.wait(), start_b.wait()), timeout=2.0
    )
    release.set()
    await task


# ---------------------------------------------------------------------------
# 确认审批：逐工具生效、被确认工具回退串行
# ---------------------------------------------------------------------------

async def test_confirmed_tool_runs_serially_before_parallel_group() -> None:
    order = []
    broker = _AutoApproveBroker()

    async def bash_impl(tc):
        order.append("bash:run")
        return "bash done"

    async def read_impl(tc):
        order.append("read:run")
        return "read done"

    policy = types.SimpleNamespace(require_confirmation={"bash"})
    llm = _FakeLLM(
        _tool_calls_turn(
            LLMToolCall(id="1", name="bash", arguments="{}"),
            LLMToolCall(id="2", name="read", arguments="{}"),
        ),
        _final_turn(),
    )
    session = _FakeSession(
        llm,
        tool_impls={"bash": bash_impl, "read": read_impl},
        policy=policy,
        broker=broker,
    )
    ctx = _make_ctx(session)
    pipeline = _build_pipeline()

    await _collect(pipeline.run(ctx))

    assert len(broker.requests) == 1
    assert broker.requests[0]["tool_name"] == "bash"
    # 被确认工具先串行，只读工具无需确认、并行在后
    assert order == ["bash:run", "read:run"]

    tool_msgs = [m for m in session.state.messages if m["role"] == "tool"]
    assert tool_msgs[0]["content"] == "bash done"


async def test_confirmed_tool_denied_returns_denial_message() -> None:
    order = []
    broker = _AutoApproveBroker(approved=False)

    async def bash_impl(tc):
        order.append("bash:run")
        return "bash done"

    policy = types.SimpleNamespace(require_confirmation={"bash"})
    llm = _FakeLLM(
        _tool_calls_turn(LLMToolCall(id="1", name="bash", arguments="{}")),
        _final_turn(),
    )
    session = _FakeSession(
        llm, tool_impls={"bash": bash_impl}, policy=policy, broker=broker
    )
    ctx = _make_ctx(session)
    pipeline = _build_pipeline()

    await _collect(pipeline.run(ctx))

    assert order == []  # 工具未执行
    tool_msgs = [m for m in session.state.messages if m["role"] == "tool"]
    assert "did not approve" in tool_msgs[0]["content"]


# ---------------------------------------------------------------------------
# 断流兜底：aclose / task.cancel → assistant 占位消息
# ---------------------------------------------------------------------------

async def test_stream_abort_aclose_persists_placeholder() -> None:
    """token 已发出、done 未发出时 aclose() → 占位消息落库。"""
    llm = _FakeLLM([
        {"type": "token", "data": "partial "},
        {"type": "done", "finish_reason": "stop"},
    ])
    session = _FakeSession(llm)
    ctx = _make_ctx(session)
    pipeline = _build_pipeline()

    gen = pipeline.run(ctx)
    event = await anext(gen)
    assert event["type"] == "token"
    await gen.aclose()

    assert ctx.aborted is True
    assert session.state.messages[-1]["role"] == "assistant"
    assert "响应中断" in session.state.messages[-1]["content"]


async def test_stream_abort_placeholder_when_last_message_is_user() -> None:
    """无任何 token 产出、唯一事件是 done 时 aclose() → 最后一条是 user → 占位。"""
    llm = _FakeLLM([{"type": "done", "finish_reason": "stop"}])
    session = _FakeSession(llm)
    ctx = _make_ctx(session)
    pipeline = _build_pipeline(with_context_build=True)

    gen = pipeline.run(ctx)
    event = await anext(gen)
    assert event["type"] == "done"
    await gen.aclose()

    roles = [m["role"] for m in session.state.messages]
    assert roles == ["user", "assistant"]
    assert "响应中断" in session.state.messages[-1]["content"]


async def test_stream_abort_task_cancel_during_tool_execution() -> None:
    """工具执行中途 task.cancel() → CancelledError 被兜底，占位消息落库。"""
    release = asyncio.Event()

    async def slow(tc):
        await release.wait()  # 永不放行
        return "never"

    llm = _FakeLLM(
        _tool_calls_turn(LLMToolCall(id="1", name="read", arguments="{}")),
        _final_turn(),
    )
    session = _FakeSession(llm, tool_impls={"read": slow})
    ctx = _make_ctx(session)
    pipeline = _build_pipeline(with_context_build=True)

    gen = pipeline.run(ctx)
    task = asyncio.create_task(_collect(gen))
    await asyncio.sleep(0.05)  # 让工具执行挂起
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # 用户消息已落库、assistant 回复未发出 → 占位兜底
    roles = [m["role"] for m in session.state.messages]
    assert roles[-2:] == ["user", "assistant"]
    assert "响应中断" in session.state.messages[-1]["content"]
