"""Stats API router."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ostiari.dashboard.cache import QueryCache
from ostiari.dashboard.dependencies import get_cache, get_stats_service
from ostiari.dashboard.services.stats import StatsService

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("")
async def get_stats(
    period: str = "24h",
    stats_service: StatsService = Depends(get_stats_service),
    cache: QueryCache = Depends(get_cache),
) -> dict[str, Any]:
    return await cache.get_or_compute(
        f"stats:{period}",
        lambda: stats_service.aggregate(period),
        ttl=5,
    )


@router.get("/timeseries")
async def get_timeseries(
    period: str = "24h",
    bucket: str = "1h",
    stats_service: StatsService = Depends(get_stats_service),
    cache: QueryCache = Depends(get_cache),
) -> list[dict[str, Any]]:
    return await cache.get_or_compute(
        f"timeseries:{period}:{bucket}",
        lambda: stats_service.timeseries(period, bucket),
        ttl=10,
    )
