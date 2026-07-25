"""Quota management API — rate limits, budgets, model restrictions."""

from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from control_plane.auth.dependencies import get_current_org

router = APIRouter(prefix="/api/quotas", tags=["quotas"])


class QuotaCreate(BaseModel):
    name: str
    scope: str = "gateway"
    scope_id: str = ""
    rate_limit_rpm: int | None = None
    budget_limit_usd: float | None = None
    max_tokens_per_request: int | None = None
    allowed_models: list[str] = Field(default_factory=list)


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


# In-memory store (production would use DB), scoped per org (tenant).
_quotas: dict[str, dict[int, QuotaResponse]] = defaultdict(dict)
_next_id: dict[str, int] = defaultdict(lambda: 1)


@router.get("", response_model=list[QuotaResponse])
async def list_quotas(org: str = Depends(get_current_org)):
    return list(_quotas[org].values())


@router.post("", response_model=QuotaResponse)
async def create_quota(body: QuotaCreate, org: str = Depends(get_current_org)):
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
    return quota


@router.delete("/{quota_id}")
async def delete_quota(quota_id: int, org: str = Depends(get_current_org)):
    if quota_id not in _quotas[org]:
        raise HTTPException(status_code=404, detail="Quota not found")
    del _quotas[org][quota_id]
    return {"deleted": quota_id}


@router.post("/{quota_id}/push")
async def push_quota(quota_id: int, org: str = Depends(get_current_org)):
    """Push this quota to its assigned gateway."""
    import httpx

    quota = _quotas[org].get(quota_id)
    if not quota:
        raise HTTPException(status_code=404, detail="Quota not found")
    if quota.scope != "gateway" or not quota.scope_id:
        return {"status": "skipped", "reason": "Only gateway-scoped quotas can be pushed directly"}

    # Get gateway endpoint from the database
    from control_plane.database import async_session
    from control_plane.models.database import Gateway

    async with async_session() as db:
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

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(f"{gateway.endpoint}/config/quota", json=payload)
            if resp.status_code == 200:
                return {"status": "pushed", "gateway": quota.scope_id, "quota": resp.json()}
            return {"status": "error", "detail": resp.text[:200]}
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))
