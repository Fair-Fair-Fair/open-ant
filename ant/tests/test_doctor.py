"""Tests for the `open-ant doctor` self-check logic (pure functions)."""

import sys
from pathlib import Path

import yaml

# 若 editable install（pythonpath=src）未生效，兜底把 src 加进 sys.path
try:
    import ant  # noqa: F401
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import ant  # noqa: F401

from ant.cli.doctor import (  # noqa: E402
    check_bindings,
    check_default_agent,
    check_disk_space,
    check_history_rw,
    load_config_checked,
)
from ant.utils.config import Config  # noqa: E402


def _write_good_config(workspace: Path) -> None:
    """写一份能通过 Config 校验的最小 config.user.yaml。

    sandbox.command.backend 固定为 host，让 doctor 跳过 docker 检查
    （docker 是否可用取决于本机，不该影响配置相关的测试判定）。
    """
    (workspace / "config.user.yaml").write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "provider": "deepseek",
                    "model": "deepseek/deepseek-v4-flash",
                    "api_key": "test-key",
                },
                "default_agent": "pickle",
                "sandbox": {"command": {"backend": "host"}},
            }
        ),
        encoding="utf-8",
    )


# ── check_bindings：binding 编译检查（纯函数） ──────────────────────────


def test_check_bindings_ok():
    bindings = [
        {"agent": "pickle", "value": r"platform-telegram:.*"},
        {"agent": "cookie", "value": r"platform-webSocket:web-.*"},
    ]
    assert check_bindings(bindings) == []


def test_check_bindings_bad_regex():
    bindings = [{"agent": "pickle", "value": "[unclosed"}]
    problems = check_bindings(bindings)
    assert len(problems) == 1
    assert "binding[0]" in problems[0]


def test_check_bindings_list_value():
    """improve.md #1 的 /route bug：list 被当 str 存进 value。"""
    bindings = [{"agent": "pickle", "value": ["platform-telegram", "123"]}]
    problems = check_bindings(bindings)
    assert len(problems) == 1
    assert "expected str" in problems[0]


def test_check_bindings_mixed_good_and_bad():
    """好坏混存时只报坏的那几条，且不抛异常。"""
    bindings = [
        {"agent": "pickle", "value": r"platform-telegram:.*"},
        {"agent": "cookie", "value": 123},
        {"agent": "cookie", "value": "("},
    ]
    problems = check_bindings(bindings)
    assert len(problems) == 2


def test_check_bindings_non_mapping_entry():
    problems = check_bindings(["not-a-dict", 42])
    assert len(problems) == 2
    assert "binding[0]" in problems[0]


# ── doctor 对坏配置的判定 ───────────────────────────────────────────────


def test_load_config_checked_missing(tmp_path):
    config, detail = load_config_checked(tmp_path)
    assert config is None
    assert "not found" in detail


def test_load_config_checked_bad_yaml(tmp_path):
    (tmp_path / "config.user.yaml").write_text(
        "llm:\n  provider: [broken", encoding="utf-8"
    )
    config, detail = load_config_checked(tmp_path)
    assert config is None
    assert "failed to load" in detail


def test_load_config_checked_ok(tmp_path):
    _write_good_config(tmp_path)
    config, detail = load_config_checked(tmp_path)
    assert config is not None
    assert "OK" in detail


def test_check_default_agent_missing(tmp_path):
    """default_agent 指向不存在的 agent → 判定失败。"""
    _write_good_config(tmp_path)  # agents/ 目录不存在
    config = Config.load(tmp_path)
    ok, detail = check_default_agent(config)
    assert ok is False
    assert "pickle" in detail


def test_check_default_agent_ok(tmp_path):
    agent_dir = tmp_path / "agents" / "pickle"
    agent_dir.mkdir(parents=True)
    (agent_dir / "AGENT.md").write_text(
        "---\nname: Pickle\n---\nYou are pickle.\n", encoding="utf-8"
    )
    _write_good_config(tmp_path)
    config = Config.load(tmp_path)
    ok, detail = check_default_agent(config)
    assert ok is True


# ── 磁盘 / history 目录检查 ─────────────────────────────────────────────


def test_check_disk_space_ok(monkeypatch, tmp_path):
    class FakeUsage:
        free = 100 * (1024 ** 3)
        total = 500 * (1024 ** 3)

    monkeypatch.setattr("shutil.disk_usage", lambda _p: FakeUsage())
    ok, detail = check_disk_space(tmp_path)
    assert ok is True
    assert "GiB" in detail


def test_check_disk_space_low(monkeypatch, tmp_path):
    class FakeUsage:
        free = 10 * 1024 * 1024  # 10 MiB —— 低于 1 GiB 阈值
        total = 100 * (1024 ** 3)

    monkeypatch.setattr("shutil.disk_usage", lambda _p: FakeUsage())
    ok, _ = check_disk_space(tmp_path)
    assert ok is False


def test_check_history_rw(tmp_path):
    history = tmp_path / ".history"
    ok, detail = check_history_rw(history)
    assert ok is True
    assert not (history / ".doctor_probe").exists()  # 探测文件已清理


def test_check_history_rw_blocked(tmp_path):
    """探测文件位置被同名目录占据 → 写失败 → 判定失败。"""
    history = tmp_path / ".history"
    history.mkdir(parents=True)
    (history / ".doctor_probe").mkdir()
    ok, detail = check_history_rw(history)
    assert ok is False


# ── 端到端：doctor 命令对坏配置的退出码判定 ────────────────────────────


def test_doctor_command_exit_code_on_bad_config(tmp_path):
    from typer.testing import CliRunner

    from ant.cli.main import app

    (tmp_path / "config.user.yaml").write_text(
        "llm:\n  provider: [broken", encoding="utf-8"
    )
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "-w", str(tmp_path)])
    assert result.exit_code == 1
    assert "config.user.yaml" in result.output
    assert "ERROR" in result.output


def test_doctor_command_exit_code_ok(tmp_path):
    from typer.testing import CliRunner

    from ant.cli.main import app

    agent_dir = tmp_path / "agents" / "pickle"
    agent_dir.mkdir(parents=True)
    (agent_dir / "AGENT.md").write_text(
        "---\nname: Pickle\n---\nYou are pickle.\n", encoding="utf-8"
    )
    _write_good_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "-w", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "All checks passed" in result.output
