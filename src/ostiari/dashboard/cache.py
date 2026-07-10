"""Query cache — Redis-backed with in-memory fallback."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("ostiari.dashboard")


class QueryCache:
    """TTL-based query cache. Uses Redis when available, falls back to in-memory."""

    def __init__(self, redis: Any = None, default_ttl: int = 5) -> None:
        self._redis = redis
        self._default_ttl = default_ttl
        self._local: dict[str, tuple[float, Any]] = {}

    async def get_or_compute(
        self,
        key: str,
        compute: Callable[[], Awaitable[Any]],
        ttl: int | None = None,
    ) -> Any:
        ttl = ttl or self._default_ttl

        if self._redis:
            try:
                cached = await self._redis.get(f"ostiari:cache:{key}")
                if cached:
                    return json.loads(cached)
                result = await compute()
                await self._redis.setex(
                    f"ostiari:cache:{key}", ttl, json.dumps(result, default=str)
                )
                return result
            except Exception as e:
                logger.warning("Redis cache error, falling back to local: %s", e)

        now = time.monotonic()
        if key in self._local:
            expires, value = self._local[key]
            if now < expires:
                return value

        result = await compute()
        self._local[key] = (now + ttl, result)
        return result

    def invalidate(self, key: str) -> None:
        self._local.pop(key, None)
