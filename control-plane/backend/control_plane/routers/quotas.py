"""Quota management API — rate limits, budgets, model restrictions."""

import logging
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org
from control_plane.database import get_db
from control_plane.services.audit_service import actor_of, audit

log = logging.getLogger("control_plane.quotas")

router = APIRouter(prefix="/api/quotas", tags=["quotas"])


class QuotaCreate(BaseModel):
    name: str
    scope: str = "gateway"
    scope_id: str = ""
    rate_limit_rpm: int | None = None
    budget_limit_usd: float | None = None
    max_tokens_per_request: int | None = None
    allowed_models: list[str] = Field(default_factory=list)


class QuotaUpdate(BaseModel):
    """Partial update of an existing quota.

    Every field is optional, and the handler applies only the ones actually
    present in the request body (`model_dump(exclude_unset=True)`). That keeps
    `{"budget_limit_usd": null}` — clear the budget cap — distinguishable from a
    body that never mentions the budget, which must leave it alone. A plain
    `None` default can't tell those apart, and the frontend's edit panel sends
    only the fields the operator filled in.
    """

    name: str | None = None
    rate_limit_rpm: int | None = None
    budget_limit_usd: float | None = None
    max_tokens_per_request: int | None = None
    allowed_models: list[str] | None = None


class QuotaResponse(BaseModel):
    id: int
    name: str
    scope: str
    scope_id: str
    rate_limit_rpm: int | None
    budget_limit_usd: float | None
    max_tokens_per_request: int | None
    allowed_models: list[str]
    current_spend: float
    current_rpm: int


class BudgetAlert(BaseModel):
    """A budget threshold crossing reported by a gateway."""

    gateway_id: str = ""
    threshold: str = ""
    spend_usd: float = 0.0
    budget_usd: float = 0.0
    timestamp: float = 0.0


# In-memory store (production would use DB), scoped per org (tenant).
_quotas: dict[str, dict[int, QuotaResponse]] = defaultdict(dict)
_next_id: dict[str, int] = defaultdict(lambda: 1)

# Budget alerts reported by gateways, newest last, per org. Bounded: an alert is
# a notification, not a ledger — the spend itself lives in usage_records. The cap
# is what keeps a chatty fleet from growing this without limit; it is *not* a
# reason to drop the whole deque on restart, which is why app.py's lifespan
# persists it alongside the quotas themselves.
ALERT_HISTORY = 200
_alerts: dict[str, deque[BudgetAlert]] = defaultdict(lambda: deque(maxlen=ALERT_HISTORY))


@router.get("", response_model=list[QuotaResponse])
async def list_quotas(org: str = Depends(get_current_org)):
    return list(_quotas[org].values())


@router.post("", response_model=QuotaResponse)
async def create_quota(
    body: QuotaCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    quota = QuotaResponse(
        id=_next_id[org],
        name=body.name,
        scope=body.scope,
        scope_id=body.scope_id,
        rate_limit_rpm=body.rate_limit_rpm,
        budget_limit_usd=body.budget_limit_usd,
        max_tokens_per_request=body.max_tokens_per_request,
        allowed_models=body.allowed_models,
        current_spend=0.0,
        current_rpm=0,
    )
    _quotas[org][_next_id[org]] = quota
    _next_id[org] += 1
    # Quotas are spend and rate controls — a change here is exactly the kind of
    # thing an audit asks "who loosened this, and when".
    await audit.log(db, actor_of(request), "create", "quota", str(quota.id), {
        "name": quota.name, "scope": quota.scope, "scope_id": quota.scope_id,
        "rate_limit_rpm": quota.rate_limit_rpm,
        "budget_limit_usd": quota.budget_limit_usd,
        "max_tokens_per_request": quota.max_tokens_per_request,
        "allowed_models": quota.allowed_models,
    }, org=org)
    await db.commit()
    return quota


@router.post("/alerts")
async def ingest_budget_alert(body: BudgetAlert):
    """Receive a budget threshold crossing (80/90/100%) from a gateway.

    Unauthenticated gateway path, like payment/approval ingest: the org comes from
    the reporting gateway's row, never from the payload. Kept in memory — an alert
    is a notification whose underlying spend is already recorded in usage_records.
    """
    from control_plane.database import async_session
    from control_plane.models.scoping import org_of_gateway

    async with async_session() as db:
        alert_org = await org_of_gateway(db, body.gateway_id)

    if not body.timestamp:
        body.timestamp = time.time()
    _alerts[alert_org].append(body)
    log.warning(
        "Budget alert from gateway %s: %s ($%.4f / $%.2f)",
        body.gateway_id or "unknown", body.threshold, body.spend_usd, body.budget_usd,
    )
    return {"recorded": True}


@router.get("/alerts", response_model=list[BudgetAlert])
async def list_budget_alerts(org: str = Depends(get_current_org)):
    """Budget alerts reported by this org's gateways, newest first."""
    return list(reversed(_alerts[org]))


@router.delete("/alerts")
async def clear_budget_alerts(org: str = Depends(get_current_org)):
    """Acknowledge (clear) this org's budget alerts."""
    count = len(_alerts[org])
    _alerts[org].clear()
    return {"cleared": count}


@router.put("/{quota_id}", response_model=QuotaResponse)
async def update_quota(
    quota_id: int,
    body: QuotaUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    """Update an existing quota's limits.

    The Quotas page's Edit → Save button has always called this route; until now
    there was no handler, so it answered 405 and the edit was silently discarded —
    the panel closed and the list refetched, which looked exactly like success.

    Declared below the /alerts routes with the other path-parameter routes. It
    can't shadow them (none of them accept PUT), but keeping the literal paths
    first is the rule that stops the next route from doing so.

    Editing a limit does not enforce it. Push the quota afterwards, same as
    creating one.
    """
    quota = _quotas[org].get(quota_id)
    if quota is None:
        raise HTTPException(status_code=404, detail="Quota not found")

    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return quota
    updated = quota.model_copy(update=changes)
    _quotas[org][quota_id] = updated

    # Audited for the same reason create and delete are: these are spend and rate
    # controls, and "who raised this budget" is the question an audit asks.
    await audit.log(db, actor_of(request), "update", "quota", str(quota_id), {
        "name": updated.name, **{k: changes[k] for k in sorted(changes)},
    }, org=org)
    await db.commit()
    return updated


@router.delete("/{quota_id}")
async def delete_quota(
    quota_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    quota = _quotas[org].get(quota_id)
    if quota is None:
        raise HTTPException(status_code=404, detail="Quota not found")
    del _quotas[org][quota_id]
    await audit.log(db, actor_of(request), "delete", "quota", str(quota_id),
                    {"name": quota.name, "scope_id": quota.scope_id}, org=org)
    await db.commit()
    return {"deleted": quota_id}


@router.post("/{quota_id}/push")
async def push_quota(
    quota_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    """Push this quota to its assigned gateway."""
    import httpx

    quota = _quotas[org].get(quota_id)
    if not quota:
        raise HTTPException(status_code=404, detail="Quota not found")
    if quota.scope != "gateway" or not quota.scope_id:
        return {"status": "skipped", "reason": "Only gateway-scoped quotas can be pushed directly"}

    # Get gateway endpoint from the database
    from control_plane.models.database import Gateway

    gateway = await db.get(Gateway, quota.scope_id)
    if not gateway:
        raise HTTPException(status_code=404, detail=f"Gateway '{quota.scope_id}' not found")

    payload = {}
    if quota.rate_limit_rpm is not None:
        payload["rate_limit_rpm"] = quota.rate_limit_rpm
    if quota.budget_limit_usd is not None:
        payload["budget_limit_usd"] = quota.budget_limit_usd
    if quota.max_tokens_per_request is not None:
        payload["max_tokens_per_request"] = quota.max_tokens_per_request
    if quota.allowed_models:
        payload["allowed_models"] = quota.allowed_models

    # Ship the org's model-registry prices so the gateway projects budgets using
    # the same numbers the operator configured. Without this the enforcer fell back
    # to its built-in DEFAULT_PRICING (8 models), so editing a price in the Models
    # page changed the cost dashboard but not what the gateway enforced.
    from control_plane.routers.model_config import pricing_table
    pricing = pricing_table(org)
    if pricing:
        payload["pricing"] = pricing

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(f"{gateway.endpoint}/config/quota", json=payload)
            if resp.status_code == 200:
                # Audit the push, not just the edit: a stored quota that was never
                # pushed enforces nothing, so the two are separate facts.
                await audit.log(db, actor_of(request), "push", "quota", str(quota_id), {
                    "gateway_id": quota.scope_id,
                    "rate_limit_rpm": quota.rate_limit_rpm,
                    "budget_limit_usd": quota.budget_limit_usd,
                    "pricing_models": len(pricing),
                }, org=org)
                await db.commit()
                return {"status": "pushed", "gateway": quota.scope_id, "quota": resp.json()}
            return {"status": "error", "detail": resp.text[:200]}
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))
