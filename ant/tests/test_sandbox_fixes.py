"""Tests for sandbox/security fixes (improve.md §1.7 docker, §3 #25/#26).

覆盖：
- #25 builtin_tools 读写 UTF-8 中文文件 round-trip（Windows 默认 GBK 乱码回归）
- #26 read_file 超限截断 + "[Truncated ...]" 提示
- 安全项：sandbox.command.docker_user 配置后，execute_in_docker 的
  docker run 参数必须包含 --user
"""
import asyncio

import pytest

from ant.core.sandbox import CommandSandbox
from ant.tools.builtin_tools import edit_file, read_file, write_file
from ant.utils.config import CommandSandboxConfig, SandboxConfig

# ---------------------------------------------------------------------------
# Stubs — builtin_tools 只依赖 session.shared_context.sandbox.path 的校验方法
# ---------------------------------------------------------------------------


class _FakePathSandbox:
    def validate_read(self, path: str) -> None:
        pass

    def validate_write(self, path: str) -> None:
        pass


class _FakeSandbox:
    def __init__(self) -> None:
        self.path = _FakePathSandbox()


class _FakeSharedContext:
    def __init__(self) -> None:
        self.sandbox = _FakeSandbox()


class _FakeSession:
    def __init__(self) -> None:
        self.shared_context = _FakeSharedContext()
        self.session_id = "test-session"


# ---------------------------------------------------------------------------
# #25 UTF-8 round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_read_edit_utf8_chinese_round_trip(tmp_path):
    """中文内容经 write → read → edit → read 后逐字一致，且落盘字节为 UTF-8。"""
    session = _FakeSession()
    target = tmp_path / "中文文档.txt"
    text = "你好，世界！Hello, 世界。"

    result = await write_file.execute(session, path=str(target), content=text)
    assert "Successfully wrote" in result

    # 落盘字节必须是 UTF-8 编码（GBK 写入会得到不同字节序列）
    assert target.read_bytes() == text.encode("utf-8")

    content = await read_file.execute(session, path=str(target))
    assert content == text

    result = await edit_file.execute(
        session, path=str(target), old_string="世界", new_string="天地"
    )
    assert "Successfully edited" in result

    content = await read_file.execute(session, path=str(target))
    # replace 替换全部出现位置
    assert content == "你好，天地！Hello, 天地。"


# ---------------------------------------------------------------------------
# #26 read_file 大小上限
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_small_file_not_truncated(tmp_path):
    """小于上限的文件原样返回，不带截断提示。"""
    session = _FakeSession()
    target = tmp_path / "small.txt"
    text = "小文件内容"
    target.write_text(text, encoding="utf-8")

    content = await read_file.execute(session, path=str(target))
    assert content == text
    assert "[Truncated" not in content


@pytest.mark.asyncio
async def test_read_file_default_limit_truncates(tmp_path):
    """超过默认上限 50000 时返回截断前缀 + [Truncated ...] 提示。"""
    session = _FakeSession()
    target = tmp_path / "big.txt"
    big = "字" * 60000
    target.write_text(big, encoding="utf-8")

    content = await read_file.execute(session, path=str(target))
    assert "[Truncated" in content
    assert content.startswith(big[:50000])
    assert "limit is 50,000 chars" in content


@pytest.mark.asyncio
async def test_read_file_custom_max_chars(tmp_path):
    """max_chars 参数可调，按指定上限截断并提示。"""
    session = _FakeSession()
    target = tmp_path / "big.txt"
    big = "a" * 1000
    target.write_text(big, encoding="utf-8")

    content = await read_file.execute(session, path=str(target), max_chars=100)
    assert content.startswith("a" * 100)
    assert "[Truncated" in content


# ---------------------------------------------------------------------------
# Docker 沙箱 --user（improve.md §1.7：docstring 声称 non-root 但参数缺失）
# ---------------------------------------------------------------------------


class _FakeProcess:
    """Fake asyncio.subprocess.Process — communicate 立即返回成功。"""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""

    def kill(self) -> None:
        pass


def _capture_docker_args(monkeypatch) -> dict[str, list[str]]:
    """monkeypatch asyncio.create_subprocess_exec，捕获传给 docker 的 args。"""
    captured: dict[str, list[str]] = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = list(args)
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    return captured


@pytest.mark.asyncio
async def test_execute_in_docker_adds_user_flag(monkeypatch, tmp_path):
    """docker_user 配置后，docker run 参数必须包含 --user <docker_user>。"""
    cfg = SandboxConfig(command=CommandSandboxConfig(docker_user="1000:1000"))
    sandbox = CommandSandbox(cfg, tmp_path)

    captured = _capture_docker_args(monkeypatch)
    await sandbox.execute_in_docker("echo hi", session_id="test-session")

    args = captured["args"]
    assert args[0] == "docker"
    assert args[1] == "run"
    assert "--user" in args
    assert args[args.index("--user") + 1] == "1000:1000"


@pytest.mark.asyncio
async def test_execute_in_docker_no_user_by_default(monkeypatch, tmp_path):
    """docker_user 未配置（默认 None）时不加 --user，保持镜像默认用户。"""
    cfg = SandboxConfig(command=CommandSandboxConfig())
    sandbox = CommandSandbox(cfg, tmp_path)

    captured = _capture_docker_args(monkeypatch)
    await sandbox.execute_in_docker("echo hi", session_id="test-session")

    assert "--user" not in captured["args"]
