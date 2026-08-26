"""Server CLI command for worker-based architecture."""

import asyncio

import typer
from rich.console import Console
from rich.text import Text

from ant.core.context import SharedContext
from ant.server.server import Server
from ant.ui.logo import print_logo
from ant.utils.logging import setup_logging


def server_command(ctx: typer.Context) -> None:
    """Start the 24/7 server for cron and messagebus execution."""
    config = ctx.obj.get("config")

    setup_logging(config, console_output=True)

    console = Console()
    print_logo(console)
    console.print(
        Text(
            f"workspace: {config.workspace}  ·  model: {config.llm.model}",
            style="dim",
        )
    )

    # Warn about sandbox/tool consistency before anything runs.
    from ant.core.sandbox import CommandSandbox

    sandbox = CommandSandbox(config.sandbox, config.workspace)
    for warning in sandbox.startup_warnings():
        console.print(f"[yellow]⚠ {warning}[/yellow]")

    console.print("\nStarting workers... Press Ctrl+C to stop\n")

    try:
        context = SharedContext(config)
        asyncio.run(Server(context).run())
    except KeyboardInterrupt:
        typer.echo("\nServer stopped")