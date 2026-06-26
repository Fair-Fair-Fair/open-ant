"""Logging configuration for ant"""

import logging
import sys
from ant.utils.config import Config
from logging.handlers import RotatingFileHandler


def setup_logging(config: Config, console_output: bool = False) -> None:
    """Set up logging for pickle-bot"""
    format_str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    formatter = logging.Formatter(format_str)

    # Console format is simpler (no timestamp)
    console_format = "%(levelname)s - %(name)s - %(message)s"
    console_formatter = logging.Formatter(console_format)

    root_logger = logging.getLogger("ant")
    root_logger.setLevel(logging.DEBUG)

    config.logging_path.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        config.logging_path / "ant.log", maxBytes=256 * 1024 * 128, backupCount=3
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)

    # Optionally log to console (for server mode)
    if console_output:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(console_formatter)
        console_handler.setLevel(logging.INFO)
        root_logger.addHandler(console_handler)