"""Audit log API — view config change history + verify tamper-evidence.

Read access is gated by the global AuthMiddleware (OSTIARI_REQUIRE_AUTH) plus an
explicit admin/operator role check here (defense in depth). Both are no-ops in
the local demo where auth isn't enforced.
"""

import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org, get_current_user
from control_plane.database import get_db
from control_plane.models.database import AuditLog
from control_plane.models.schemas import AuditLogResponse
from control_plane.models.scoping import scoped
from control_plane.services.audit_service import audit

router = APIRouter(prefix="/api/audit", tags=["audit"])


async def _require_audit_reader(request: Request) -> None:
    """Require admin/operator when auth is enforced; no-op in demo mode.

    Evaluated per-request (not at import) so the mode can differ across runs.
    """
    if os.environ.get("OSTIARI_REQUIRE_AUTH", "").lower() not in ("1", "true", "yes", "on"):
        return
    user = await get_current_user(request)
    if user.role not in ("admin", "operator"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="audit access requires admin or operator role")


@router.get("", response_model=list[AuditLogResponse])
async def list_audit_logs(
    request: Request,
    resource_type: str | None = None,
    resource_id: str | None = None,
    actor: str | None = None,
    action: str | None = None,
    limit: int = Query(default=100, le=500),
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    """List audit log entries with optional filters (scoped to the caller's org)."""
    await _require_audit_reader(request)
    query = scoped(select(AuditLog), AuditLog, org).order_by(AuditLog.timestamp.desc()).limit(limit)
    if resource_type:
        query = query.where(AuditLog.resource_type == resource_type)
    if resource_id:
        query = query.where(AuditLog.resource_id == resource_id)
    if actor:
        query = query.where(AuditLog.actor == actor)
    if action:
        query = query.where(AuditLog.action == action)
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/verify")
async def verify_audit_chain(
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    """Verify the audit hash-chain. Returns {valid, checked, [broken_at_id, reason]}."""
    await _require_audit_reader(request)
    return await audit.verify_chain(db, org)
