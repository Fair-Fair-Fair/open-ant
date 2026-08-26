"""open-ant migrate-chroma — one-time Chroma → Qdrant data migration.

Reads every document from the legacy Chroma collection (via the existing
``ChromaVectorStore.all_documents``), re-embeds in batches (the Qdrant
store uses ``aembed`` internally, with the Redis cache warm on re-runs)
and writes the documents into the Qdrant backend with full payloads.
Progress is printed every 100 documents; the migration is idempotent
(deterministic ids — re-running overwrites instead of duplicating).

Exit codes: 1 when the source Chroma store is unavailable or Qdrant
credentials are missing; per-batch failures are reported but do not abort
the remaining batches (a final non-zero exit reports them).
"""

import asyncio

import typer
from rich.console import Console

from ant.provider.memory.base import EmbeddingProvider
from ant.provider.memory.chroma_store import ChromaVectorStore
from ant.provider.memory.qdrant_store import QdrantStore, QdrantStoreError
from ant.utils.config import Config
from ant.utils.logging import setup_logging
from ant.utils.settings import InfraSettings

console = Console()

# Progress-reporting granularity (documents per batch).
PROGRESS_BATCH = 100


def migrate_chroma_command(ctx: typer.Context) -> None:
    """Run the Chroma → Qdrant migration for the workspace config."""
    config: Config = ctx.obj.get("config")
    setup_logging(config, console_output=False)

    if not config.memory.enabled:
        console.print(
            "[red]Error: memory.enabled is false in config — nothing to migrate.[/red]"
        )
        raise typer.Exit(1)

    asyncio.run(_migrate(config))


async def _migrate(config: Config) -> None:
    embedding_provider = EmbeddingProvider.from_config(config)

    # 1. Source — the legacy Chroma collection must be readable.
    try:
        chroma_store = ChromaVectorStore(config, embedding_provider)
        docs = chroma_store.all_documents()
    except Exception as exc:  # noqa: BLE001 — report the real failure, exit 1
        console.print(f"[red]Source Chroma store unavailable: {exc}[/red]")
        console.print(
            "[red]Nothing was migrated. Fix the local Chroma store "
            "(config.memory.persist_directory) and re-run.[/red]"
        )
        raise typer.Exit(1)

    if not docs:
        console.print("[yellow]Chroma collection is empty — nothing to migrate.[/yellow]")
        return

    # 2. Target — preflight Qdrant credentials so failures are clear early.
    infra = InfraSettings()
    if not infra.qdrant_url() or not infra.qdrant_api_key():
        console.print(
            "[red]Qdrant credentials missing: set QDRANT_URL and QDRANT_API_KEY in .env.[/red]"
        )
        raise typer.Exit(1)
    qdrant_store = QdrantStore(config, embedding_provider)

    console.print(
        f"[bold]Migrating {len(docs)} document(s) from Chroma → Qdrant "
        f"({infra.masked_qdrant_url()})[/bold]"
    )

    failed = 0
    for start in range(0, len(docs), PROGRESS_BATCH):
        batch = docs[start : start + PROGRESS_BATCH]
        end = min(start + PROGRESS_BATCH, len(docs))
        try:
            await qdrant_store.add(
                documents=[d.content for d in batch],
                metadatas=[d.metadata for d in batch],
                ids=[d.id for d in batch],
            )
        except QdrantStoreError as exc:
            console.print(f"[red]Qdrant error at document {start + 1}: {exc}[/red]")
            raise typer.Exit(1)
        except Exception as exc:  # noqa: BLE001 — keep migrating the rest
            failed += len(batch)
            console.print(f"[red]Batch {start + 1}..{end} failed: {exc}[/red]")
            continue
        console.print(f"[green]Migrated {end}/{len(docs)}[/green]")

    if failed:
        console.print(
            f"[yellow]Migration finished with {failed} document(s) failed.[/yellow]"
        )
        raise typer.Exit(1)
    console.print(
        f"[bold green]Migration complete: {len(docs)} document(s) in Qdrant.[/bold green]"
    )
