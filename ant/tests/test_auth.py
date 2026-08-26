"""Phase 4A auth tests — no network, no real credentials, no real sockets.

Covers: static token hit / missing-degradation, db_keys=False skipping the
DB, AuthError paths, verify_ws_token 4401 close on a fake websocket, and the
credential discipline rule (the token value never appears in log records).
"""
import logging

import pytest

from ant.server.auth import (
    DEFAULT_USER,
    AuthError,
    require_auth,
    verify_ws_token,
)

# Isolated env var name — never collides with the real .env / process env.
TEST_TOKEN_ENV = "OPEN_ANT_AUTH_TEST_TOKEN"


class FakeAuthConfig:
    def __init__(self, enabled=True, token_env=TEST_TOKEN_ENV, db_keys=True):
        self.enabled = enabled
        self.token_env = token_env
        self.db_keys = db_keys


class FakeApiConfig:
    def __init__(self, auth=None):
        self.auth = auth if auth is not None else FakeAuthConfig()


class FakeConfig:
    def __init__(self, api=None):
        self.api = api if api is not None else FakeApiConfig()


class FakeContext:
    def __init__(self, api=None, session_factory=None):
        self.config = FakeConfig(api)
        self._session_factory = session_factory


class FakeRequest:
    """Minimal stand-in for a fastapi Request / WebSocket token surface."""

    def __init__(self, query=None, headers=None):
        self.query_params = query or {}
        self.headers = headers or {}


class FakeWebSocket(FakeRequest):
    def __init__(self, query=None, headers=None):
        super().__init__(query=query, headers=headers)
        self.closed = None  # (code, reason) when close() was called

    async def close(self, code=1000, reason=None):
        self.closed = (code, reason)


class FakeRow:
    def __init__(self, user_id):
        self._user_id = user_id

    def __getitem__(self, index):
        return self._user_id


class FakeResult:
    def __init__(self, user_id):
        self._user_id = user_id

    def fetchone(self):
        if self._user_id is None:
            return None
        return FakeRow(self._user_id)


class FakeSession:
    """sqlalchemy async-session stand-in; records execute() calls."""

    def __init__(self, user_id, log):
        self._user_id = user_id
        self._log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params):
        # Only the statement text is recorded — never the token parameter.
        self._log.append(("execute", str(stmt)))
        return FakeResult(self._user_id)


class FakeSessionFactory:
    def __init__(self, user_id=None):
        self._user_id = user_id
        self.calls = []  # one entry per factory() invocation
        self.executes = []  # (kind, stmt) recorded by FakeSession

    def __call__(self):
        self.calls.append(1)
        return FakeSession(self._user_id, self.executes)


def _ctx(api=None, session_factory=None) -> FakeContext:
    return FakeContext(api=api, session_factory=session_factory)


# ── require_auth: static token ──────────────────────────────────────────


async def test_static_token_match_returns_local(monkeypatch):
    monkeypatch.setenv(TEST_TOKEN_ENV, "sekret-static-token")
    ctx = _ctx(api=FakeApiConfig(FakeAuthConfig(db_keys=False)))
    user_id = await require_auth(FakeRequest(query={"token": "sekret-static-token"}), ctx)
    assert user_id == DEFAULT_USER


async def test_static_token_mismatch_raises_auth_error(monkeypatch):
    monkeypatch.setenv(TEST_TOKEN_ENV, "sekret-static-token")
    ctx = _ctx(api=FakeApiConfig(FakeAuthConfig(db_keys=False)))
    with pytest.raises(AuthError):
        await require_auth(FakeRequest(query={"token": "wrong-token"}), ctx)


async def test_missing_token_raises_when_credentials_configured(monkeypatch):
    monkeypatch.setenv(TEST_TOKEN_ENV, "sekret-static-token")
    ctx = _ctx(api=FakeApiConfig(FakeAuthConfig(db_keys=False)))
    with pytest.raises(AuthError):
        await require_auth(FakeRequest(), ctx)


# ── require_auth: degraded mode (nothing configured) ────────────────────


async def test_no_credentials_degrades_to_local_with_warning(caplog, monkeypatch):
    monkeypatch.delenv(TEST_TOKEN_ENV, raising=False)
    ctx = _ctx(api=FakeApiConfig(FakeAuthConfig(db_keys=False)))
    with caplog.at_level(logging.WARNING, logger="ant.server.auth"):
        user_id = await require_auth(FakeRequest(), ctx)
    assert user_id == DEFAULT_USER
    assert any("degraded" in r.message for r in caplog.records)


async def test_no_credentials_ignores_presented_token(monkeypatch):
    """Phase 0 compat: with nothing configured, a presented token is not
    judged — everyone is the local user."""
    monkeypatch.delenv(TEST_TOKEN_ENV, raising=False)
    ctx = _ctx(api=FakeApiConfig(FakeAuthConfig(db_keys=False)))
    user_id = await require_auth(FakeRequest(query={"token": "anything"}), ctx)
    assert user_id == DEFAULT_USER


# ── require_auth: api_keys DB path ──────────────────────────────────────


async def test_db_keys_disabled_skips_db(monkeypatch):
    monkeypatch.delenv(TEST_TOKEN_ENV, raising=False)
    factory = FakeSessionFactory(user_id="should-not-be-used")
    ctx = _ctx(api=FakeApiConfig(FakeAuthConfig(db_keys=False)), session_factory=factory)
    user_id = await require_auth(FakeRequest(query={"token": "x"}), ctx)
    assert user_id == DEFAULT_USER
    assert factory.calls == []  # DB never touched


async def test_db_key_hit_returns_user_id(monkeypatch):
    monkeypatch.delenv(TEST_TOKEN_ENV, raising=False)
    factory = FakeSessionFactory(user_id="alice")
    ctx = _ctx(session_factory=factory)
    user_id = await require_auth(FakeRequest(query={"token": "db-key-1"}), ctx)
    assert user_id == "alice"
    assert factory.calls == [1]
    assert factory.executes and factory.executes[0][0] == "execute"


async def test_db_key_miss_raises_auth_error(monkeypatch):
    monkeypatch.delenv(TEST_TOKEN_ENV, raising=False)
    ctx = _ctx(session_factory=FakeSessionFactory(user_id=None))
    with pytest.raises(AuthError):
        await require_auth(FakeRequest(query={"token": "unknown-db-key"}), ctx)


async def test_auth_disabled_returns_local_without_db(monkeypatch):
    monkeypatch.setenv(TEST_TOKEN_ENV, "sekret-static-token")
    factory = FakeSessionFactory(user_id="nope")
    ctx = _ctx(
        api=FakeApiConfig(FakeAuthConfig(enabled=False, db_keys=True)),
        session_factory=factory,
    )
    user_id = await require_auth(FakeRequest(query={"token": "anything"}), ctx)
    assert user_id == DEFAULT_USER
    assert factory.calls == []


# ── verify_ws_token: fake websocket handshake ───────────────────────────


async def test_verify_ws_token_success_from_query(monkeypatch):
    monkeypatch.setenv(TEST_TOKEN_ENV, "sekret-static-token")
    ctx = _ctx(api=FakeApiConfig(FakeAuthConfig(db_keys=False)))
    ws = FakeWebSocket(query={"token": "sekret-static-token"})
    user_id = await verify_ws_token(ws, ctx)
    assert user_id == DEFAULT_USER
    assert ws.closed is None


async def test_verify_ws_token_accepts_sec_websocket_protocol(monkeypatch):
    monkeypatch.setenv(TEST_TOKEN_ENV, "sekret-static-token")
    ctx = _ctx(api=FakeApiConfig(FakeAuthConfig(db_keys=False)))
    ws = FakeWebSocket(headers={"sec-websocket-protocol": "sekret-static-token"})
    user_id = await verify_ws_token(ws, ctx)
    assert user_id == DEFAULT_USER
    assert ws.closed is None


async def test_verify_ws_token_accepts_authorization_header(monkeypatch):
    monkeypatch.setenv(TEST_TOKEN_ENV, "sekret-static-token")
    ctx = _ctx(api=FakeApiConfig(FakeAuthConfig(db_keys=False)))
    ws = FakeWebSocket(headers={"authorization": "Bearer sekret-static-token"})
    user_id = await verify_ws_token(ws, ctx)
    assert user_id == DEFAULT_USER
    assert ws.closed is None


async def test_verify_ws_token_rejects_with_4401(monkeypatch, caplog):
    monkeypatch.setenv(TEST_TOKEN_ENV, "sekret-static-token")
    ctx = _ctx(api=FakeApiConfig(FakeAuthConfig(db_keys=False)))
    ws = FakeWebSocket()  # no token at all
    with caplog.at_level(logging.WARNING, logger="ant.server.auth"):
        user_id = await verify_ws_token(ws, ctx)
    assert user_id is None
    assert ws.closed is not None
    assert ws.closed[0] == 4401


# ── credential discipline: token never reaches logs ─────────────────────


async def test_token_never_appears_in_logs(caplog, monkeypatch):
    token = "sekret-static-token-xyz"
    monkeypatch.setenv(TEST_TOKEN_ENV, token)
    ctx = _ctx(api=FakeApiConfig(FakeAuthConfig(db_keys=False)))
    with caplog.at_level(logging.DEBUG, logger="ant.server.auth"):
        # success path
        await require_auth(FakeRequest(query={"token": token}), ctx)
        # failure paths (mismatch + missing)
        with pytest.raises(AuthError):
            await require_auth(FakeRequest(query={"token": "wrong"}), ctx)
        with pytest.raises(AuthError):
            await require_auth(FakeRequest(), ctx)
        # WS reject path (4401)
        ws = FakeWebSocket(query={"token": "wrong"})
        await verify_ws_token(ws, ctx)
    assert token not in caplog.text
