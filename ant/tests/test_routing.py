"""test_routing.py — improve.md #1 / #2 的回归测试。

覆盖：
(a) literal binding（无正则字符）精确匹配，且 resolve 回落到 default_agent
(b) 坏正则 / 历史 list 脏数据绑定被跳过，其余绑定正常 resolve
(c) tier 排序：literal(0) 优先于 正则(1) 优先于 .*(2)
(d) /route 命令传给 persist_binding 的是 str 而非 list（handlers 层直接验证）
"""

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ant.core.commands.handlers import RouteCommand
from ant.core.routing import Binding, RoutingTable


class FakeConfig:
    """Config 的最小替身：只实现 routing / default_agent / set_runtime 三个接口。"""

    def __init__(self, bindings=None, default_agent="default"):
        self.routing = {"bindings": list(bindings) if bindings else []}
        self.default_agent = default_agent
        self.sources = {}

    def set_runtime(self, key, value):
        if key == "routing.bindings":
            self.routing["bindings"] = value


def make_table(bindings, default_agent="default"):
    return RoutingTable(context=SimpleNamespace(config=FakeConfig(bindings, default_agent)))


# ---------- (a) literal binding 匹配 ----------

def test_binding_literal_exact_match():
    binding = Binding(agent="pickle", value="platform-telegram:123")
    assert binding.tier == 0
    assert binding.pattern.match("platform-telegram:123")
    assert not binding.pattern.match("platform-telegram:1234")
    assert not binding.pattern.match("x-platform-telegram:123")


def test_resolve_literal_binding_and_fallback():
    table = make_table([{"agent": "pickle", "value": "platform-telegram:123"}])
    assert table.resolve("platform-telegram:123") == "pickle"
    assert table.resolve("platform-telegram:456") == "default"


# ---------- (b) 坏绑定被跳过，其余正常 ----------

def test_binding_raises_on_invalid_value():
    # Binding 构造契约：坏正则 / 非字符串 value 必须抛异常
    # （cli/doctor.py 的 check_bindings 依赖这个契约来报告坏绑定）
    with pytest.raises(re.error):
        Binding(agent="broken", value="[unclosed")
    with pytest.raises(TypeError):
        Binding(agent="legacy", value=["a", "b"])


def test_invalid_bindings_skipped_others_resolve(caplog):
    bindings = [
        {"agent": "broken", "value": "(unclosed"},   # 坏正则 → re.error
        {"agent": "legacy", "value": ["a", "b"]},    # 历史 /route 脏数据（list）→ 非字符串
        {"agent": "pickle", "value": "platform-telegram:.*"},
    ]
    with caplog.at_level("WARNING", logger="ant.core.routing"):
        table = make_table(bindings)
        # 坏绑定被跳过后路由不崩溃，其余绑定与默认回退仍工作
        assert table.resolve("platform-telegram:42") == "pickle"
        assert table.resolve("unknown-source") == "default"

    # 两个坏绑定都不进入路由表
    assert [b.agent for b in table.bindings] == ["pickle"]
    assert len(caplog.records) >= 2


# ---------- (c) tier 排序 ----------

def test_bindings_sorted_by_tier():
    bindings = [
        {"agent": "regex", "value": "platform-(telegram|discord):42"},  # tier 1
        {"agent": "literal", "value": "platform-telegram:42"},          # tier 0
        {"agent": "catchall", "value": ".*"},                           # tier 2
    ]
    table = make_table(bindings)
    assert [b.agent for b in table._load_bindings()] == ["literal", "regex", "catchall"]
    # 同一来源命中多个绑定（literal + 正则 + catchall）时，tier 更小者优先
    assert table.resolve("platform-telegram:42") == "literal"
    assert table.resolve("platform-discord:42") == "regex"
    assert table.resolve("anything-else") == "catchall"


# ---------- (d) persist_binding 收到 str 而非 list ----------

def test_persist_binding_routing_layer_stores_str():
    config = FakeConfig()
    table = RoutingTable(context=SimpleNamespace(config=config))
    table.persist_binding("platform-telegram:.*", "pickle")
    stored = config.routing["bindings"][-1]
    assert stored == {"agent": "pickle", "value": "platform-telegram:.*"}
    assert isinstance(stored["value"], str)


def test_route_command_passes_str_not_list_to_persist_binding():
    # improve.md #1：/route 曾把 parts(list) 传给 persist_binding，
    # 导致绑定 value 变成 list，下次路由必崩
    routing_table = MagicMock()
    ctx = MagicMock()
    ctx.agent_loader.load.return_value = object()  # agent 存在性校验通过
    ctx.routing_table = routing_table
    session = MagicMock()
    session.shared_context = ctx

    result = asyncio.run(RouteCommand().execute("platform-telegram:.* pickle", session))

    assert "Route bound" in result
    args, _ = routing_table.persist_binding.call_args
    assert args[0] == "platform-telegram:.*"
    assert isinstance(args[0], str)  # 关键回归：必须是 str，不能是 list
    assert args[1] == "pickle"
