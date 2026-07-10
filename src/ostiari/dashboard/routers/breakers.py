"""Breakers API router."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ostiari.dashboard.dependencies import get_storage
from ostiari.dashboard.storage_async import AsyncStorageWrapper
from ostiari.models import MetricType

router = APIRouter(prefix="/api/breakers", tags=["breakers"])


@router.get("")
async def list_breakers(
    storage: AsyncStorageWrapper = Depends(get_storage),
) -> list[dict[str, Any]]:
    results = []
    for metric in MetricType:
        state = await storage.get_breaker_state(metric.value)
        if state is not None:
            results.append(state.model_dump(mode="json"))
    return results


@router.get("/{breaker_id}")
async def get_breaker(
    breaker_id: str,
    storage: AsyncStorageWrapper = Depends(get_storage),
) -> dict[str, Any]:
    state = await storage.get_breaker_state(breaker_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Breaker not found")
    return state.model_dump(mode="json")
