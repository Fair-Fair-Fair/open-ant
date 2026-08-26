"""Phase 4C — StreamRedactor 流式脱敏回归测试（安全 P0 #12）。

不依赖任何网络。验证：
  - 单 token 内含密钥整段被吞（feed 返回空），flush 时脱敏补全
  - 跨 chunk 密钥（sk-xxx 拆两半）在 flush 时被脱敏，且安全前缀先出
  - 缓冲边界：正好 buffer_size 时什么都不放，之后逐字符释放
  - buffer_size=0 直通（feed 原样返回、flush 空）
  - flush 幂等；flush 之后 feed 是 no-op
  - 密钥跨释放边界时整段压住，绝不部分泄漏前缀
  - from_output_guard 复用 OutputGuard 的自定义 secret 正则
  - 集成：StreamLLMCallStage 先 feed 后 yield、done 前 flush 补尾
"""

import types

from ant.core.guardrails import Guardrails, OutputGuard, StreamRedactor
from ant.utils.config import GuardrailConfig, OutputGuardrailConfig

_SK_SECRET = "sk-" + "A" * 40


# ---------------------------------------------------------------------------
# 单 token 整段被吞 / flush 脱敏
# ---------------------------------------------------------------------------

def test_secret_in_single_token_is_withheld_then_redacted_on_flush() -> None:
    """单 token 内含完整密钥：feed 返回空（整段被吞），flush 时脱敏补全。"""
    redactor = StreamRedactor(buffer_size=128)
    token = f"hello world, my api key is {_SK_SECRET} bye"
    assert redactor.feed(token) == ""  # < buffer_size → 什么都不放
    tail = redactor.flush()
    assert "[REDACTED_API_KEY]" in tail
    assert "sk-" not in tail
    assert "AAAA" not in tail
    assert tail.startswith("hello world")


# ---------------------------------------------------------------------------
# 跨 chunk 密钥
# ---------------------------------------------------------------------------

def test_cross_chunk_secret_redacted_at_flush() -> None:
    """sk-xxx 拆两半：安全前缀先出，密钥本体在 flush 时被脱敏。"""
    redactor = StreamRedactor(buffer_size=32)
    assert redactor.feed("the key is sk-") == ""  # 12 chars < 32 → 压住
    out = redactor.feed("A" * 40)
    assert out == "the key is "  # 密钥跨边界 → 只放 match 之前的安全前缀
    assert redactor.flush() == "[REDACTED_API_KEY]"
    assert "sk-" not in out


# ---------------------------------------------------------------------------
# 缓冲边界
# ---------------------------------------------------------------------------

def test_release_starts_only_after_buffer_size() -> None:
    """正好 buffer_size 时什么都不放；之后每多一字符释放一个前缀字符。"""
    redactor = StreamRedactor(buffer_size=10)
    assert redactor.feed("abcdefghij") == ""  # 正好 10 → boundary=0 → 不放
    assert redactor.feed("k") == "a"          # 11 chars → boundary=1 → 放 "a"
    assert redactor.feed("l") == "b"


def test_secret_split_at_buffer_boundary_is_never_leaked() -> None:
    """密钥恰在 buffer_size 边界处开始：整段压住，只放密钥之前的文本。"""
    redactor = StreamRedactor(buffer_size=10)
    assert redactor.feed("abcdefghij") == ""     # 正好 buffer_size
    assert redactor.feed("sk-") == "abc"         # 13 chars；密钥前缀还没成串 → 放 3
    assert redactor.feed("A" * 40) == "defghij"  # 密钥成串 → 放完 match 之前的
    assert redactor.flush() == "[REDACTED_API_KEY]"


def test_secret_prefix_never_released_before_match_completes() -> None:
    """buffer_size 覆盖密钥长度时：密钥的任意前缀都压住、不部分泄漏。

    注：这是"延迟换覆盖"的有效边界——buffer_size 小于密钥本身长度时，
    密钥前缀会被逐步释放（正则脱敏在流式下无法覆盖全部边界情形，
    见 StreamRedactor docstring 的局限说明）。
    """
    redactor = StreamRedactor(buffer_size=48)
    leaked = ""
    for ch in "start " + _SK_SECRET + " end":
        leaked += redactor.feed(ch)
    tail = redactor.flush()
    full = leaked + tail
    assert "sk-" not in full
    assert "[REDACTED_API_KEY]" in full
    assert full == "start [REDACTED_API_KEY] end"


def test_secret_inside_release_region_is_redacted_mid_stream() -> None:
    """完整密钥落在释放区内：不在 flush 时、而在流式中途就被脱敏。"""
    redactor = StreamRedactor(buffer_size=8)
    redactor.feed("A" * 10)  # → "AA"
    redactor.feed("B" * 10)  # → "AAAAAAAAAA"
    # 注意结尾用非字母数字（!）：sk- 正则是贪婪的 [A-Za-z0-9]{32,}，
    # 字母数字后缀会把 match 延长到缓冲区末尾、整段压住，测不到流式中途脱敏。
    out = redactor.feed(_SK_SECRET + "!" * 10)
    assert "[REDACTED_API_KEY]" in out
    assert "sk-" not in out
    assert redactor.flush() == "!" * 8


def test_plain_text_flows_losslessly_with_bounded_delay() -> None:
    """普通文本无损流过（最多 buffer_size 字符延迟）。"""
    redactor = StreamRedactor(buffer_size=8)
    out = ""
    for ch in "hello world, this is a plain sentence":
        out += redactor.feed(ch)
    out += redactor.flush()
    assert out == "hello world, this is a plain sentence"


# ---------------------------------------------------------------------------
# buffer_size=0 直通 / flush 幂等
# ---------------------------------------------------------------------------

def test_buffer_size_zero_is_passthrough() -> None:
    """buffer_size=0 直通：feed 原样返回、不缓冲不脱敏；flush 恒空。"""
    redactor = StreamRedactor(buffer_size=0)
    assert redactor.feed(_SK_SECRET) == _SK_SECRET
    assert redactor.flush() == ""
    assert redactor.feed("more") == "more"


def test_flush_is_idempotent_and_feed_after_flush_is_noop() -> None:
    redactor = StreamRedactor(buffer_size=16)
    assert redactor.feed(f"token: {_SK_SECRET}") == "token: "  # 安全前缀先出
    assert redactor.flush() == "[REDACTED_API_KEY]"
    assert redactor.flush() == ""  # 幂等
    assert redactor.feed("more") == ""  # flush 后 feed 是 no-op


# ---------------------------------------------------------------------------
# 复用 OutputGuard 的正则
# ---------------------------------------------------------------------------

def test_from_output_guard_reuses_custom_patterns() -> None:
    """from_output_guard 复用 OutputGuard 的自定义 redact_patterns。"""
    guard = OutputGuard(OutputGuardrailConfig(redact_patterns=[r"SECRET-\d+"]))
    redactor = StreamRedactor.from_output_guard(guard, buffer_size=64)
    assert redactor.feed("the value is SECRET-") == ""
    assert redactor.feed("987654") == ""
    assert redactor.flush() == "the value is [REDACTED]"


def test_from_output_guard_custom_patterns_replace_defaults() -> None:
    """自定义正则替换默认正则：sk- 密钥不再被脱敏。"""
    guard = OutputGuard(OutputGuardrailConfig(redact_patterns=[r"SECRET-\d+"]))
    redactor = StreamRedactor.from_output_guard(guard, buffer_size=64)
    redactor.feed(_SK_SECRET)
    assert redactor.flush() == _SK_SECRET


def test_from_output_guard_falls_back_to_defaults() -> None:
    """guard 没有 _secret_patterns（鸭子类型）时回落默认正则。"""
    redactor = StreamRedactor.from_output_guard(types.SimpleNamespace(), buffer_size=64)
    redactor.feed(f"key {_SK_SECRET}")
    assert redactor.flush() == "key [REDACTED_API_KEY]"


# ---------------------------------------------------------------------------
# 集成：StreamLLMCallStage 接线
# ---------------------------------------------------------------------------

async def test_llm_stage_streams_redacted_tokens_and_flush_tail() -> None:
    """集成验证：token 先 feed 后 yield，done 前 flush 补尾。

    redact_buffer_chars 从 config.guardrails.output getattr 读取（16），
    密钥跨 chunk 时不泄漏、在 done 前补尾输出。
    """
    from ant.core.stream_pipeline import PipelineContext, StreamPipeline
    from ant.core.stream_stages import StreamLLMCallStage, StreamTerminalStage

    class _StreamLLM:
        async def stream_chat(self, messages, tools):
            yield {"type": "token", "data": "hello "}
            yield {"type": "token", "data": "sk-"}
            yield {"type": "token", "data": "A" * 40}
            yield {"type": "token", "data": " world"}
            yield {"type": "done", "finish_reason": "stop"}

    class _State:
        def __init__(self):
            self.messages = []

        async def add_message(self, message):
            self.messages.append(message)

        def build_messages(self):
            return []

    class _Sess:
        def __init__(self):
            self.agent = types.SimpleNamespace(llm=_StreamLLM())
            self.session_id = "s1"
            self.state = _State()
            self.shared_context = types.SimpleNamespace(
                guardrails=Guardrails(GuardrailConfig()),
                config=types.SimpleNamespace(
                    guardrails=types.SimpleNamespace(
                        output=types.SimpleNamespace(redact_buffer_chars=16),
                    ),
                ),
            )

    pipeline = StreamPipeline()
    pipeline.add_stage(StreamLLMCallStage())
    pipeline.add_stage(StreamTerminalStage())
    ctx = PipelineContext(session=_Sess(), user_message="hi")

    tokens = []
    async for event in pipeline.run(ctx):
        if event["type"] == "token":
            tokens.append(event["data"])

    text = "".join(tokens)
    assert "sk-" not in text
    assert "[REDACTED_API_KEY]" in text
    assert text.startswith("hello ")
    # ctx.response_content 累积的是脱敏文本（历史落库与用户所见一致）
    assert ctx.response_content == text


async def test_llm_stage_redact_buffer_zero_passes_tokens_through() -> None:
    """redact_buffer_chars=0 → 不建 redactor → token 原样流转。"""
    from ant.core.stream_pipeline import PipelineContext, StreamPipeline
    from ant.core.stream_stages import StreamLLMCallStage, StreamTerminalStage

    class _StreamLLM:
        async def stream_chat(self, messages, tools):
            yield {"type": "token", "data": f"key {_SK_SECRET}"}
            yield {"type": "done", "finish_reason": "stop"}

    class _State:
        def __init__(self):
            self.messages = []

        async def add_message(self, message):
            self.messages.append(message)

        def build_messages(self):
            return []

    class _Sess:
        def __init__(self):
            self.agent = types.SimpleNamespace(llm=_StreamLLM())
            self.session_id = "s1"
            self.state = _State()
            self.shared_context = types.SimpleNamespace(
                guardrails=Guardrails(GuardrailConfig()),
                config=types.SimpleNamespace(
                    guardrails=types.SimpleNamespace(
                        output=types.SimpleNamespace(redact_buffer_chars=0),
                    ),
                ),
            )

    pipeline = StreamPipeline()
    pipeline.add_stage(StreamLLMCallStage())
    pipeline.add_stage(StreamTerminalStage())
    ctx = PipelineContext(session=_Sess(), user_message="hi")

    tokens = []
    async for event in pipeline.run(ctx):
        if event["type"] == "token":
            tokens.append(event["data"])

    assert "".join(tokens) == f"key {_SK_SECRET}"
