"""Shared utilities for loading definition files (agents, skills, crons)."""

import logging
from pathlib import Path
from typing import Any, Callable, TypeVar

import yaml

T = TypeVar("T")
logger = logging.getLogger(__name__)


class DefNotFoundError(Exception):
    """Definition folder or file doesn't exist."""

    def __init__(self, kind: str, def_id: str):
        super().__init__(f"{kind.capitalize()} not found: {def_id}")
        self.kind = kind
        self.def_id = def_id


class InvalidDefError(Exception):
    """Definition file is malformed."""

    def __init__(self, kind: str, def_id: str, reason: str):
        super().__init__(f"Invalid {kind} '{def_id}': {reason}")
        self.kind = kind
        self.def_id = def_id
        self.reason = reason


def parse_definition(
        content: str,
        def_id: str,
        parse_fn: Callable[[str, dict[str, Any], str], T]
) -> T:
    """Parse YAML frontmatter + markdown body with type conversion."""
    # Find frontmatter delimiters
    if not content.startswith("---\n"):
        body = content
        return parse_fn(def_id, {}, body)

    end_delimiter = content.find("\n---\n", 4)
    if end_delimiter == -1:
        body = content
        return parse_fn(def_id, {}, body)

    frontmatter_text = content[4:end_delimiter]
    body = content[end_delimiter + 5:]

    raw_dict = yaml.safe_load(frontmatter_text) or {}
    return parse_fn(def_id, raw_dict, body)


def discover_definitions(
    path: Path,
    filename: str,
    parse_fn: Callable[[str, dict[str, Any], str], T | None],
) -> list[T]:
    """Scan directory for definition files."""
    if not path.exists():
        logger.warning(f"Definitions directory not found: {path}")
        return []

    results = []
    for def_dir in path.iterdir():
        if not def_dir.is_dir():
            continue

        def_file = def_dir / filename
        if not def_file.exists():
            logger.warning(f"No {filename} found in {def_dir.name}")
            continue

        try:
            content = def_file.read_text()
            result = parse_definition(content, def_dir.name, parse_fn)
            if result is not None:
                results.append(result)
        except Exception as e:
            logger.warning(f"Failed to parse {def_dir.name}: {e}")
            continue

    return results


def write_definition(
    def_id: str,
    frontmatter: dict[str, Any],
    body: str,
    base_path: Path,
    filename: str,
) -> Path:
    """Write a definition file with YAML frontmatter and markdown body."""
    def_dir = base_path / def_id
    def_dir.mkdir(parents=True, exist_ok=True)

    # Build file content with YAML frontmatter
    yaml_content = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)
    content = f"---\n{yaml_content}---\n\n{body.strip()}\n"

    def_file = def_dir / filename
    def_file.write_text(content)

    return def_file
