"""`open-ant init` — bootstrap a workspace in a directory.

Creates the standard workspace scaffold (config, agent, bootstrap doc)
so the agent can be started right in a user directory, OpenClaw-style.
"""

from __future__ import annotations

from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ant.ui.logo import print_logo

console = Console()

_CONFIG_TEMPLATE = """\
# Open-Ant user configuration — edit me!
# Provider docs: https://docs.litellm.ai/docs/providers
llm:
  provider: openai                      # litellm provider name
  model: gpt-4o                         # litellm model id, e.g. deepseek/deepseek-chat
  api_key: "sk-REPLACE_ME"              # <-- your API key goes here
  api_base: null                        # optional: custom endpoint, e.g. https://api.deepseek.com
  temperature: 0.7
  max_tokens: 2048

default_agent: pickle

# ── Optional: web search / web read ──
# websearch:
#   provider: tavily
#   api_key: tvly-YOUR_KEY
# webread:
#   provider: crawl4ai

# ── Optional: Telegram / Discord channels ──
# channels:
#   enabled: true
#   telegram:
#     bot_token: YOUR_BOT_TOKEN
#     allowed_user_ids: []

# ── Optional: RAG long-term memory ──
# memory:
#   enabled: true
#   provider: chroma
#   persist_directory: .memory
#   chunk_size: 500              # ~500 tokens/chunk (zh: 1 char ≈ 1 token)
#   chunk_overlap: 50            # overlap window avoids semantic breaks
#   # Hybrid retrieval — vector + BM25 keyword dual index (OpenClaw-aligned)
#   hybrid_enabled: true
#   fusion_mode: rrf             # rrf | weighted (70/30 vector/text)
#   score_threshold: 0.0         # drop weak fused results (0 = off)
#   diversity_by_source: true    # top-1 per source first, then fill
#   reranker: none               # none | cross_encoder (needs local model)

# ── Security (all enabled by default; see README for the full set) ──
sandbox:
  command:
    # "host"   = bash runs directly in this workspace (local-first, like OpenClaw).
    #             read/write/edit and bash all act on the same files — recommended
    #             for a personal agent living in your own directory.
    # "docker" = bash runs in isolated containers. Deployment mode: only use it
    #             when open-ant runs ON the Docker host (or the workspace is kept
    #             in sync), otherwise bash and the file tools see different files.
    backend: host
  path:
    allowed_dirs: []            # extra dirs the agent may access; workspace is always allowed
guardrails:
  input:
    detect_injection: true      # prompt-injection detection (NFKC + mixed-script + 25+ patterns)
  output:
    redact_secrets: true        # 7 secret types auto-redacted
"""

_AGENT_MD = """\
---
name: Ant
description: Your personal AI agent — reads, writes, and runs commands in this workspace.
allow_skills: true
llm:
  temperature: 0.7
  max_tokens: 4096
tool_policy:
  require_confirmation: [bash, write, edit, ingest_document]
---

You are Ant, a personal AI agent that lives in this workspace. You help with daily tasks, coding, files, questions, and creative work.

## Capabilities

- Read, write, and edit files in the workspace
- Run shell commands (sandboxed by policy)
- Search the web and read web pages
- Load skills when appropriate

## Guidelines

- Before deleting or overwriting a file, read it first
- Report outcomes faithfully: if something failed, say so
- Prefer simple, direct solutions
"""

_SOUL_MD = """\
# Personality

You are Ant — diligent, precise, and quietly capable. Work steadily like an ant colony: each task small and well-executed, the whole greater than the parts.
"""

_BOOTSTRAP_MD = """\
# Workspace Bootstrap

This workspace is the agent's home. Files here are safe to read and write.

## Layout

- `agents/` — agent definitions (AGENT.md identity + SOUL.md personality)
- `skills/` — reusable capability skills
- `crons/` — scheduled tasks
- `memories/topics/` — persistent memory files
"""

_EMPTY_DIRS = ["skills", "crons", "memories/topics"]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _read_default_agent(config_file: Path) -> str:
    """Best-effort read of ``default_agent`` from an existing config (raw YAML).

    No validation on purpose — the config may reference anything; init must
    scaffold a matching agent directory, not crash.
    """
    try:
        raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            name = raw.get("default_agent")
            if isinstance(name, str) and name.strip():
                return name.strip()
    except Exception:
        pass
    return "default"


def _template_default_agent() -> str:
    """The default_agent the config template ships with — the fresh
    scaffold must create a matching agents/ directory."""
    try:
        raw = yaml.safe_load(_CONFIG_TEMPLATE)
        if isinstance(raw, dict):
            name = raw.get("default_agent")
            if isinstance(name, str) and name.strip():
                return name.strip()
    except Exception:
        pass
    return "default"


def _scaffold(target: Path, agent_name: str | None = None) -> list[Path]:
    """Create the workspace scaffold. Returns created files (relative paths).

    The agent directory is named after the template's default_agent so a
    fresh workspace is always self-consistent.
    """
    if agent_name is None:
        agent_name = _template_default_agent()
    created: list[Path] = []

    files = [
        (Path("config.user.yaml"), _CONFIG_TEMPLATE),
        (Path("BOOTSTRAP.md"), _BOOTSTRAP_MD),
        (Path(f"agents/{agent_name}/AGENT.md"), _AGENT_MD),
        (Path(f"agents/{agent_name}/SOUL.md"), _SOUL_MD),
    ]
    for rel, content in files:
        dest = target / rel
        _write(dest, content)
        created.append(rel)

    for rel in _EMPTY_DIRS:
        (target / rel).mkdir(parents=True, exist_ok=True)

    return created


def init_command(target_dir: str, force: bool = False) -> None:
    """Bootstrap an open-ant workspace at *target_dir*."""
    print_logo(console)

    target = Path(target_dir)
    config_file = target / "config.user.yaml"

    if config_file.exists() and not force:
        console.print(
            f"[yellow]A workspace already exists at {target.resolve()}[/yellow]\n"
            "Use --force to re-create the scaffold (won't touch your config)."
        )
        raise typer.Exit(1)

    if force and config_file.exists():
        # Keep the user's config; only fill missing scaffold files.
        # The agent directory follows the config's default_agent so the
        # scaffold matches whatever the user's config actually references.
        agent_name = _read_default_agent(config_file)
        scaffold_files = {
            "BOOTSTRAP.md": _BOOTSTRAP_MD,
            f"agents/{agent_name}/AGENT.md": _AGENT_MD,
            f"agents/{agent_name}/SOUL.md": _SOUL_MD,
        }
        created = []
        for rel_str, content in scaffold_files.items():
            dest = target / rel_str
            if not dest.exists():
                _write(dest, content)
                created.append(Path(rel_str))
        for rel in _EMPTY_DIRS:
            (target / rel).mkdir(parents=True, exist_ok=True)
        console.print(f"[dim]Existing config kept; added {len(created)} missing file(s).[/dim]\n")
    else:
        created = _scaffold(target)

    # ── Done panel ──
    lines = "\n".join(f"  {rel}" for rel in created)
    console.print(
        Panel(
            Text(
                f"workspace created at {target.resolve()}\n\n{lines}\n"
                f"+ {len(_EMPTY_DIRS)} empty dirs (skills/, crons/, memories/topics/)",
                style="bold bright_white",
            ),
            title="[bold cyan]ANT COLONY ESTABLISHED[/bold cyan]",
            border_style="cyan",
        )
    )

    # ── Next steps ──
    console.print("[bold]Next steps:[/bold]")
    console.print(
        f"  1. Edit [cyan]{config_file}[/cyan] and set your [bold]api_key[/bold]\n"
        f"  2. Start chatting: [bold]open-ant chat -w {target}[/bold]\n"
        f"  3. Or run the 24/7 server: [bold]open-ant server -w {target}[/bold]\n"
    )
