"""Server CLI command for worker-based architecture."""

import asyncio

import typer

from ant.core.context import SharedContext
from ant.server.server import Server
from ant.utils.logging import setup_logging


def server_command(ctx: typer.Context) -> None:
    """Start the 24/7 server for cron and messagebus execution."""
    config = ctx.obj.get("config")

    setup_logging(config, console_output=True)

    typer.echo("Starting ant server...")
    typer.echo("Press Ctrl+C to stop")

    try:
        context = SharedContext(config)
        asyncio.run(Server(context).run())
    except KeyboardInterrupt:
        typer.echo("\nServer stopped")