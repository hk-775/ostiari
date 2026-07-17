"""Redis connection with graceful fallback."""

import logging
import os

import redis.asyncio as redis

logger = logging.getLogger(__name__)

REDIS_URL: str | None = os.environ.get("REDIS_URL")

_redis: redis.Redis | None = None


async def get_redis() -> redis.Redis | None:
    """Get Redis connection, or None if not configured/available."""
    global _redis
    if not REDIS_URL:
        return None
    if _redis is None:
        try:
            _redis = redis.from_url(REDIS_URL, decode_responses=True)
            await _redis.ping()
        except Exception as e:
            logger.warning(f"Redis unavailable: {e}")
            _redis = None
            return None
    return _redis


async def close_redis() -> None:
    """Close Redis connection if open."""
    global _redis
    if _redis:
        await _redis.close()
        _redis = None
