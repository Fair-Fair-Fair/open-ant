"""CLI command for ingesting documents into the RAG knowledge base."""

import asyncio
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from ant.core.context import SharedContext
from ant.provider.memory.doc_ingester import DocumentIngester
from ant.utils.config import Config
from ant.utils.logging import setup_logging

console = Console()


def ingest_command(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="File or directory path to ingest"),
) -> None:
    """Ingest documents into the vector knowledge base."""
    config: Config = ctx.obj.get("config")
    setup_logging(config, console_output=False)

    if not config.memory.enabled:
        console.print("[red]Error: Memory system is not enabled. Set memory.enabled: true in config.[/red]")
        raise typer.Exit(1)

    target = Path(path)
    if not target.exists():
        console.print(f"[red]Error: Path does not exist: {path}[/red]")
        raise typer.Exit(1)

    asyncio.run(_do_ingest(config, target))


async def _do_ingest(config: Config, target: Path) -> None:
    """Perform the actual ingestion."""
    context = SharedContext(config=config, channels=[])
    ingester: DocumentIngester | None = context.doc_ingester

    if ingester is None:
        console.print("[red]Error: Document ingester not initialized.[/red]")
        raise typer.Exit(1)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(f"Ingesting {target}...", total=None)

        try:
            if target.is_file():
                count = await ingester.ingest_file(str(target))
                progress.update(task, description=f"Ingested {target.name}")
            else:
                count = await ingester.ingest_directory(str(target))
                progress.update(task, description=f"Ingested directory {target.name}")

        except Exception as e:
            console.print(f"\n[red]Error: {e}[/red]")
            raise typer.Exit(1)

    console.print(f"\n[bold green]Done![/bold green] {count} chunks stored in vector DB.")
