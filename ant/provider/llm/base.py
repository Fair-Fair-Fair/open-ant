"""LLM provider backed by a litellm Router.

The Router provides the reliability trio for free — retries, per-request
timeout and ordered model fallbacks — so every call (streaming or not)
behaves the same under provider outages.  Usage accounting (tokens +
cost) is emitted as a ``usage`` event on streams and forwarded to an
optional async ``usage_callback`` on non-streaming calls.
"""

import logging
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Awaitable, Callable, Optional, cast

from litellm import TYPE_CHECKING, Choices, Router, completion_cost
from litellm.types.completion import ChatCompletionMessageParam as Message
from litellm.types.utils import OpenAIChatCompletionFinishReason

if TYPE_CHECKING:
    from ant.utils.config import LLMConfig

logger = logging.getLogger(__name__)

StopReason = OpenAIChatCompletionFinishReason


@dataclass
class LLMToolCall:
    """A tool/function call from the LLM."""

    id: str
    name: str
    arguments: str  # JSON string


def _build_retry_policy(timeout: float, num_retries: int) -> Any:
    """Best-effort ``litellm.RetryPolicy`` construction.

    RetryPolicy's shape changed across litellm versions:
      * < ~1.52: a class with ``RetryPolicy(time_to_retry, num_retries)``
      * >= ~1.52: a TypedDict with per-exception keys
        (``TimeoutExceptionRetries`` / ``APIConnectionErrorRetries`` /
        ``RateLimitErrorRetries`` / ``TimeToRetryTimeout``)
    Both forms are attempted; ``None`` is returned when the installed
    version accepts neither — the Router then simply relies on its own
    ``num_retries``/``timeout`` settings.  Lazy import so very old litellm
    releases (pre-RetryPolicy, < ~1.13) degrade gracefully instead of
    failing at module import time.
    """
    try:
        from litellm import RetryPolicy  # lazy: absent in very old versions
    except ImportError:
        return None
    try:
        return RetryPolicy(time_to_retry=timeout, num_retries=num_retries)
    except Exception:
        try:
            return RetryPolicy(
                TimeoutExceptionRetries=num_retries,
                APIConnectionErrorRetries=num_retries,
                RateLimitErrorRetries=num_retries,
                TimeToRetryTimeout=timeout,
            )
        except Exception:
            return None


class LLMProvider:
    """LLM provider using litellm Router for multi-provider support."""

    def __init__(
            self,
            model: str,
            api_key: str,
            api_base: Optional[str] = None,
            temperature: float = 0.7,
            max_tokens: int = 2048,
            num_retries: int = 2,
            timeout: float = 120.0,
            fallbacks: Optional[list[str]] = None,
            summarize_model: Optional[str] = None,
            usage_callback: Optional[Callable[[dict], Awaitable[None]]] = None,
            **kwargs: Any,
    ):
        """Initialize LLM provider.

        ``num_retries`` / ``timeout`` / ``fallbacks`` configure the Router
        resilience layer.  ``summarize_model`` is the dedicated small model
        for context compaction (``None`` = use the main model).  ``usage_callback``
        is an optional async callable receiving the usage dict (``prompt_tokens``
        / ``completion_tokens`` / ``model`` / ``cost``) after each non-streaming
        ``chat()``; failures there never break the chat call.
        """
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.num_retries = num_retries
        self.timeout = timeout
        self.fallbacks = list(fallbacks or [])
        self.summarize_model = summarize_model
        self.usage_callback = usage_callback
        self._settings = kwargs

        litellm_params: dict[str, Any] = {
            "model": self.model,
            "api_key": self.api_key,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.api_base:
            litellm_params["api_base"] = self.api_base

        router_kwargs: dict[str, Any] = {
            "model_list": [
                {
                    "model_name": self.model,
                    "litellm_params": litellm_params,
                }
            ],
            "num_retries": self.num_retries,
            "timeout": self.timeout,
        }
        if self.fallbacks:
            # Ordered degradation: primary → fallbacks[0] → fallbacks[1] …
            router_kwargs["fallbacks"] = [{self.model: list(self.fallbacks)}]
        retry_policy = _build_retry_policy(self.timeout, self.num_retries)
        if retry_policy is not None:
            router_kwargs["retry_policy"] = retry_policy

        try:
            self._router = Router(**router_kwargs)
        except TypeError:
            # Very old litellm Routers (< ~1.13) predate the retry_policy
            # kwarg — drop it and retry once before surfacing the error.
            router_kwargs.pop("retry_policy", None)
            self._router = Router(**router_kwargs)

    @classmethod
    def from_config(cls, config: "LLMConfig") -> "LLMProvider":
        """Create provider from LLMConfig."""
        return cls(
            model=config.model,
            api_key=config.api_key,
            api_base=config.api_base,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            num_retries=config.num_retries,
            timeout=config.timeout,
            fallbacks=list(config.fallbacks or []),
            summarize_model=config.summarize_model,
        )

    async def chat(
            self,
            messages: list[Message],
            tools: Optional[list[dict[str, Any]]] = None,
            **kwargs: Any,
    ) -> tuple[str, list[LLMToolCall], StopReason]:
        """Send a chat request to the LLM.

        Default implementation using litellm Router. Subclasses can
        override if provider-specific behavior is needed.

        Returns:
            Tuple of (content, tool_calls, stop_reason)
        """
        request_kwargs = self._build_request_kwargs(messages, tools, **kwargs)

        response = await self._router.acompletion(**request_kwargs)

        choice = cast(Choices, response.choices[0])
        message = choice.message
        stop_reason = choice.finish_reason

        tool_calls = [
            LLMToolCall(
                # Some providers omit the tool-call id; never KeyError on it.
                id=tc.get("id", ""),
                name=tc["function"]["name"],
                arguments=tc["function"]["arguments"],
            )
            for tc in (message.tool_calls or [])
        ]

        usage = self._extract_usage(response)
        if usage is not None and self.usage_callback is not None:
            try:
                await self.usage_callback(usage)
            except Exception:
                # Accounting is best-effort: a broken callback must never
                # break the chat call itself.
                logger.warning("usage_callback failed", exc_info=True)

        return (message.content or "", tool_calls, stop_reason)

    async def stream_chat(
            self,
            messages: list[Message],
            tools: Optional[list[dict[str, Any]]] = None,
            **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """
        流式聊天，生成事件：
        - {"type": "token", "data": str}       : 文本增量
        - {"type": "tool_calls", "data": list[LLMToolCall]} : 完整工具调用（流结束时发送，若存在）
        - {"type": "usage", "data": dict}      : 用量记账
          {prompt_tokens, completion_tokens, model, cost}（done 之前）
        - {"type": "done", "finish_reason": str} : 结束信号
        - {"type": "error", "data": str}       : 出错信号（发送后生成器正常结束，不再 raise）
        """

        request_kwargs = self._build_request_kwargs(messages, tools, stream=True, **kwargs)
        # 用于累积 tool_calls (因为可能跨多个 chunk)

        tool_call_accumulator: list[dict] = []  # 存放部分构建的 tool_call

        finish_reason: Optional[StopReason] = None

        final_content_pieces: list[str] = []  # 用于组装 content（虽然我们逐 token 发送，但可能也要知道最终内容）  # noqa: E501

        # 流式 usage：litellm 在最终 chunk 上聚合 usage 与 response_cost
        stream_usage: Any = None
        stream_cost: Optional[float] = None

        try:
            response = await self._router.acompletion(**request_kwargs)

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
                        idx = tc.index if hasattr(tc, "index") else 0

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

                # 记录最终 chunk 上的 usage（部分 provider 只在末 chunk 上报）
                chunk_usage = getattr(chunk, "usage", None)
                if (
                    chunk_usage is not None
                    and getattr(chunk_usage, "prompt_tokens", None) is not None
                ):
                    stream_usage = chunk_usage
                hidden = getattr(chunk, "_hidden_params", None) or {}
                hidden_cost = hidden.get("response_cost")
                if hidden_cost is not None:
                    stream_cost = float(hidden_cost)

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

            # usage 事件（在 done 之前）
            usage_event = self._build_stream_usage(stream_usage, stream_cost)
            if usage_event is not None:
                yield {"type": "usage", "data": usage_event}

            # 发送结束事件
            yield {"type": "done", "finish_reason": finish_reason or "stop"}

        except Exception as e:
            # 错误事件本身就是终止信号——yield 后正常结束生成器；
            # 再 raise 会让调用方（pipeline）的迭代崩溃，错误被二次处理。
            yield {"type": "error", "data": str(e)}
            return

    # ── usage / cost helpers ────────────────────────────────────────────

    def _extract_usage(self, response: Any) -> Optional[dict]:
        """Best-effort usage dict from a non-streaming response; None if the
        provider reported no usage at all."""
        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        if prompt_tokens is None and completion_tokens is None:
            return None
        return {
            "prompt_tokens": int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "model": self.model,
            "cost": self._compute_response_cost(response),
        }

    def _build_stream_usage(
            self, usage: Any, cost: Optional[float]
    ) -> Optional[dict]:
        """Build the ``usage`` event payload from the final stream chunk."""
        if usage is None:
            return None
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        if cost is None:
            cost = self._compute_tokens_cost(prompt_tokens, completion_tokens)
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "model": self.model,
            "cost": cost,
        }

    def _compute_response_cost(self, response: Any) -> float:
        """Best-effort USD cost of a completion response.

        Prefers the cost litellm attached to the response
        (``_hidden_params.response_cost``); falls back to
        ``litellm.completion_cost``.  Unknown/custom models have no price
        table entry — 0.0 in that case.
        """
        try:
            hidden = getattr(response, "_hidden_params", None) or {}
            cost = hidden.get("response_cost")
            if cost is not None:
                return float(cost)
        except Exception:
            pass
        try:
            cost = completion_cost(
                model=self.model, completion_response=response
            )
            return float(cost or 0.0)
        except Exception:
            return 0.0

    def _compute_tokens_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        """Best-effort USD cost from raw token counts (streaming path)."""
        try:
            cost = completion_cost(
                model=self.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            return float(cost or 0.0)
        except Exception:
            return 0.0

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
