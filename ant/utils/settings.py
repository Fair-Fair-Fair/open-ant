"""Infrastructure connection settings (MySQL / RabbitMQ / Redis).

Loaded from environment variables, with ``.env`` file support
(pydantic-settings).  The ``.env`` search order is:

1. current working directory (``./.env``)
2. parent directory of the cwd (repo root, e.g. ``D:/agent/project1/open-ant/.env``)

CREDENTIALS DISCIPLINE
----------------------
This module holds real passwords.  Never log, print, or embed the raw
``MYSQL_PASSWORD`` / ``RABBITMQ_PASSWORD`` values.  Use the ``masked_*``
helpers when a DSN must appear in logs or user-facing output.
"""

from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["InfraSettings"]

_DEFAULT_MYSQL_HOST = "127.0.0.1"
_DEFAULT_MYSQL_PORT = 3306
_DEFAULT_MYSQL_DATABASE = "open_ant"
_DEFAULT_RABBITMQ_HOST = "127.0.0.1"
_DEFAULT_RABBITMQ_PORT = 5672
_DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"


def _find_env_file() -> str | None:
    """Locate the ``.env`` file: cwd first, then the parent directory.

    Returns an absolute path, or ``None`` when no ``.env`` exists in either
    location (pure environment-variable usage then applies).
    """
    cwd = Path.cwd()
    candidates = (cwd / ".env", cwd.parent / ".env")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


class InfraSettings(BaseSettings):
    """Credentials and endpoints for external infrastructure.

    Field names map to uppercase env vars automatically
    (``mysql_username`` <-> ``MYSQL_USERNAME``).

    Defaults reflect the local-first dev layout (Phase 1 design,
    workspace/plan.md): MySQL 127.0.0.1:3306/open_ant, RabbitMQ
    127.0.0.1:5672 vhost ``/``, Redis 127.0.0.1:6379/0.
    """

    model_config = SettingsConfigDict(
        env_file=_find_env_file() or None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── MySQL ───────────────────────────────────────────────────────────
    mysql_username: str | None = None
    mysql_password: str | None = None
    mysql_host: str = _DEFAULT_MYSQL_HOST
    mysql_port: int = _DEFAULT_MYSQL_PORT
    mysql_database: str = _DEFAULT_MYSQL_DATABASE

    # ── RabbitMQ ────────────────────────────────────────────────────────
    rabbitmq_username: str | None = None
    rabbitmq_password: str | None = None
    rabbitmq_host: str = _DEFAULT_RABBITMQ_HOST
    rabbitmq_port: int = _DEFAULT_RABBITMQ_PORT

    # ── Redis ───────────────────────────────────────────────────────────
    redis_url: str = _DEFAULT_REDIS_URL

    # ── Connection-string builders ──────────────────────────────────────

    def mysql_dsn(self) -> str | None:
        """Asyncmy DSN for the application database, or None if incomplete.

        Any missing credential component (username or password) yields
        ``None`` so callers can fall back to a local backend.
        """
        if not self.mysql_username or not self.mysql_password:
            return None
        return self._mysql_dsn_for(self.mysql_database)

    def mysql_server_dsn(self) -> str | None:
        """Asyncmy DSN *without* a database (server-level) — for
        ``CREATE DATABASE`` bootstrap.  None if credentials incomplete."""
        if not self.mysql_username or not self.mysql_password:
            return None
        return self._mysql_dsn_for(None)

    def rabbitmq_url(self) -> str | None:
        """AMQP URL, or None when RabbitMQ credentials are incomplete."""
        if not self.rabbitmq_username or not self.rabbitmq_password:
            return None
        user = quote_plus(self.rabbitmq_username)
        password = quote_plus(self.rabbitmq_password)
        return (
            f"amqp://{user}:{password}@{self.rabbitmq_host}:"
            f"{self.rabbitmq_port}/"
        )

    # ── Masked printing (passwords never leak) ──────────────────────────

    def masked_mysql_dsn(self) -> str:
        """MySQL DSN with the password replaced by ``***`` — safe to log."""
        return self._mysql_dsn_for(self.mysql_database, mask=True)

    def masked_mysql_server_dsn(self) -> str:
        """Server-level MySQL DSN with the password masked."""
        return self._mysql_dsn_for(None, mask=True)

    def masked_rabbitmq_url(self) -> str:
        """AMQP URL with the password replaced by ``***`` — safe to log."""
        return (
            f"amqp://{quote_plus(self.rabbitmq_username) if self.rabbitmq_username else '***'}:"
            f"***@{self.rabbitmq_host}:{self.rabbitmq_port}/"
        )

    def masked_redis_url(self) -> str:
        """Redis URL with any embedded password masked."""
        url = self.redis_url
        if "://" not in url:
            return url
        scheme, _, rest = url.partition("://")
        if "@" in rest:
            authority, _, tail = rest.rpartition("@")
            if ":" in authority:
                authority = authority.split(":", 1)[0] + ":***"
            rest = f"{authority}@{tail}"
        return f"{scheme}://{rest}"

    # ── Internals ───────────────────────────────────────────────────────

    def _mysql_dsn_for(self, database: str | None, mask: bool = False) -> str:
        user = self.mysql_username or ""
        password = self.mysql_password or ""
        if mask:
            user_part = f"{quote_plus(user)}:{'***' if password else ''}"
        else:
            user_part = f"{quote_plus(user)}:{quote_plus(password)}"
        db_part = f"/{database}" if database else "/"
        return (
            f"mysql+asyncmy://{user_part}@{self.mysql_host}:"
            f"{self.mysql_port}{db_part}?charset=utf8mb4"
        )

    def _repr(self, **_: Any) -> str:
        # Never render real secrets in repr/str.
        return (
            f"InfraSettings(mysql_host={self.mysql_host!r}, "
            f"mysql_port={self.mysql_port}, mysql_database={self.mysql_database!r}, "
            f"mysql_configured={bool(self.mysql_username and self.mysql_password)}, "
            f"rabbitmq_host={self.rabbitmq_host!r}, "
            f"rabbitmq_port={self.rabbitmq_port}, "
            f"rabbitmq_configured={bool(self.rabbitmq_username and self.rabbitmq_password)}, "
            f"redis_url={self.masked_redis_url()!r})"
        )

    def __repr__(self) -> str:
        return self._repr()

    def __str__(self) -> str:
        return self._repr()
