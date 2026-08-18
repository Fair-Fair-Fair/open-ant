"""OPEN-ANT terminal banner.

Big block-letter logo in ANSI Shadow figlet style, rendered with a
cyan→white RGB gradient.  Rows are composed from per-letter templates
so the word can be rebuilt programmatically.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from rich.console import Console
from rich.text import Text

try:
    __version__ = version("open-ant-harness")
except PackageNotFoundError:
    __version__ = "dev"

TAGLINE = "harness engineering · personal AI agent"

# ── Letter templates (ANSI Shadow style, fixed-width rows) ──
# Each letter is a list of 6 strings of equal width.

_LETTERS: dict[str, list[str]] = {
    "O": [
        " ██████╗ ",
        "██╔═══██╗",
        "██║   ██║",
        "██║   ██║",
        "╚██████╔╝",
        " ╚═════╝ ",
    ],
    "P": [
        "██████╗ ",
        "██╔══██╗",
        "██████╔╝",
        "██╔═══╝ ",
        "██║     ",
        "╚═╝     ",
    ],
    "E": [
        "███████╗",
        "██╔════╝",
        "█████╗  ",
        "██╔══╝  ",
        "███████╗",
        "╚══════╝",
    ],
    "N": [
        "███╗   ██╗",
        "████╗  ██║",
        "██╔██╗ ██║",
        "██║╚██╗██║",
        "██║ ╚████║",
        "╚═╝  ╚═══╝",
    ],
    "A": [
        " █████╗ ",
        "██╔══██╗",
        "███████║",
        "██╔══██║",
        "██║  ██║",
        "╚═╝  ╚═╝",
    ],
    "T": [
        "████████╗",
        "╚══██╔══╝",
        "   ██║   ",
        "   ██║   ",
        "   ██║   ",
        "   ╚═╝   ",
    ],
    "-": [
        " ",
        " ",
        "━",
        " ",
        " ",
        " ",
    ],
}

_ROWS = 6


def _render_word(word: str) -> list[str]:
    """Compose *word* rows from letter templates, one space between letters."""
    rows = ["" for _ in range(_ROWS)]
    for ch in word:
        letter = _LETTERS[ch]
        for i, line in enumerate(letter):
            rows[i] += line + " "
    return [r.rstrip() for r in rows]


# "OPEN" + "-" + "ANT" joined on one line (77 cols, fits in an 80-col terminal)
_LOGO_ROWS = [
    open_row + " " + dash_row + " " + ant_row
    for open_row, dash_row, ant_row in zip(
        _render_word("OPEN"), _render_word("-"), _render_word("ANT")
    )
]


def _gradient_text(line: str, row: int, total_rows: int) -> Text:
    """Color each character along a cyan→white gradient.

    Gradient runs left→right on each row and shifts slightly top→bottom
    so the logo brightens toward the lower right.
    """
    start = (0, 210, 255)    # bright cyan
    end = (255, 255, 255)    # white

    text = Text()
    width = max(len(line) - 1, 1)
    for idx, ch in enumerate(line):
        ratio = (idx + row * 0.6) / max(width + (total_rows - 1) * 0.6, 1)
        ratio = max(0.0, min(ratio, 1.0))
        r = int(start[0] + (end[0] - start[0]) * ratio)
        g = int(start[1] + (end[1] - start[1]) * ratio)
        b = int(start[2] + (end[2] - start[2]) * ratio)
        text.append(ch, style=f"bold rgb({r},{g},{b})")
    return text


def build_banner() -> Text:
    """Build the full logo banner as a Rich Text (logo + tagline)."""
    banner = Text()
    for i, row in enumerate(_LOGO_ROWS):
        banner.append(_gradient_text(row, i, _ROWS))
        banner.append("\n")
    banner.append(f"  {TAGLINE}", style="dim")
    banner.append(f"      v{__version__}", style="dim")
    return banner


def print_logo(console: Console | None = None) -> None:
    """Print the OPEN-ANT logo banner to the terminal."""
    console = console or Console()
    console.print(build_banner())
    console.print()
