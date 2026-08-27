"""Phase 5B — doctor 的 Qdrant/Neo4j 探活检查（纯函数部分）。

与 test_doctor_bus.py 同模式：假 settings 注入，不真连任何外部服务——
未配置分支（ERROR）、连接失败分支（monkeypatch 假 store/graph）、
成功分支（假对象），以及 doctor 表的 SKIP 逻辑。
"""

import pytest

from ant.cli.doctor import check_neo4j, check_qdrant


class FakeSettings:
    """check_* 依赖的 InfraSettings 鸭子类型（方法式 API）。"""

    def __init__(self, qdrant_url=None, qdrant_key=None, neo4j=None):
        self._qdrant_url = qdrant_url
        self._qdrant_key = qdrant_key
        self._neo4j = neo4j or {}

    def qdrant_url(self):
        return self._qdrant_url

    def qdrant_api_key(self):
        return self._qdrant_key

    def masked_qdrant_url(self):
        return self._qdrant_url or "unset"

    def neo4j_uri(self):
        return self._neo4j.get("uri")

    def neo4j_username(self):
        return self._neo4j.get("username")

    def neo4j_password(self):
        return self._neo4j.get("password")

    def neo4j_database(self):
        return self._neo4j.get("database")

    def masked_neo4j_uri(self):
        return self._neo4j.get("uri") or "unset"


# ── Qdrant：未配置 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_qdrant_not_configured():
    ok, detail = await check_qdrant(FakeSettings(qdrant_url=None, qdrant_key=None))
    assert ok is False
    assert "未配置" in detail


@pytest.mark.asyncio
async def test_check_qdrant_missing_key_only():
    ok, detail = await check_qdrant(
        FakeSettings(qdrant_url="https://qdrant.example", qdrant_key=None)
    )
    assert ok is False
    assert "未配置" in detail


# ── Qdrant：连接失败 / 成功（假 store，不真连） ────────────────────────────


class _FailingStore:
    def __init__(self, *a, **kw):
        pass

    @property
    def _client_async(self):
        raise RuntimeError("connection refused (fake)")

    async def get(self, ids):
        return []


class _OkStore:
    def __init__(self, *a, **kw):
        pass

    async def _client_async(self):
        return None

    async def get(self, ids):
        return []


@pytest.mark.asyncio
async def test_check_qdrant_connect_failure(monkeypatch):
    monkeypatch.setattr(
        "ant.cli.doctor.QdrantStore", _FailingStore
    )
    ok, detail = await check_qdrant(
        FakeSettings(qdrant_url="https://qdrant.example", qdrant_key="k")
    )
    assert ok is False
    assert "RuntimeError" in detail or "connection" in detail
    # 凭据纪律：api key 值绝不进 detail
    assert "secret-key-value" not in detail


@pytest.mark.asyncio
async def test_check_qdrant_ok(monkeypatch):
    monkeypatch.setattr("ant.cli.doctor.QdrantStore", _OkStore)
    ok, detail = await check_qdrant(
        FakeSettings(qdrant_url="https://qdrant.example", qdrant_key="k")
    )
    assert ok is True
    assert "qdrant" in detail


# ── Neo4j：未配置 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_neo4j_not_configured():
    ok, detail = await check_neo4j(FakeSettings(neo4j=None))
    assert ok is False
    assert "未配置" in detail


# ── Neo4j：连接失败 / 成功（假 graph，不真连） ─────────────────────────────


class _FailingGraph:
    def __init__(self, *a, **kw):
        class _Driver:
            async def verify_connectivity(self):
                raise RuntimeError("unable to retrieve routing information")

        self._driver = _Driver()

    async def close(self):
        pass


class _OkGraph:
    def __init__(self, *a, **kw):
        class _Driver:
            async def verify_connectivity(self):
                return None

        self._driver = _Driver()

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_check_neo4j_connect_failure(monkeypatch):
    monkeypatch.setattr("ant.cli.doctor.MemoryGraph", _FailingGraph)
    ok, detail = await check_neo4j(
        FakeSettings(
            neo4j={
                "uri": "neo4j+s://x.databases.neo4j.io",
                "username": "neo4j",
                "password": "pw",
                "database": "x",
            }
        )
    )
    assert ok is False
    assert "RuntimeError" in detail
    # 密码绝不进 detail
    assert "pw" not in detail


@pytest.mark.asyncio
async def test_check_neo4j_ok(monkeypatch):
    monkeypatch.setattr("ant.cli.doctor.MemoryGraph", _OkGraph)
    ok, detail = await check_neo4j(
        FakeSettings(
            neo4j={
                "uri": "neo4j+s://x.databases.neo4j.io",
                "username": "neo4j",
                "password": "pw",
                "database": "x",
            }
        )
    )
    assert ok is True
    assert "neo4j" in detail
    assert "pw" not in detail
