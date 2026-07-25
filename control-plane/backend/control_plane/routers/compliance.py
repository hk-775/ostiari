"""Compliance report API — maps Ostiari governance data to frameworks (EU AI Act)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane import compliance
from control_plane.auth.dependencies import get_current_org
from control_plane.database import get_db
from control_plane.models.database import AuditLog, Policy
from control_plane.models.scoping import scoped
from control_plane.routers.traces import recent_traces_for

router = APIRouter(prefix="/api/compliance", tags=["compliance"])


@router.get("/frameworks")
async def frameworks() -> Any:
    """List supported compliance frameworks."""
    return {"frameworks": compliance.list_frameworks()}


@router.get("/report")
async def report(
    framework: str = "eu-ai-act",
    period_days: int = 90,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
) -> Any:
    """Generate a compliance report from audit logs, traces, and policies (org-scoped)."""
    if framework not in compliance.list_frameworks():
        raise HTTPException(status_code=400, detail=f"Unknown framework: {framework}")

    since = datetime.now(timezone.utc) - timedelta(days=period_days)

    audit_rows = (
        await db.execute(scoped(select(AuditLog), AuditLog, org).where(AuditLog.timestamp >= since))
    ).scalars().all()

    policy_count = (
        await db.execute(scoped(select(func.count()).select_from(Policy), Policy, org).where(Policy.is_active == True))  # noqa: E712
    ).scalar_one()

    traces = recent_traces_for(org)

    evidence = compliance.build_evidence(audit_rows, traces, policy_count)
    result = compliance.generate_report(framework, evidence)
    result["period_days"] = period_days
    return result
