"""Traces API router."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ostiari.dashboard.dependencies import get_storage
from ostiari.dashboard.storage_async import AsyncStorageWrapper
from ostiari.models import TraceFilters

router = APIRouter(prefix="/api/traces", tags=["traces"])


@router.get("")
async def list_traces(
    action: str | None = None,
    tier: str | None = None,
    min_score: int | None = None,
    max_score: int | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    correlation_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
    storage: AsyncStorageWrapper = Depends(get_storage),
) -> dict[str, Any]:
    filters = TraceFilters(
        action=action,
        tier=tier,
        min_risk=min_score,
        max_risk=max_score,
        start_time=start_time,
        end_time=end_time,
        correlation_id=correlation_id,
        limit=min(limit, 1000),
        offset=offset,
    )
    traces = await storage.get_traces(filters)
    return {
        "data": [t.model_dump(mode="json") for t in traces],
        "limit": filters.limit,
        "offset": filters.offset,
    }


@router.get("/{trace_id}")
async def get_trace(
    trace_id: str,
    storage: AsyncStorageWrapper = Depends(get_storage),
) -> dict[str, Any]:
    trace = await storage.get_trace(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return trace.model_dump(mode="json")
