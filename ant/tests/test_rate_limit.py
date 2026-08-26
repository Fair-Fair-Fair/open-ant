"""Phase 4A rate-limiter tests — fake Redis, no network.

Covers: INCR/EXPIRE parameters and window key format, fail-open when Redis
is unreachable (with a one-time warning), ip+user combination, and close().
Uses the real ``RateLimitConfig`` so the config contract is exercised too.
"""
import logging

from ant.server.rate_limit import SlidingWindowLimiter
from ant.utils.config import RateLimitConfig


class FakeRedis:
    """Records INCR/EXPIRE calls; configurable failure (Redis down)."""

    def __init__(self, fail=False):
        self.fail = fail
        self.counts = {}
        self.expires = []  # (key, seconds, nx)
        self.closed = False

    async def incr(self, key):
        if self.fail:
            raise ConnectionError("redis unreachable")
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key, seconds, nx=False):
        self.expires.append((key, seconds, nx))

    async def aclose(self):
        self.closed = True


def _limiter(max_requests=3, redis=None, **overrides) -> SlidingWindowLimiter:
    config = RateLimitConfig(
        enabled=True, window_seconds=60, max_requests_per_window=max_requests, **overrides
    )
    return SlidingWindowLimiter(config, redis=redis)


async def test_window_incr_expire_params_and_block():
    redis = FakeRedis()
    limiter = _limiter(max_requests=3, redis=redis)
    key = "ip:10.0.0.1"

    assert await limiter.allow(key) is True  # count=1
    assert await limiter.allow(key) is True  # count=2
    assert await limiter.allow(key) is True  # count=3
    assert await limiter.allow(key) is False  # count=4 > max

    # INCR happened once per call, on the prefixed sliding-window key.
    assert redis.counts == {"ratelimit:ip:10.0.0.1": 4}
    # EXPIRE with NX semantics is armed exactly once — by the first request.
    assert redis.expires == [("ratelimit:ip:10.0.0.1", 60, True)]


async def test_window_arms_expire_only_on_first_request():
    redis = FakeRedis()
    limiter = _limiter(redis=redis)
    await limiter.allow("user:alice")
    await limiter.allow("user:alice")
    assert redis.expires == [("ratelimit:user:alice", 60, True)]


async def test_fail_open_warns_exactly_once(caplog):
    redis = FakeRedis(fail=True)
    limiter = _limiter(redis=redis)

    with caplog.at_level(logging.WARNING, logger="ant.server.rate_limit"):
        assert await limiter.allow("ip:1.2.3.4") is True
        assert await limiter.allow("ip:1.2.3.4") is True  # still allowed

    warnings = [r for r in caplog.records if "rate limiter" in r.message]
    assert len(warnings) == 1
    assert "fail-open" in warnings[0].message


async def test_check_ip_and_user_requires_both_within_window():
    redis = FakeRedis()
    # max=1：第二次调用（新 IP 同用户）时 user 键计数=2 超限，验证"双键都过才放行"
    limiter = _limiter(max_requests=1, redis=redis)

    # First call: ip + user both at count 1 → allowed.
    assert await limiter.check_ip_and_user("10.0.0.5", "alice") is True
    # Second call from a NEW ip but the SAME user → user over limit.
    assert await limiter.check_ip_and_user("10.0.0.6", "alice") is False
    # Separate user, fresh ip → allowed (independent keys).
    assert await limiter.check_ip_and_user("10.0.0.7", "bob") is True

    assert redis.counts == {
        "ratelimit:ip:10.0.0.5": 1,
        "ratelimit:ip:10.0.0.6": 1,
        "ratelimit:ip:10.0.0.7": 1,
        "ratelimit:user:alice": 2,
        "ratelimit:user:bob": 1,
    }


async def test_close_closes_redis():
    redis = FakeRedis()
    limiter = _limiter(redis=redis)
    await limiter.close()
    assert redis.closed is True


def test_custom_redis_url_env(monkeypatch):
    monkeypatch.setenv("OPEN_ANT_TEST_REDIS_URL", "redis://127.0.0.1:6399/3")
    limiter = _limiter(redis=FakeRedis(), redis_url_env="OPEN_ANT_TEST_REDIS_URL")
    assert (
        limiter._resolve_redis_url(limiter._config)
        == "redis://127.0.0.1:6399/3"
    )


def test_config_contract_defaults():
    """The contract defaults from config.py hold for the limiter."""
    cfg = RateLimitConfig()
    assert cfg.enabled is True
    assert cfg.window_seconds == 60
    assert cfg.max_requests_per_window == 60
    assert cfg.redis_url_env == "REDIS_URL"
