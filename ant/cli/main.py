"""CLI interface for open-ant using Typer."""

from pathlib import Path
from typing import Annotated

import typer        # 引入写命令菜单的工具
from rich.console import Console        # 引入让字变好看的工具

from ant.cli.chat import chat_command
from ant.cli.server import server_command
from ant.cli.ingest import ingest_command
from ant.cli.init import init_command
from ant.utils.config import Config

app = typer.Typer(
    name="open-ant",            # 菜单板上写着“open-ant 餐厅”
    help="open-ant: Personal AI Assistant",  # 下面小字“个人AI助手”
    no_args_is_help=True,     # 如果客人只喊“服务员”而不说干什么，就自动显示帮助菜单
    add_completion=True,      # 允许按 Tab 键自动补全命令
)

console = Console()


def workspace_callback(ctx: typer.Context, workspace: str) -> Path:
    """Store workspace path in context for later use."""
    ctx.ensure_object(dict)   # 拿出服务员随身带的小本本（上下文）
    ctx.obj["workspace"] = Path(workspace)  # 把路径转换成 Path 对象，塞进小本本！
    return Path(workspace)      # 顺手返回给 workspace 变量


def override_workspace_callback(ctx: typer.Context, workspace: str | None) -> str | None:
    """Subcommand-level -w: override the global workspace, force config reload.

    Click invokes option callbacks even when the flag is omitted (value is
    the default, None here) — only act when the user actually passed -w,
    otherwise the bare ``open-ant chat`` crashes on ``Path(None)``.
    """
    if workspace is None:
        return None
    ctx.ensure_object(dict)
    ctx.obj["workspace"] = Path(workspace)
    ctx.obj["config"] = None  # invalidate cache — load_config() re-reads
    return workspace


def load_config(ctx: typer.Context) -> Config:
    """Load (and cache) the workspace config; exit with guidance if missing.

    Loading is lazy so ``-w`` works both before AND after the subcommand
    name: ``open-ant chat -w .`` and ``open-ant -w . chat`` are equivalent.
    """
    cfg = ctx.obj.get("config")
    if cfg is not None:
        return cfg

    workspace_path = ctx.obj["workspace"]
    config_file = workspace_path / "config.user.yaml"

    if not config_file.exists():
        console.print(f"[yellow]No configuration found at {config_file}[/yellow]")
        console.print(
            f"Run [bold]open-ant init {workspace_path}[/bold] to create a new workspace here."
        )
        raise typer.Exit(1)

    try:
        cfg = Config.load(workspace_path)
    except Exception as e:
        console.print(f"[red]Error loading config: {e}[/red]")
        raise typer.Exit(1)

    ctx.obj["config"] = cfg
    return cfg


def workspace_option(help_text: str) -> str | None:
    """Common per-command ``-w`` option (overrides the global workspace).

    NOTE: do NOT pass ``None`` as the Option default — typer misparses it
    inside Annotated. The parameter itself carries ``= None`` instead.
    """
    return typer.Option(
        "--workspace",
        "-w",
        help=help_text,
        callback=override_workspace_callback,
    )


@app.callback()
def main(
    ctx: typer.Context,
    workspace: str = typer.Option(
        "./workspace",
        "--workspace",
        "-w",
        help="Path to workspace directory",
        callback=workspace_callback,
    ),
) -> None:
    """Configuration is loaded from workspace/config.user.yaml by default."""
    # Config is loaded lazily by each subcommand via load_config(ctx).


@app.command("chat")
def chat(
    ctx: typer.Context,
    agent: Annotated[
        str | None,
        typer.Option(
            "--agent",
            "-a",
            help="Agent ID to use (overrides default_agent from config)",
        ),
    ] = None,
    workspace: Annotated[str | None, workspace_option("Path to workspace directory")] = None,
) -> None:
    """Start interactive chat session."""
    load_config(ctx)
    chat_command(ctx, agent_id=agent)


@app.command("server")
def server(
    ctx: typer.Context,
    workspace: Annotated[str | None, workspace_option("Path to workspace directory")] = None,
) -> None:
    """Start the 24/7 server for cron and messagebus execution."""
    load_config(ctx)
    server_command(ctx)


@app.command("init")
def init(
    ctx: typer.Context,
    target_dir: Annotated[
        str | None,
        typer.Argument(
            help="Directory to create the workspace in (default: the global -w value)"
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help="Fill missing scaffold files even if a workspace already exists",
        ),
    ] = False,
) -> None:
    """Bootstrap an open-ant workspace in a directory.

    Examples:

        open-ant init            # creates ./workspace
        open-ant init .          # creates the workspace right here
    """
    init_command(target_dir or str(ctx.obj["workspace"]), force=force)


@app.command("ingest")
def ingest(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="File or directory path to ingest"),
    workspace: Annotated[str | None, workspace_option("Path to workspace directory")] = None,
) -> None:
    """Ingest documents into the vector knowledge base for RAG."""
    load_config(ctx)
    ingest_command(ctx, path)


if __name__ == "__main__":
    app()
