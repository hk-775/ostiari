"""Human-in-the-loop approval queue.

When the gateway scores a tool call in the `intervene` tier, it does not execute
it — it creates a pending approval here and returns 202 to the agent. A human
reviews the queue and approves or denies; the decision (who, when, why) is
recorded for audit. The agent re-submits the call with the approval id; the
gateway checks the decision here and proceeds only if approved.

This is the "ask a human on the gray cases" workflow — the middle tier made
real. PostgreSQL/SQLite is the source of truth; the small in-memory map remains
only for demo seeding and the synchronous in-process status helper.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org
from control_plane.database import get_db
from control_plane.models.database import ApprovalRecord
from control_plane.models.scoping import org_of_gateway
from control_plane.services.audit_service import actor_of, audit

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


class Approval(BaseModel):
    id: str
    agent_id: str
    gateway_id: str = ""
    action: str
    params: dict = Field(default_factory=dict)
    score: int = 0
    reason: str = ""
    status: str = "pending"          # pending | approved | denied | expired
    decided_by: str = ""
    decided_at: str = ""
    created_at: str = ""


class ApprovalCreate(BaseModel):
    agent_id: str
    gateway_id: str = ""
    action: str
    params: dict = Field(default_factory=dict)
    score: int = 0
    reason: str = ""


class Decision(BaseModel):
    decision: str                    # approve | deny
    # Retained for wire compatibility. The server derives decided_by from the
    # authenticated principal and never trusts this caller-supplied value.
    decided_by: str = ""


# Compatibility cache, scoped per org (tenant): org -> id -> Approval.
_pending: dict[str, dict[str, Approval]] = defaultdict(dict)


def _encrypt_params(params: dict) -> str:
    from control_plane.routers.providers import _encrypt

    return _encrypt(json.dumps(params, sort_keys=True, separators=(",", ":")))


def _decrypt_params(record: ApprovalRecord) -> dict:
    if not record.params_encrypted:
        return {}
    from control_plane.routers.providers import _decrypt

    try:
        value = json.loads(_decrypt(record.params_encrypted))
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Approval parameters cannot be decrypted",
        ) from exc
    return value if isinstance(value, dict) else {}


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _from_record(record: ApprovalRecord) -> Approval:
    return Approval(
        id=record.id,
        agent_id=record.agent_id,
        gateway_id=record.gateway_id,
        action=record.action,
        params=_decrypt_params(record),
        score=record.score,
        reason=record.reason,
        status=record.status,
        decided_by=record.decided_by,
        decided_at=_iso(record.decided_at),
        created_at=_iso(record.created_at),
    )


async def load_approval_cache(db: AsyncSession) -> None:
    """Restore durable approvals for the synchronous status compatibility path."""
    _pending.clear()
    result = await db.execute(select(ApprovalRecord))
    for record in result.scalars():
        approval = _from_record(record)
        _pending[record.org_id][record.id] = approval


async def _record_by_id(db: AsyncSession, approval_id: str) -> ApprovalRecord | None:
    result = await db.execute(
        select(ApprovalRecord).where(ApprovalRecord.id == approval_id)
    )
    return result.scalar_one_or_none()


def _find(approval_id: str) -> tuple[str, Approval] | None:
    """Locate an approval by id across orgs, returning (org, approval).

    Needed because the id-addressed routes are also the gateway's resume-check
    path, and the gateway calls them without a user token (see
    `_org_guard`) — so the org can't come from the caller.
    """
    for org, byid in _pending.items():
        appr = byid.get(approval_id)
        if appr is not None:
            return org, appr
    return None


def _org_guard(request_org: str | None, owner_org: str) -> None:
    """404 when a tokened caller from another org addresses this approval.

    `request_org` is None for a tokenless caller — the gateway reporting or
    polling its own approvals, which has no user token to scope by. Those are
    allowed through (the demo posture, same as trace/usage ingest); a caller that
    *did* present a token is held to its own org.
    """
    if request_org is not None and request_org != owner_org:
        raise HTTPException(status_code=404, detail="Approval not found")


def _tokenless(headers) -> bool:
    """True when no Bearer token was presented — i.e. a gateway, not a human.

    `get_current_org` collapses "no token" to "default", so the org value alone
    can't distinguish a gateway polling its own approval from a default-org user
    reaching for another tenant's. The header is what tells them apart.
    """
    return not headers.get("Authorization", "").startswith("Bearer ")


@router.post("", response_model=Approval)
async def create_approval(
    request: Request,
    body: ApprovalCreate,
    db: AsyncSession = Depends(get_db),
) -> Approval:
    """Gateway calls this on an intervene-tier call — record it as pending.

    The reporting gateway has no user token, so the owning org is derived from
    its `gateways` row rather than from the caller or the payload.
    """
    aid = f"apr-{uuid.uuid4().hex[:12]}"
    owner_org = await org_of_gateway(db, body.gateway_id)
    created_at = datetime.now(timezone.utc)
    record = ApprovalRecord(
        id=aid,
        org_id=owner_org,
        agent_id=body.agent_id,
        gateway_id=body.gateway_id,
        action=body.action,
        params_encrypted=_encrypt_params(body.params),
        score=body.score,
        reason=body.reason,
        status="pending",
        created_at=created_at,
    )
    db.add(record)
    appr = Approval(
        id=aid, agent_id=body.agent_id, gateway_id=body.gateway_id,
        action=body.action, params=body.params, score=body.score,
        reason=body.reason, status="pending", created_at=created_at.isoformat(),
    )
    _pending[owner_org][aid] = appr
    await audit.log(
        db,
        actor_of(request),
        "create",
        "approval",
        aid,
        {
            "gateway_id": body.gateway_id,
            "agent_id": body.agent_id,
            "action": body.action,
            "score": body.score,
        },
        org=owner_org,
    )
    return appr


@router.get("")
async def list_approvals(status: str | None = None,
                         org: str = Depends(get_current_org),
                         db: AsyncSession = Depends(get_db)) -> list[Approval]:
    """List approvals; default shows the pending queue (what a human acts on)."""
    effective_status = status or "pending"
    result = await db.execute(
        select(ApprovalRecord).where(
            ApprovalRecord.org_id == org,
            ApprovalRecord.status == effective_status,
        )
    )
    items_by_id = {
        record.id: _from_record(record)
        for record in result.scalars()
    }
    for approval in _pending[org].values():
        if approval.status == effective_status:
            items_by_id.setdefault(approval.id, approval)
    items = list(items_by_id.values())
    return sorted(items, key=lambda a: a.created_at, reverse=True)


@router.get("/all")
async def list_all(
    org: str = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> list[Approval]:
    """Full history (pending + decided) — the audit view."""
    result = await db.execute(
        select(ApprovalRecord).where(ApprovalRecord.org_id == org)
    )
    items_by_id = {
        record.id: _from_record(record)
        for record in result.scalars()
    }
    for approval in _pending[org].values():
        items_by_id.setdefault(approval.id, approval)
    return sorted(items_by_id.values(), key=lambda a: a.created_at, reverse=True)


@router.get("/{approval_id}", response_model=Approval)
async def get_approval(request: Request, approval_id: str,
                       org: str = Depends(get_current_org),
                       db: AsyncSession = Depends(get_db)) -> Approval:
    record = await _record_by_id(db, approval_id)
    if record is not None:
        _org_guard(None if _tokenless(request.headers) else org, record.org_id)
        return _from_record(record)

    found = _find(approval_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    owner_org, appr = found
    _org_guard(None if _tokenless(request.headers) else org, owner_org)
    return appr


@router.post("/{approval_id}/decision", response_model=Approval)
async def decide(request: Request, approval_id: str, body: Decision,
                 org: str = Depends(get_current_org),
                 db: AsyncSession = Depends(get_db)) -> Approval:
    """Approve or deny a pending call. Records who/when for audit."""
    if body.decision not in ("approve", "deny"):
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'deny'")

    status_value = "approved" if body.decision == "approve" else "denied"
    decided_at = datetime.now(timezone.utc)
    decided_by = actor_of(request)
    existing = await _record_by_id(db, approval_id)
    if existing is not None:
        _org_guard(None if _tokenless(request.headers) else org, existing.org_id)
        result = await db.execute(
            update(ApprovalRecord)
            .where(
                ApprovalRecord.id == approval_id,
                ApprovalRecord.status == "pending",
            )
            .values(
                status=status_value,
                decided_by=decided_by,
                decided_at=decided_at,
            )
            .returning(ApprovalRecord)
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise HTTPException(status_code=409, detail=f"Already {existing.status}")
        appr = _from_record(record)
        _pending[record.org_id][approval_id] = appr
        await audit.log(
            db,
            decided_by,
            status_value,
            "approval",
            approval_id,
            {"gateway_id": record.gateway_id, "action": record.action},
            org=record.org_id,
        )
        return appr

    found = _find(approval_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    owner_org, appr = found
    _org_guard(None if _tokenless(request.headers) else org, owner_org)
    if appr.status != "pending":
        raise HTTPException(status_code=409, detail=f"Already {appr.status}")
    appr.status = status_value
    appr.decided_by = decided_by
    appr.decided_at = decided_at.isoformat()
    await audit.log(
        db,
        decided_by,
        status_value,
        "approval",
        approval_id,
        {"gateway_id": appr.gateway_id, "action": appr.action},
        org=owner_org,
    )
    return appr


def approval_status(approval_id: str) -> str | None:
    """Internal helper for the gateway resume-check. None if unknown.

    Ids are globally unique, so this looks up across orgs — it is called on the
    gateway's behalf, which has no user org to scope by.
    """
    found = _find(approval_id)
    return found[1].status if found else None
