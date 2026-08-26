"""Phase 2 第 3 条：工具参数 JSON Schema 校验（workspace/plan.md）。

覆盖：
  (a) validate_args 对多余参数 / 缺 required / 类型错误 / session 注入键的判定，
      以及 jsonschema 不可用时的降级行为；
  (b) get_tool_schema 统一给 parameters 补 type:"object" 与
      additionalProperties:false；
  (c) ToolRegistry.execute_tool 校验失败返回错误串、工具不执行、
      governance 不计入调用次数。
"""

import builtins
import logging
import sys
from pathlib import Path

# 根 pyproject.toml 的 pytest pythonpath=src 配置由并行代理负责，
# 这里沿用 test_tools_fixes.py 的做法：临时把 src 加入 sys.path 兜底。
_SRC = Path(__file__).resolve().parents[2]  # src/ant/tests -> src
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ant.tools.base import FunctionTool, validate_args  # noqa: E402
from ant.tools.builtin_tools import bash, edit_file, read_file, write_file  # noqa: E402
from ant.tools.policy import ToolGovernance, ToolPolicy  # noqa: E402
from ant.tools.registry import ToolRegistry  # noqa: E402


def _jsonschema_available() -> bool:
    try:
        import jsonschema  # noqa: F401
    except ImportError:
        return False
    return True


_SIMPLE_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
}


# ---------------------------------------------------------------------------
# (a) validate_args —— 直接校验
# ---------------------------------------------------------------------------


def test_validate_args_accepts_valid_args() -> None:
    ok, err = validate_args(_SIMPLE_SCHEMA, {"path": "/a.txt"})
    assert ok is True
    assert err == ""


def test_validate_args_rejects_extra_params() -> None:
    """多余参数（未在 properties 声明的键）必须被拒绝，并点名参数。"""
    ok, err = validate_args(_SIMPLE_SCHEMA, {"path": "/a.txt", "bogus": 1})
    assert ok is False
    assert "「bogus」" in err
    assert err.startswith("参数校验失败")


def test_validate_args_rejects_missing_required() -> None:
    """缺少必需参数必须被拒绝，并点名缺哪个参数。"""
    ok, err = validate_args(_SIMPLE_SCHEMA, {})
    assert ok is False
    assert "「path」" in err

    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }
    ok, err = validate_args(schema, {"path": "/a.txt"})
    assert ok is False
    assert "「content」" in err


def test_validate_args_rejects_wrong_type() -> None:
    """类型错误必须被拒绝。

    jsonschema 可用时做完整类型校验；降级模式（未安装）只做
    多余参数 + 必需参数检查，不做类型判断。
    """
    ok, err = validate_args(_SIMPLE_SCHEMA, {"path": 123})
    if _jsonschema_available():
        assert ok is False
        assert "「path」" in err
        assert "类型错误" in err
    else:
        assert ok is True


def test_validate_args_ignores_injected_session_kwarg() -> None:
    """session 是运行时内部注入的键：不算多余参数，也不能顶替必需参数。"""
    ok, err = validate_args(_SIMPLE_SCHEMA, {"path": "/a.txt", "session": object()})
    assert ok is True

    ok, err = validate_args(_SIMPLE_SCHEMA, {"session": object()})
    assert ok is False
    assert "「path」" in err
    assert "「session」" not in err


def test_validate_args_works_with_schema_without_type() -> None:
    """schema 缺顶层 type 字段也能校验（properties/required 仍生效）。"""
    schema = {"properties": {"a": {"type": "string"}}, "required": ["a"]}
    ok, err = validate_args(schema, {"a": "x"})
    assert ok is True

    ok, err = validate_args(schema, {"a": "x", "b": 2})
    assert ok is False
    assert "「b」" in err


def test_validate_args_fallback_without_jsonschema(monkeypatch, caplog) -> None:
    """jsonschema 未安装时降级为手动检查（多余参数 + 必需参数）并 warning。"""
    real_import = builtins.__import__

    def _no_jsonschema(name, *args, **kwargs):
        if name == "jsonschema":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_jsonschema)

    with caplog.at_level(logging.WARNING, logger="ant.tools.base"):
        ok, err = validate_args(_SIMPLE_SCHEMA, {"path": "/a", "bogus": 1})
    assert ok is False
    assert "「bogus」" in err
    assert any("jsonschema" in r.getMessage() for r in caplog.records)

    ok, err = validate_args(_SIMPLE_SCHEMA, {"path": "/a"})
    assert ok is True
    assert err == ""


# ---------------------------------------------------------------------------
# (b) get_tool_schema —— 严格化归一
# ---------------------------------------------------------------------------


def test_get_tool_schema_normalizes_parameters() -> None:
    """缺 type 的 parameters 自动补 type:"object" 并加 additionalProperties:false。"""
    tool = FunctionTool(
        "legacy_tool",
        "legacy tool",
        {"properties": {"a": {"type": "string"}}},
        lambda session: "ok",
    )
    params = tool.get_tool_schema()["function"]["parameters"]
    assert params["type"] == "object"
    assert params["additionalProperties"] is False

    # 幂等：重复调用不产生新键
    assert tool.get_tool_schema()["function"]["parameters"] is params


def test_all_builtin_tool_schemas_are_strict() -> None:
    """所有内置工具 schema 统一带 additionalProperties:false 与 type:"object"。"""
    for tool in (read_file, write_file, edit_file, bash):
        params = tool.get_tool_schema()["function"]["parameters"]
        assert params["type"] == "object"
        assert params["additionalProperties"] is False


def test_builtin_tool_required_lists_are_correct() -> None:
    def _required(tool) -> list[str]:
        return tool.get_tool_schema()["function"]["parameters"]["required"]

    assert _required(read_file) == ["path"]
    assert _required(write_file) == ["path", "content"]
    assert _required(edit_file) == ["path", "old_string", "new_string"]
    assert _required(bash) == ["command"]


# ---------------------------------------------------------------------------
# (c) ToolRegistry.execute_tool —— 校验失败不执行、不计入 governance
# ---------------------------------------------------------------------------


async def test_execute_tool_validation_failure_not_executed_and_not_counted() -> None:
    """校验失败直接返回错误串：工具不执行，governance 不计入调用次数。"""
    executed: list[dict] = []

    async def _impl(session, path: str) -> str:
        executed.append({"path": path})
        return f"ok:{path}"

    governance = ToolGovernance(ToolPolicy())
    registry = ToolRegistry(governance=governance)
    registry.register(FunctionTool(
        "strict_tool",
        "strict test tool",
        _SIMPLE_SCHEMA,
        _impl,
    ))

    # 多余参数 → 校验失败，不执行、不计入
    result = await registry.execute_tool(
        "strict_tool", session=object(), path="/a", bogus=1
    )
    assert result.startswith("参数校验失败")
    assert "「bogus」" in result
    assert executed == []
    assert governance.get_audit_summary()["total_calls"] == 0

    # 缺 required → 校验失败，不执行、不计入
    result = await registry.execute_tool("strict_tool", session=object())
    assert result.startswith("参数校验失败")
    assert "「path」" in result
    assert executed == []

    # 类型错误 → 校验失败（jsonschema 可用时）
    result = await registry.execute_tool("strict_tool", session=object(), path=123)
    if _jsonschema_available():
        assert result.startswith("参数校验失败")
        assert "类型错误" in result
        assert executed == []
    else:
        # 降级模式：类型不校验，正常执行
        assert result == "ok:123"
        executed.clear()

    # 校验通过 → 正常执行，且计入 governance 审计
    result = await registry.execute_tool("strict_tool", session=object(), path="/x")
    assert result == "ok:/x"
    assert executed == [{"path": "/x"}]
    summary = governance.get_audit_summary()
    assert summary["total_calls"] == 1
    assert summary["calls_by_tool"] == {"strict_tool": 1}


async def test_execute_tool_validation_failure_does_not_raise() -> None:
    """校验失败返回字符串而非 raise，幻觉参数不会炸掉整轮。"""
    registry = ToolRegistry()
    registry.register(FunctionTool(
        "empty_tool",
        "tool with no declared params",
        {"type": "object", "properties": {}, "required": []},
        lambda session: "ok",
    ))

    result = await registry.execute_tool(
        "empty_tool", session=object(), hallucinated=1
    )
    assert isinstance(result, str)
    assert result.startswith("参数校验失败")
    assert "「hallucinated」" in result
