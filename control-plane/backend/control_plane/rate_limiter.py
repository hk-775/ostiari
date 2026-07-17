"""Distributed rate limiting via Redis with in-memory fallback."""

import time
from collections import defaultdict

import redis.asyncio as aioredis

from control_plane.redis_client import get_redis


class _InMemoryBucket:
    """Simple in-memory sliding window counter."""

    def __init__(self) -> None:
        self.counts: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str, limit_rpm: int) -> tuple[bool, int]:
        now = time.time()
        window_start = now - 60.0
        # Prune old entries
        self.counts[key] = [t for t in self.counts[key] if t > window_start]
        current = len(self.counts[key])
        if current >= limit_rpm:
            return False, 0
        self.counts[key].append(now)
        return True, limit_rpm - current - 1


_fallback = _InMemoryBucket()


class RateLimiter:
    """Distributed rate limiter using Redis INCR + EXPIRE (sliding window approximation)."""

    def __init__(self, prefix: str = "rl") -> None:
        self.prefix = prefix

    async def check(self, key: str, limit_rpm: int) -> tuple[bool, int]:
        """Check if request is allowed. Returns (allowed, remaining)."""
        r: aioredis.Redis | None = await get_redis()
        if r is None:
            return _fallback.check(key, limit_rpm)

        redis_key = f"{self.prefix}:{key}"
        try:
            pipe = r.pipeline()
            pipe.incr(redis_key)
            pipe.ttl(redis_key)
            results = await pipe.execute()
            current: int = results[0]
            ttl: int = results[1]

            # Set expiry on first request in window
            if ttl == -1:
                await r.expire(redis_key, 60)

            if current > limit_rpm:
                return False, 0
            return True, limit_rpm - current
        except Exception:
            # Fallback to in-memory on Redis error
            return _fallback.check(key, limit_rpm)
