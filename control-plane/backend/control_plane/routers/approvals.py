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
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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


# In-memory queue. id -> Approval.
_pending: dict[str, Approval] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("", response_model=Approval)
async def create_approval(body: ApprovalCreate) -> Approval:
    """Gateway calls this on an intervene-tier call — record it as pending."""
    aid = f"apr-{uuid.uuid4().hex[:12]}"
    appr = Approval(
        id=aid, agent_id=body.agent_id, gateway_id=body.gateway_id,
        action=body.action, params=body.params, score=body.score,
        reason=body.reason, status="pending", created_at=_now(),
    )
    _pending[aid] = appr
    return appr


@router.get("")
async def list_approvals(status: str | None = None) -> list[Approval]:
    """List approvals; default shows the pending queue (what a human acts on)."""
    items = list(_pending.values())
    if status:
        items = [a for a in items if a.status == status]
    else:
        items = [a for a in items if a.status == "pending"]
    return sorted(items, key=lambda a: a.created_at, reverse=True)


@router.get("/all")
async def list_all() -> list[Approval]:
    """Full history (pending + decided) — the audit view."""
    return sorted(_pending.values(), key=lambda a: a.created_at, reverse=True)


@router.get("/{approval_id}", response_model=Approval)
async def get_approval(approval_id: str) -> Approval:
    if approval_id not in _pending:
        raise HTTPException(status_code=404, detail="Approval not found")
    return _pending[approval_id]


@router.post("/{approval_id}/decision", response_model=Approval)
async def decide(approval_id: str, body: Decision) -> Approval:
    """Approve or deny a pending call. Records who/when for audit."""
    appr = _pending.get(approval_id)
    if appr is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    if appr.status != "pending":
        raise HTTPException(status_code=409, detail=f"Already {appr.status}")
    if body.decision not in ("approve", "deny"):
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'deny'")

    appr.status = "approved" if body.decision == "approve" else "denied"
    appr.decided_by = body.decided_by
    appr.decided_at = _now()
    return appr


def approval_status(approval_id: str) -> str | None:
    """Internal helper for the gateway resume-check. None if unknown."""
    appr = _pending.get(approval_id)
    return appr.status if appr else None
