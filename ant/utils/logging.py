"""Logging configuration for ant"""

import logging
import sys
from logging.handlers import RotatingFileHandler

from ant.utils.config import Config

# Third-party libraries that spam the console — keep them at WARNING+.
# (Some of them configure root handlers via basicConfig() at import time;
# ant's own records are isolated from the root logger via propagate=False.)
_NOISY_LIBRARIES = (
    "litellm",
    "chromadb",
    "sentence_transformers",
    "httpx",
    "httpcore",
    "watchdog",
    "langchain",
    "langchain_core",
    "langchain_community",
    "langchain_text_splitters",
)


def setup_logging(config: Config, console_output: bool = False) -> None:
    """Set up logging for ant.

    ant's own records always go to the workspace log file (DEBUG level).
    Console output is opt-in (server mode). Third-party loggers are
    silenced so the CLI UI stays clean.
    """
    format_str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    formatter = logging.Formatter(format_str)

    ant_logger = logging.getLogger("ant")
    ant_logger.setLevel(logging.DEBUG)
    # Isolate ant records from the root logger: third-party libraries may
    # have added root handlers (basicConfig) — don't echo our logs there.
    ant_logger.propagate = False

    # Idempotent: repeated calls (e.g. config reload) must not duplicate handlers.
    has_file = any(isinstance(h, logging.FileHandler) for h in ant_logger.handlers)
    if not has_file:
        config.logging_path.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            config.logging_path / "ant.log", maxBytes=256 * 1024 * 128, backupCount=3
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        ant_logger.addHandler(file_handler)

    if console_output:
        has_console = any(
            isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
            for h in ant_logger.handlers
        )
        if not has_console:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(
                logging.Formatter("%(levelname)s - %(name)s - %(message)s")
            )
            console_handler.setLevel(logging.INFO)
            ant_logger.addHandler(console_handler)

    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)
