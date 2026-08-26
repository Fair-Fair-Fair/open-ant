import asyncio
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from ant.core.agent import AgentSession

logger = logging.getLogger(__name__)

#: jsonschema 校验错误 → 中文提示的类型名映射（供 LLM 纠错）
_TYPE_NAMES = {
    "string": "字符串",
    "integer": "整数",
    "number": "数字",
    "boolean": "布尔值",
    "array": "数组",
    "object": "对象",
    "null": "空值",
}


class BaseTool(ABC):
    """Abstract base class for all tools."""
    name: str
    description: str
    parameters: dict[str, Any]

    @abstractmethod
    async def execute(self, session: "AgentSession", **kwargs) -> str:
        pass

    def get_tool_schema(self) -> dict[str, Any]:
        # 统一约束：所有工具的 parameters 都是 strict object —— 不允许
        # 未声明的额外参数（additionalProperties: false）。该 schema 同时
        # 是运行时 validate_args 的校验依据，LLM 看到的与执行的必须一致。
        parameters = self.parameters
        parameters.setdefault("type", "object")
        parameters.setdefault("additionalProperties", False)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters
            }
        }


def _describe_error(error: Any) -> str:
    """把 jsonschema 校验错误翻译成面向 LLM 的中文描述。

    兜底（如 enum/maximum 等未覆盖的校验器）直接使用原始 message。
    """
    if error.validator == "type":
        expected = error.validator_value
        if isinstance(expected, list):
            expected_cn = " 或 ".join(
                _TYPE_NAMES.get(e, f"「{e}」") for e in expected
            )
        else:
            expected_cn = _TYPE_NAMES.get(expected, f"「{expected}」")
        return f"类型错误：应为{expected_cn}，实际为{type(error.instance).__name__}"
    if error.validator == "enum":
        allowed = "、".join(repr(v) for v in error.validator_value)
        return f"取值不在允许范围内（可选值：{allowed}）"
    return error.message


def validate_args(schema: dict[str, Any], kwargs: dict[str, Any]) -> tuple[bool, str]:
    """按 JSON Schema（Draft 7）校验工具调用参数。

    返回 ``(ok, message)``：校验失败时 ``ok=False``，``message`` 为面向
    LLM 的中文错误描述，列出具体出问题的参数名，便于模型下次调用时纠错。

    - ``session`` 是运行时内部注入的键，不属于工具的公开参数，校验前剔除；
    - 优先使用 jsonschema 库做完整校验（类型/枚举/范围等）；库不可用时
      降级为手动检查（多余参数 + 必需参数），并记录 warning。
    """
    # session 为运行时注入，不参与工具参数校验。
    kwargs = {k: v for k, v in kwargs.items() if k != "session"}

    properties = schema.get("properties", {})
    problems: list[str] = []

    # 1) 多余参数（additionalProperties 不允许的字段）
    extra = [k for k in kwargs if k not in properties]
    if extra:
        problems.append(
            "存在未定义的参数：" + "、".join(f"「{k}」" for k in extra)
        )

    # 2) 缺少必需参数
    missing = [k for k in schema.get("required", []) if k not in kwargs]
    if missing:
        problems.append(
            "缺少必需的参数：" + "、".join(f"「{k}」" for k in missing)
        )

    # 3) 类型 / 枚举等深层校验 —— 依赖 jsonschema（Draft 7）
    try:
        import jsonschema
    except ImportError:
        logger.warning(
            "jsonschema 未安装，参数校验降级为手动检查（仅多余参数 + 必需参数）"
        )
    else:
        validator = jsonschema.Draft7Validator(schema)
        for error in sorted(validator.iter_errors(kwargs), key=str):
            if error.validator in ("additionalProperties", "required"):
                continue  # 已在上方给出中文提示，避免重复
            param = error.path[0] if error.path else "<整体>"
            problems.append(f"参数「{param}」{_describe_error(error)}")

    if not problems:
        return True, ""
    return False, "参数校验失败：" + "；".join(problems)


def tool(name: str, description: str, parameters: dict[str, Any]) -> Callable:
    """Decorator to register a function as a tool."""
    def decorator(func: Callable) -> "FunctionTool":
        return FunctionTool(name, description, parameters, func)

    return decorator


class FunctionTool(BaseTool):
    """A tool created from a function using the @tool decorator."""
    def __init__(self,
                 name: str,
                 description: str,
                 parameters: dict[str, Any],
                 func: Callable):
        self.name = name
        self.description = description
        self.parameters = parameters
        self._func = func

    async def execute(self, session: "AgentSession", **kwargs) -> str:
        """Execute the underlying function."""
        result = self._func(session=session, **kwargs)
        if asyncio.iscoroutine(result):
            result = await result
        return str(result)
