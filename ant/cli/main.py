"""CLI interface for my-bot using Typer."""

from pathlib import Path
from typing import Annotated

import typer        # 引入写命令菜单的工具
from rich.console import Console        # 引入让字变好看的工具

from ant.cli.chat import chat_command
from ant.cli.server import server_command
from ant.cli.ingest import ingest_command
from ant.utils.config import Config

app = typer.Typer(
    name="open-ant",            # 菜单板上写着“my-bot 餐厅”
    help="open-ant: Personal AI Assistant",  # 下面小字“个人AI助手”
    no_args_is_help=True,     # 如果客人只喊“服务员”而不说干什么，就自动显示帮助菜单
    add_completion=True,      # 允许按 Tab 键自动补全命令
)

console = Console()


def workspace_callback(ctx: typer.Context, workspace: str) -> Path:
    """Store workspace path in context for later use."""
    ctx.ensure_object(dict)   # 拿出服务员随身带的小本本（上下文）
    ctx.obj["workspace"] = Path(workspace)  # # 把路径转换成 Path 对象，塞进小本本！
    return Path(workspace)      # 顺手返回给 workspace 变量


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
    workspace_path = ctx.obj["workspace"]
    config_file = workspace_path / "config.user.yaml"

    if not config_file.exists():
        console.print(f"[yellow]No configuration found at {config_file}[/yellow]")
        raise typer.Exit(1)

    try:
        cfg = Config.load(workspace_path)
        ctx.obj["config"] = cfg
    except Exception as e:
        console.print(f"[red]Error loading config: {e}[/red]")
        raise typer.Exit(1)


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
) -> None:
    """Start interactive chat session."""
    chat_command(ctx, agent_id=agent)


@app.command("server")
def server(ctx: typer.Context) -> None:
    """Start the 24/7 server for cron and messagebus execution."""
    server_command(ctx)


@app.command("ingest")
def ingest(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="File or directory path to ingest"),
) -> None:
    """Ingest documents into the vector knowledge base for RAG."""
    ingest_command(ctx, path)


if __name__ == "__main__":
    app()
