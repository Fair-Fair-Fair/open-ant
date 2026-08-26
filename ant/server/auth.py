"""Phase 4A authentication for the WebSocket / HTTP API.

Validation order (``require_auth``):
  1. Static API token from the environment (``config.api.auth.token_env``,
     e.g. ``OPEN_ANT_API_TOKEN`` in ``.env``). Missing → skipped.
  2. ``api_keys`` table lookup when ``config.api.auth.db_keys`` is enabled
     and the context has a MySQL session factory.
  3. Nothing configured → default user ``"local"`` with a WARNING
     (Phase 0 banner behaviour: loopback-only trust).

CREDENTIALS DISCIPLINE
----------------------
Tokens are never logged.  Only the resolved ``user_id`` and the pass/fail
result appear in log records; error messages never embed the presented or
expected token value.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

if TYPE_CHECKING:
    from ant.core.context import SharedContext

logger = logging.getLogger(__name__)

#: Default identity used when no credentials are configured (single-user
#: local deployment — the "local" banner behaviour from Phase 0).
DEFAULT_USER = "local"

#: WebSocket close code for authentication failure (4401 = custom auth error).
WS_AUTH_CLOSE_CODE = 4401

#: One-time warning guard for the defensive ``api_keys`` lookup.
_db_key_warned = False


class AuthError(Exception):
    """Raised when a request fails authentication."""


def _resolve_token(token_env: str) -> str | None:
    """Read the static API token from the environment (incl. ``.env``).

    Search order mirrors ``InfraSettings`` (``ant/utils/settings.py``):
    process environment first, then the ``.env`` file (cwd → parent).
    The token value is never logged.
    """
    raw = os.environ.get(token_env)
    if raw is not None:
        return raw.strip() or None

    for env_file in (Path.cwd() / ".env", Path.cwd().parent / ".env"):
        if not env_file.is_file():
            continue
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == token_env:
                    value = value.strip().strip('"').strip("'")
                    return value or None
        except OSError:
            return None
    return None


def _extract_token(websocket_or_request: Any) -> str | None:
    """Pull the presented token from a WebSocket or HTTP request.

    Sources, in priority order: ``token`` query parameter →
    ``Sec-WebSocket-Protocol`` header → ``Authorization`` header
    (``Bearer <token>`` or raw value).
    """
    query_params = getattr(websocket_or_request, "query_params", None)
    if query_params is not None:
        token = query_params.get("token")
        if token:
            return str(token).strip() or None

    headers = getattr(websocket_or_request, "headers", None)
    if headers is not None:
        ws_protocol = headers.get("sec-websocket-protocol")
        if ws_protocol:
            for part in ws_protocol.split(","):
                part = part.strip()
                if part:
                    return part

        auth_header = headers.get("authorization")
        if auth_header:
            auth_header = auth_header.strip()
            if auth_header.lower().startswith("bearer "):
                return auth_header[7:].strip()
            return auth_header
    return None


async def _check_api_key(context: "SharedContext", token: str) -> str | None:
    """Look ``token`` up in the ``api_keys`` table.

    BOUNDARY NOTE: ``ant/storage/models.py`` currently defines NO
    ``api_keys`` table — there is no token column, so the static token is
    authoritative and this branch stays dormant.  It is written
    defensively so a future ``api_keys(token, user_id, ...)`` table
    activates it without code changes; any failure (missing table,
    unreachable DB) degrades to the static-token path with a one-time
    warning (design principle 11: availability wins).
    """
    session_factory = getattr(context, "_session_factory", None)
    if session_factory is None:
        return None
    try:
        async with session_factory() as session:
            result = await session.execute(
                text("SELECT user_id FROM api_keys WHERE token = :token LIMIT 1"),
                {"token": token},
            )
            row = result.fetchone()
    except Exception:  # noqa: BLE001 — degrade, never break authentication
        global _db_key_warned
        if not _db_key_warned:
            _db_key_warned = True
            logger.warning(
                "auth: api_keys lookup unavailable (table missing or DB down) "
                "— falling back to the static token only"
            )
        return None
    if row is None:
        return None
    user_id = row[0]
    return user_id if user_id else DEFAULT_USER


def reset_db_key_warning() -> None:
    """Test hook: re-enable the one-time ``api_keys`` warning."""
    global _db_key_warned
    _db_key_warned = False


async def require_auth(websocket_or_request: Any, context: "SharedContext") -> str:
    """Authenticate a WebSocket handshake or HTTP request.

    Returns the authenticated ``user_id``; raises ``AuthError`` when a
    credential is expected but missing or invalid.  When no credentials
    are configured at all (static token empty AND no usable ``api_keys``
    DB), returns ``DEFAULT_USER`` (``"local"``) with a WARNING — the
    Phase 0 banner behaviour (loopback-only trust).
    """
    auth_cfg = getattr(getattr(context.config, "api", None), "auth", None)
    if auth_cfg is None or not getattr(auth_cfg, "enabled", True):
        # Explicit opt-out (or no auth config at all) — nothing to enforce.
        return DEFAULT_USER

    token_env = getattr(auth_cfg, "token_env", "OPEN_ANT_API_TOKEN")
    db_keys = getattr(auth_cfg, "db_keys", True)
    static_token = _resolve_token(token_env)
    session_factory = getattr(context, "_session_factory", None)
    db_usable = bool(db_keys and session_factory is not None)

    if static_token is None and not db_usable:
        logger.warning(
            "auth: no credentials configured (static token env %r empty and "
            "no api_keys DB) — API authentication degraded to loopback-only "
            "trust, default user=%r (Phase 0 banner behaviour)",
            token_env,
            DEFAULT_USER,
        )
        return DEFAULT_USER

    token = _extract_token(websocket_or_request)
    if token is None:
        raise AuthError("missing credentials")

    # 1. Static token (fast path — constant-time comparison).
    if static_token is not None and secrets.compare_digest(token, static_token):
        return DEFAULT_USER

    # 2. api_keys table (only when enabled and a session factory exists).
    if db_usable:
        user_id = await _check_api_key(context, token)
        if user_id is not None:
            return user_id

    raise AuthError("invalid credentials")


async def verify_ws_token(websocket: Any, context: "SharedContext") -> str | None:
    """Validate the token on a WebSocket handshake.

    Reads the token from the ``token`` query parameter or the
    ``Sec-WebSocket-Protocol`` / ``Authorization`` headers.  On failure
    the socket is closed with code 4401 and ``None`` is returned; on
    success the authenticated ``user_id`` is returned.  The token value
    itself never appears in logs — only the result and the user id.
    """
    try:
        user_id = await require_auth(websocket, context)
    except AuthError as exc:
        logger.warning("websocket auth rejected (user_id not resolved): %s", exc)
        try:
            await websocket.close(code=WS_AUTH_CLOSE_CODE, reason="unauthorized")
        except Exception:  # noqa: BLE001 — the close is best-effort
            logger.debug("websocket auth: failed to close rejected socket", exc_info=True)
        return None

    logger.info("websocket authenticated: user=%s", user_id)
    return user_id
