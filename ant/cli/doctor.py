"""open-ant doctor — startup self-check command.

Phase 0 (improve.md §4): 把 §3 里"下次必崩"的配置问题变成"启动即报"。
检查项：
  (a) config.user.yaml 可加载
  (b) routing bindings 每个都能编译（坏绑定列出来但不崩）
  (c) 默认 agent 存在
  (d) docker 可用性（仅 sandbox.command.backend=docker 时）
  (e) 磁盘剩余空间（workspace 所在盘）
  (f) history 目录可读写
  (g) MySQL 连通性（仅 storage.backend=mysql 时；真实 SELECT 1）
  (h) RabbitMQ 连通性（仅 bus.backend=rabbitmq 时；真实连接）
退出码非 0 表示存在 error（供脚本当 pre-flight 门禁用）。
"""

import asyncio
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import text

from ant.bus.rabbitmq import RabbitMqBus
from ant.core.agent_loader import AgentLoader
from ant.core.routing import Binding
from ant.storage.db import create_engine
from ant.utils.config import Config
from ant.utils.settings import InfraSettings

logger = logging.getLogger(__name__)

# 真实探活超时（秒）——避免对不可达主机挂起过久
PROBE_TIMEOUT_SECONDS = 5.0

console = Console()

# 磁盘剩余空间低于该值（GiB）判定为 error
MIN_FREE_DISK_GIB = 1.0

# 结果三元组：名称、状态（True=OK / False=error / None=skip）、详情
CheckResult = tuple[str, bool | None, str]


def check_bindings(bindings_data: list[dict[str, Any]]) -> list[str]:
    """Validate routing bindings against the runtime compile path.

    纯函数：返回问题列表（空 = 全部通过）。
    单个坏绑定只被列出来、不会让整个 doctor 崩掉——
    ``/route`` 命令曾把 list 存进 ``value``（improve.md #1），
    这类非字符串 value 与编译不了的 regex 都会被单独报告。
    """
    problems: list[str] = []
    for i, raw in enumerate(bindings_data):
        if not isinstance(raw, dict):
            problems.append(
                f"binding[{i}]: expected a mapping, got {type(raw).__name__}"
            )
            continue
        agent = raw.get("agent")
        value = raw.get("value")
        label = f"binding[{i}] agent={agent!r}"
        if not isinstance(value, str):
            problems.append(
                f"{label}: value is {type(value).__name__}, expected str"
                " (the /route bug stores a list here)"
            )
            continue
        try:
            Binding(agent=str(agent), value=value)
        except (re.error, TypeError) as e:
            problems.append(f"{label}: invalid pattern {value!r}: {e}")
    return problems


def load_config_checked(workspace: Path) -> tuple[Config | None, str]:
    """Load Config for a workspace; returns (config, detail).

    失败时返回 (None, 原因)——doctor 需要把"配置坏了"本身当作一条
    失败检查项输出，而不是像其他子命令那样直接退出。
    """
    config_file = workspace / "config.user.yaml"
    if not config_file.exists():
        return None, f"config.user.yaml not found at {config_file}"
    try:
        return Config.load(workspace), f"config.user.yaml OK at {config_file}"
    except Exception as e:
        return None, f"config.user.yaml failed to load: {e}"


def check_default_agent(config: Config) -> tuple[bool, str]:
    """检查 default_agent 存在且 AGENT.md 可加载。"""
    agent_id = config.default_agent
    try:
        AgentLoader.from_config(config).load(agent_id)
    except Exception as e:
        return False, f"default_agent={agent_id!r} failed to load: {e}"
    return True, f"default_agent={agent_id!r} loads OK"


def check_docker_available() -> tuple[bool, str]:
    """检查 docker CLI 与 daemon 可用性。"""
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except FileNotFoundError:
        return False, "docker CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "docker version timed out after 10s"
    if result.returncode == 0:
        return True, f"docker daemon OK (server {result.stdout.strip()})"
    detail = (result.stderr.strip() or result.stdout.strip()
              or "docker daemon unreachable")
    return False, detail.splitlines()[-1]


def check_disk_space(
    workspace: Path, min_free_gib: float = MIN_FREE_DISK_GIB
) -> tuple[bool, str]:
    """检查 workspace 所在磁盘的剩余空间。"""
    try:
        usage = shutil.disk_usage(workspace)
    except OSError as e:
        return False, f"cannot stat disk for {workspace}: {e}"
    free_gib = usage.free / (1024 ** 3)
    total_gib = usage.total / (1024 ** 3)
    ok = free_gib >= min_free_gib
    return ok, f"{free_gib:.1f} GiB free of {total_gib:.1f} GiB"


def check_history_rw(history_path: Path) -> tuple[bool, str]:
    """用真实的写 + 读回 round-trip 探测 history 目录可用性。"""
    probe = history_path / ".doctor_probe"
    try:
        history_path.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        read_back = probe.read_text(encoding="utf-8")
    except OSError as e:
        return False, f"history dir {history_path} not usable: {e}"
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
    return read_back == "ok", f"history dir {history_path} readable/writable"


def _classify_mysql_error(exc: Exception) -> str:
    """把底层连接异常归成人类可读的失败类别（不泄露 DSN/密码）。"""
    msg = str(exc)
    lowered = msg.lower()
    if "access denied" in lowered or "1045" in msg:
        return "auth"
    if "can't connect" in lowered or "2003" in msg or "2002" in msg:
        return "unreachable"
    if "unknown database" in lowered or "1049" in msg:
        return "database-missing"
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    return "error"


async def check_mysql(settings: InfraSettings | None = None) -> tuple[bool, str]:
    """MySQL 连通性检查（仅 storage.backend=mysql 时调用）。

    凭据不完整 → ERROR（回退 JSONL）；否则真实 async connect + SELECT 1，
    失败时报失败类别 + 具体错误（不打码问题），成功时报告 host:port。
    *settings* 可注入假对象供单测（纯函数）。
    """
    settings = settings or InfraSettings()
    dsn = settings.mysql_dsn()
    if dsn is None:
        return False, "未配置 MySQL 凭据（.env），回退 JSONL"
    engine = create_engine(dsn)
    try:
        try:
            async with engine.connect() as conn:
                await asyncio.wait_for(
                    conn.execute(text("SELECT 1")), PROBE_TIMEOUT_SECONDS
                )
        except asyncio.TimeoutError:
            return (
                False,
                f"MySQL timeout: no response within {PROBE_TIMEOUT_SECONDS}s",
            )
        except Exception as exc:
            return False, f"MySQL {_classify_mysql_error(exc)}: {exc}"
    finally:
        await engine.dispose()
    return True, f"connected to open_ant@{settings.mysql_host}:{settings.mysql_port}"


async def check_rabbitmq(settings: InfraSettings | None = None) -> tuple[bool, str]:
    """RabbitMQ 连通性检查（仅 bus.backend=rabbitmq 时调用）。

    凭据不完整 → ERROR（回退内存总线）；否则真实连接
    （``RabbitMqBus.start()`` 连接失败会抛异常）。成功时报告 host:port。
    *settings* 可注入假对象供单测（纯函数）。
    """
    settings = settings or InfraSettings()
    url = settings.rabbitmq_url()
    if url is None:
        return False, "未配置 RabbitMQ 凭据（.env），回退内存总线"
    bus = RabbitMqBus(url)
    try:
        try:
            await asyncio.wait_for(bus.start(), PROBE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return (
                False,
                f"RabbitMQ timeout: no response within {PROBE_TIMEOUT_SECONDS}s",
            )
        except Exception as exc:
            return False, f"RabbitMQ connection failed: {exc}"
    finally:
        try:
            await bus.stop()
        except Exception:
            pass
    return True, f"connected to rabbitmq@{settings.rabbitmq_host}:{settings.rabbitmq_port}"


def doctor_command(ctx: typer.Context) -> None:
    """运行启动自检并输出汇总表；存在 error 时退出码非 0。"""
    workspace: Path = ctx.obj["workspace"]

    if not workspace.exists():
        console.print(f"[red]Error: workspace not found: {workspace}[/red]")
        raise typer.Exit(1)

    results: list[CheckResult] = []

    # (a) config.user.yaml 可加载
    config, config_detail = load_config_checked(workspace)
    results.append(("config.user.yaml", config is not None, config_detail))

    if config is not None:
        # (b) routing bindings 每个都能编译（坏绑定列出来但不崩）
        problems = check_bindings(config.routing.get("bindings", []))
        if problems:
            results.append(("routing bindings", False, "; ".join(problems)))
        else:
            results.append(
                ("routing bindings", True,
                 f"{len(config.routing.get('bindings', []))} binding(s) compile")
            )

        # (c) 默认 agent 存在
        results.append(("default agent", *check_default_agent(config)))

        # (d) docker 可用性（仅 backend=docker 时）
        if config.sandbox.command.backend == "docker":
            results.append(("docker", *check_docker_available()))
        else:
            results.append(
                ("docker", None,
                 f"skipped (sandbox.command.backend="
                 f"{config.sandbox.command.backend!r})")
            )

        # (f) history 目录可读写
        results.append(("history dir", *check_history_rw(config.history_path)))

        # (g) MySQL 连通性（仅 storage.backend=mysql 时真实探活）
        if config.storage.backend == "mysql":
            results.append(("mysql", *asyncio.run(check_mysql())))
        else:
            results.append(
                ("mysql", None,
                 f"skipped (storage.backend={config.storage.backend!r})")
            )

        # (h) RabbitMQ 连通性（仅 bus.backend=rabbitmq 时真实探活）
        if config.bus.backend == "rabbitmq":
            results.append(("rabbitmq", *asyncio.run(check_rabbitmq())))
        else:
            results.append(
                ("rabbitmq", None,
                 f"skipped (bus.backend={config.bus.backend!r})")
            )
    else:
        for name, reason in (
            ("routing bindings", "skipped (config failed)"),
            ("default agent", "skipped (config failed)"),
            ("docker", "skipped (config failed)"),
            ("history dir", "skipped (config failed)"),
            ("mysql", "skipped (config failed)"),
            ("rabbitmq", "skipped (config failed)"),
        ):
            results.append((name, None, reason))

    # (e) 磁盘剩余空间（workspace 所在盘，与配置无关，始终检查）
    results.append(("disk space", *check_disk_space(workspace)))

    # 汇总输出
    console.print(f"\n[bold]open-ant doctor — {workspace}[/bold]\n")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Check", style="cyan")
    table.add_column("Status")
    table.add_column("Detail")

    has_error = False
    for name, ok, detail in results:
        if ok is True:
            table.add_row(name, "[green]OK[/green]", detail)
        elif ok is False:
            has_error = True
            table.add_row(name, "[red]ERROR[/red]", detail)
        else:
            table.add_row(name, "[dim]SKIP[/dim]", detail)
    console.print(table)

    if has_error:
        console.print(
            "\n[bold red]✖ doctor found problems — fix them before starting.[/bold red]"
        )
        raise typer.Exit(1)
    console.print("\n[bold green]✔ All checks passed.[/bold green]")
