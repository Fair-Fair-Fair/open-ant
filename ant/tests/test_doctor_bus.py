"""doctor 的 Phase 1 基础设施检查（纯函数部分）。

只测 check_mysql / check_rabbitmq 的判定逻辑，全部用假 settings 注入，
不真连任何外部服务：
  * 未配置分支（mysql_dsn()/rabbitmq_url() 返回 None → ERROR）
  * 连接失败分支（monkeypatch 假 engine / 假 bus → ERROR 含失败类别）
  * 连接成功分支（假 engine / 假 bus start/stop 成功 → OK）
"""
import pytest

from ant.cli.doctor import (
    PROBE_TIMEOUT_SECONDS,
    _classify_mysql_error,
    check_mysql,
    check_rabbitmq,
)


class FakeSettings:
    """check_* 依赖的 InfraSettings 鸭子类型。"""

    def __init__(self, mysql_dsn=None, rabbitmq_url=None, host="127.0.0.1", port=1):
        self._mysql_dsn = mysql_dsn
        self._rabbitmq_url = rabbitmq_url
        self.mysql_host = host
        self.mysql_port = port
        self.rabbitmq_host = host
        self.rabbitmq_port = port

    def mysql_dsn(self):
        return self._mysql_dsn

    def rabbitmq_url(self):
        return self._rabbitmq_url


# ── check_mysql：未配置分支 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_mysql_not_configured():
    ok, detail = await check_mysql(FakeSettings(mysql_dsn=None))
    assert ok is False
    assert "未配置" in detail
    assert "JSONL" in detail


# ── check_mysql：连接失败分支（假 engine，不真连） ──────────────────────


class _FailingEngine:
    # 镜像真实 SQLAlchemy async engine：connect() 是同步方法（返回 async
    # 上下文管理器）；连接失败在 connect()/__aenter__/execute 时以异常暴露。
    def connect(self):
        raise RuntimeError("boom: connection refused")

    async def dispose(self):
        pass


@pytest.mark.asyncio
async def test_check_mysql_connect_failure(monkeypatch):
    monkeypatch.setattr(
        "ant.cli.doctor.create_engine", lambda dsn: _FailingEngine()
    )
    ok, detail = await check_mysql(
        FakeSettings(mysql_dsn="mysql+asyncmy://u:p@127.0.0.1:1/test")
    )
    assert ok is False
    # 失败类别 + 具体错误都要在详情里（不打码问题）
    assert "MySQL" in detail
    assert "boom" in detail


@pytest.mark.asyncio
async def test_check_mysql_auth_error_classified(monkeypatch):
    class _AuthEngine:
        def connect(self):
            raise RuntimeError("(1045) Access denied for user 'u'@'127.0.0.1'")

        async def dispose(self):
            pass

    monkeypatch.setattr("ant.cli.doctor.create_engine", lambda dsn: _AuthEngine())
    ok, detail = await check_mysql(
        FakeSettings(mysql_dsn="mysql+asyncmy://u:p@127.0.0.1:1/test")
    )
    assert ok is False
    assert "auth" in detail


# ── check_mysql：连接成功分支（假 engine） ──────────────────────────────


class _OkConn:
    async def execute(self, *args, **kwargs):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args, **kwargs):
        return None


class _OkEngine:
    def __init__(self, dsn):
        self.dsn = dsn

    def connect(self):
        return _OkConn()

    async def dispose(self):
        pass


@pytest.mark.asyncio
async def test_check_mysql_ok(monkeypatch):
    monkeypatch.setattr(
        "ant.cli.doctor.create_engine",
        lambda dsn: _OkEngine(dsn),
    )
    ok, detail = await check_mysql(
        FakeSettings(
            mysql_dsn="mysql+asyncmy://u:p@127.0.0.1:3306/open_ant",
            host="127.0.0.1",
            port=3306,
        )
    )
    assert ok is True
    assert "connected to open_ant@127.0.0.1:3306" in detail


# ── check_rabbitmq：未配置分支 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_rabbitmq_not_configured():
    ok, detail = await check_rabbitmq(FakeSettings(rabbitmq_url=None))
    assert ok is False
    assert "未配置" in detail


# ── check_rabbitmq：连接失败 / 成功分支（假 bus，不真连） ───────────────


class _FailingBus:
    def __init__(self, url):
        self.url = url

    async def start(self):
        raise RuntimeError("connection refused by broker")

    async def stop(self):
        pass


class _OkBus:
    def __init__(self, url):
        self.url = url
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_check_rabbitmq_connect_failure(monkeypatch):
    monkeypatch.setattr("ant.cli.doctor.RabbitMqBus", _FailingBus)
    ok, detail = await check_rabbitmq(
        FakeSettings(rabbitmq_url="amqp://guest:guest@127.0.0.1:1/")
    )
    assert ok is False
    assert "connection refused" in detail


@pytest.mark.asyncio
async def test_check_rabbitmq_ok(monkeypatch):
    monkeypatch.setattr("ant.cli.doctor.RabbitMqBus", _OkBus)
    ok, detail = await check_rabbitmq(
        FakeSettings(
            rabbitmq_url="amqp://guest:guest@127.0.0.1:5672/",
            host="127.0.0.1",
            port=5672,
        )
    )
    assert ok is True
    assert "connected to rabbitmq@127.0.0.1:5672" in detail


# ── 失败类别分类器 ──────────────────────────────────────────────────────


def test_classify_mysql_error_categories():
    assert _classify_mysql_error(RuntimeError("Access denied for user")) == "auth"
    assert _classify_mysql_error(RuntimeError("1045")) == "auth"
    assert _classify_mysql_error(RuntimeError("Can't connect to MySQL server")) == "unreachable"
    assert _classify_mysql_error(RuntimeError("(2003)")) == "unreachable"
    assert _classify_mysql_error(RuntimeError("Unknown database 'x'")) == "database-missing"
    assert _classify_mysql_error(RuntimeError("timed out")) == "timeout"
    assert _classify_mysql_error(RuntimeError("mystery")) == "error"


def test_probe_timeout_is_sane():
    assert 1.0 <= PROBE_TIMEOUT_SECONDS <= 30.0
