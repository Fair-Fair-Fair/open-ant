"""Phase 4A sliding-window rate limiting backed by Redis.

Design principle 11 (availability over strictness): when Redis is
unreachable the limiter FAILS OPEN — requests are allowed and a one-time
warning is logged, so a Redis outage never takes the API down.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import redis.asyncio as aioredis

from ant.utils.config import RateLimitConfig
from ant.utils.settings import InfraSettings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "ratelimit:"


class SlidingWindowLimiter:
    """Redis-backed window limiter (``INCR`` + ``EXPIRE NX`` per key).

    A per-key counter is incremented on every ``allow()`` call; the first
    increment within a window arms the TTL (``EXPIRE ... NX`` semantics),
    so the window slides with the first request of each window.
    """

    def __init__(self, config: RateLimitConfig, redis: Any | None = None):
        self._config = config
        self._redis = redis or aioredis.from_url(
            self._resolve_redis_url(config), decode_responses=True
        )
        self._warned = False

    @staticmethod
    def _resolve_redis_url(config: RateLimitConfig) -> str:
        """Redis URL from the configured env var, then InfraSettings
        (which itself reads ``REDIS_URL`` from the environment / ``.env``)."""
        env_name = config.redis_url_env
        if env_name:
            url = os.environ.get(env_name)
            if url:
                return url
        return InfraSettings().redis_url

    def _key(self, suffix: str) -> str:
        return f"{_KEY_PREFIX}{suffix}"

    async def allow(self, key: str) -> bool:
        """True when ``key`` is within the current window budget.

        Never raises: a Redis failure is logged once and the request is
        allowed (fail-open).
        """
        redis_key = self._key(key)
        try:
            count = await self._redis.incr(redis_key)
            if count == 1:
                # First request of the window arms the TTL — equivalent to
                # ``EXPIRE key window NX`` (no-op on subsequent requests).
                await self._redis.expire(
                    redis_key, self._config.window_seconds, nx=True
                )
            return count <= self._config.max_requests_per_window
        except Exception:  # noqa: BLE001 — fail-open on any Redis error
            self._warn_once()
            return True

    async def check_ip_and_user(self, ip: str, user_id: str) -> bool:
        """Rate-limit a request by client IP and by user.

        Allowed only when BOTH keys are within their windows.
        """
        ip_ok = await self.allow(f"ip:{ip}")
        user_ok = await self.allow(f"user:{user_id}")
        return ip_ok and user_ok

    def _warn_once(self) -> None:
        if self._warned:
            return
        self._warned = True
        logger.warning(
            "rate limiter: Redis unreachable (url=%s) — rate limiting disabled, "
            "allowing all requests (fail-open, design principle 11)",
            InfraSettings().masked_redis_url(),
        )

    async def close(self) -> None:
        """Close the underlying Redis connection pool."""
        try:
            await self._redis.aclose()
        except Exception:  # noqa: BLE001 — best-effort shutdown
            logger.debug("rate limiter: Redis close failed", exc_info=True)
