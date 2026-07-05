"""Base LLM provider abstraction."""

from dataclasses import dataclass
from typing import AsyncGenerator
from typing import Any, Optional, cast

from litellm import acompletion, Choices, TYPE_CHECKING
from litellm.types.completion import ChatCompletionMessageParam as Message
from litellm.types.utils import OpenAIChatCompletionFinishReason

if TYPE_CHECKING:
    from ant.utils.config import LLMConfig

StopReason = OpenAIChatCompletionFinishReason


@dataclass
class LLMToolCall:
    """A tool/function call from the LLM."""

    id: str
    name: str
    arguments: str  # JSON string


class LLMProvider:
    """LLM provider using litellm for multi-provider support."""

    def __init__(
            self,
            model: str,
            api_key: str,
            api_base: Optional[str] = None,
            temperature: float = 0.7,
            max_tokens: int = 2048,
            **kwargs: Any,
    ):
        """Initialize LLM provider."""
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._settings = kwargs

    @classmethod
    def from_config(cls, config: "LLMConfig") -> "LLMProvider":
        """Create provider from LLMConfig."""
        return cls(
            model=config.model,
            api_key=config.api_key,
            api_base=config.api_base,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )

    async def chat(
            self,
            messages: list[Message],
            tools: Optional[list[dict[str, Any]]] = None,
            **kwargs: Any,
    ) -> tuple[str, list[LLMToolCall], StopReason]:
        """Send a chat request to the LLM.

        Default implementation using litellm. Subclasses can override
        if provider-specific behavior is needed.

        Returns:
            Tuple of (content, tool_calls, stop_reason)
        """
        request_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "api_key": self.api_key,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        if self.api_base:
            request_kwargs["api_base"] = self.api_base
        if tools:
            request_kwargs["tools"] = tools
        request_kwargs.update(kwargs)

        response = await acompletion(**request_kwargs)

        choice = cast(Choices, response.choices[0])
        message = choice.message
        stop_reason = choice.finish_reason

        return (
            message.content or "",
            [
                LLMToolCall(
                    id=tc["id"],
                    name=tc["function"]["name"],
                    arguments=tc["function"]["arguments"],
                )
                for tc in (message.tool_calls or [])
            ],
            stop_reason,
        )

    async def stream_chat(
            self,
            messages: list[Message],
            tools: Optional[list[dict[str, Any]]] = None,
            **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """
        流式聊天，生成事件：
        - {"type": "token", "data": str}       : 文本增量

        - {"type": "tool_calls", "data": list[LLMToolCall]} : 完整的工具调用（流结束时发送，若存在）

        - {"type": "done", "finish_reason": str} : 结束信号
        """

        request_kwargs = self._build_request_kwargs(messages, tools, stream=True, **kwargs)
        # 用于累积 tool_calls (因为可能跨多个 chunk)

        tool_call_accumulator: list[dict] = []  # 存放部分构建的 tool_call

        finish_reason: Optional[StopReason] = None

        final_content_pieces: list[str] = []  # 用于组装 content（虽然我们逐 token 发送，但可能也要知道最终内容）

        try:
            response = await acompletion(**request_kwargs)

            async for chunk in response:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                # 处理文本内容增量
                if delta.content is not None:
                    yield {"type": "token", "data": delta.content}
                    final_content_pieces.append(delta.content)

                # 处理工具调用增量（可能多次出现）
                if delta.tool_calls is not None:
                    for tc in delta.tool_calls:
                        # 找到或创建对应索引的 tool_call 条目
                        idx = tc.index if hasattr(tc, 'index') else 0

                        while len(tool_call_accumulator) <= idx:
                            tool_call_accumulator.append({"id": "", "name": "", "arguments": ""})

                        if tc.id:
                            tool_call_accumulator[idx]["id"] = tc.id

                        if tc.function and tc.function.name:
                            tool_call_accumulator[idx]["name"] = tc.function.name

                        if tc.function and tc.function.arguments:
                            tool_call_accumulator[idx]["arguments"] += tc.function.arguments

                # 获取 finish_reason (可能在最后一个 chunk)
                if choice.finish_reason:
                    finish_reason = choice.finish_reason

            # 流结束，发送 tool_calls（如果有）
            if tool_call_accumulator:
                tool_calls = [
                    LLMToolCall(
                        id=item["id"],
                        name=item["name"],
                        arguments=item["arguments"],
                    )
                    for item in tool_call_accumulator
                ]

                yield {"type": "tool_calls", "data": tool_calls}

            # 发送结束事件
            yield {"type": "done", "finish_reason": finish_reason or "stop"}

        except Exception as e:
            # 发生错误时，可以发送错误事件（根据需求调整）
            yield {"type": "error", "data": str(e)}
            raise

    def _build_request_kwargs(
            self,
            messages: list[Message],
            tools: Optional[list[dict[str, Any]]] = None,
            stream: bool = False,
            **kwargs: Any,
    ) -> dict[str, Any]:

        """构建 litellm 请求参数字典，复用给 chat 和 stream_chat。"""

        base = {
            "model": self.model,
            "messages": messages,
            "api_key": self.api_key,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": stream,
        }

        if self.api_base:
            base["api_base"] = self.api_base
        if tools:
            base["tools"] = tools
        base.update(kwargs)

        return base
