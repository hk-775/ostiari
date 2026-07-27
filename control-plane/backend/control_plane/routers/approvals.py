"""Human-in-the-loop approval queue.

When the gateway scores a tool call in the `intervene` tier, it does not execute
it — it creates a pending approval here and returns 202 to the agent. A human
reviews the queue and approves or denies; the decision (who, when, why) is
recorded for audit. The agent re-submits the call with the approval id; the
gateway checks the decision here and proceeds only if approved.

This is the "ask a human on the gray cases" workflow — the middle tier made
real. In-memory (like other CP runtime state); a production build would persist
approvals in the DB.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org
from control_plane.database import get_db
from control_plane.models.scoping import org_of_gateway

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


class Approval(BaseModel):
    id: str
    agent_id: str
    gateway_id: str = ""
    action: str
    params: dict = {}
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
    params: dict = {}
    score: int = 0
    reason: str = ""


class Decision(BaseModel):
    decision: str                    # approve | deny
    decided_by: str = "operator"


# In-memory queue, scoped per org (tenant): org -> id -> Approval. The queue
# holds an agent's raw tool params (SQL, recipients, payloads) plus the reviewer's
# identity, so a flat id-keyed dict put one tenant's most sensitive call detail in
# every other tenant's review queue — and let anyone decide it.
_pending: dict[str, dict[str, Approval]] = defaultdict(dict)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
async def create_approval(body: ApprovalCreate, db: AsyncSession = Depends(get_db)) -> Approval:
    """Gateway calls this on an intervene-tier call — record it as pending.

    The reporting gateway has no user token, so the owning org is derived from
    its `gateways` row rather than from the caller or the payload.
    """
    aid = f"apr-{uuid.uuid4().hex[:12]}"
    appr = Approval(
        id=aid, agent_id=body.agent_id, gateway_id=body.gateway_id,
        action=body.action, params=body.params, score=body.score,
        reason=body.reason, status="pending", created_at=_now(),
    )
    _pending[await org_of_gateway(db, body.gateway_id)][aid] = appr
    return appr


@router.get("")
async def list_approvals(status: str | None = None,
                         org: str = Depends(get_current_org)) -> list[Approval]:
    """List approvals; default shows the pending queue (what a human acts on)."""
    items = list(_pending[org].values())
    if status:
        items = [a for a in items if a.status == status]
    else:
        items = [a for a in items if a.status == "pending"]
    return sorted(items, key=lambda a: a.created_at, reverse=True)


@router.get("/all")
async def list_all(org: str = Depends(get_current_org)) -> list[Approval]:
    """Full history (pending + decided) — the audit view."""
    return sorted(_pending[org].values(), key=lambda a: a.created_at, reverse=True)


@router.get("/{approval_id}", response_model=Approval)
async def get_approval(request: Request, approval_id: str,
                       org: str = Depends(get_current_org)) -> Approval:
    found = _find(approval_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    owner_org, appr = found
    _org_guard(None if _tokenless(request.headers) else org, owner_org)
    return appr


@router.post("/{approval_id}/decision", response_model=Approval)
async def decide(request: Request, approval_id: str, body: Decision,
                 org: str = Depends(get_current_org)) -> Approval:
    """Approve or deny a pending call. Records who/when for audit."""
    found = _find(approval_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    owner_org, appr = found
    _org_guard(None if _tokenless(request.headers) else org, owner_org)
    if appr.status != "pending":
        raise HTTPException(status_code=409, detail=f"Already {appr.status}")
    if body.decision not in ("approve", "deny"):
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'deny'")

    appr.status = "approved" if body.decision == "approve" else "denied"
    appr.decided_by = body.decided_by
    appr.decided_at = _now()
    return appr


def approval_status(approval_id: str) -> str | None:
    """Internal helper for the gateway resume-check. None if unknown.

    Ids are globally unique, so this looks up across orgs — it is called on the
    gateway's behalf, which has no user org to scope by.
    """
    found = _find(approval_id)
    return found[1].status if found else None
