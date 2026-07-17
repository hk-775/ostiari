"""Metering API — governed-call counts and tiering (the billing lens)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane import metering
from control_plane.database import get_db
from control_plane.models.database import UsageRecord

router = APIRouter(prefix="/api/metering", tags=["metering"])

_GROUPS = {"agent", "gateway", "tool"}


async def _records(db: AsyncSession, period_days: int, gateway_id: str | None):
    since = datetime.now(timezone.utc) - timedelta(days=period_days)
    query = select(UsageRecord).where(UsageRecord.timestamp >= since)
    if gateway_id:
        query = query.where(UsageRecord.gateway_id == gateway_id)
    return (await db.execute(query)).scalars().all()


@router.get("/summary")
async def summary(
    group_by: str = Query(default="agent"),
    period_days: int = Query(default=30, le=365),
    gateway_id: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Governed-call metering, grouped by agent | gateway | tool, with tiers."""
    gb = group_by if group_by in _GROUPS else "agent"
    records = await _records(db, period_days, gateway_id)
    result = metering.summarize(records, group_by=gb)
    result["period_days"] = period_days
    result["tiers"] = [{"tier": t, "min_calls": f} for t, f in metering.TIERS]
    return result


@router.get("/export", response_class=PlainTextResponse)
async def export_csv(
    group_by: str = Query(default="agent"),
    period_days: int = Query(default=30, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Export the metering summary as CSV for invoicing/finance systems."""
    gb = group_by if group_by in _GROUPS else "agent"
    records = await _records(db, period_days, None)
    return metering.to_csv(metering.summarize(records, group_by=gb))
