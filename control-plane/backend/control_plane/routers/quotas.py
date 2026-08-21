"""Quota management API — rate limits, budgets, model restrictions."""

import logging
import time
import uuid
from collections import defaultdict, deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org
from control_plane.auth.workload import authorize_reported_gateway
from control_plane.database import get_db
from control_plane.models.database import Gateway, UsageRecord
from control_plane.models.scoping import get_scoped, scoped
from control_plane.services.audit_service import actor_of, audit
from control_plane.services.push_service import gateway_config_headers
from control_plane.services.runtime_state import (
    allocate_runtime_id,
    clear_runtime_namespace,
    delete_runtime_state,
    put_runtime_state,
    put_runtime_state_once,
)

log = logging.getLogger("control_plane.quotas")

router = APIRouter(prefix="/api/quotas", tags=["quotas"])


class QuotaCreate(BaseModel):
    name: str
    scope: str = "gateway"
    scope_id: str = ""
    gateway_id: str = ""
    rate_limit_rpm: int | None = Field(default=None, gt=0)
    budget_limit_usd: float | None = Field(default=None, ge=0)
    max_tokens_per_request: int | None = Field(default=None, gt=0)
    allowed_models: list[str] = Field(default_factory=list)
    allowed_providers: list[str] = Field(default_factory=list)
    alert_threshold_pct: int = Field(default=90, ge=1, le=100)


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
    gateway_id: str | None = None
    rate_limit_rpm: int | None = Field(default=None, gt=0)
    budget_limit_usd: float | None = Field(default=None, ge=0)
    max_tokens_per_request: int | None = Field(default=None, gt=0)
    allowed_models: list[str] | None = None
    allowed_providers: list[str] | None = None
    alert_threshold_pct: int | None = Field(default=None, ge=1, le=100)


class QuotaResponse(BaseModel):
    id: int
    name: str
    scope: str
    scope_id: str
    gateway_id: str = ""
    rate_limit_rpm: int | None
    budget_limit_usd: float | None
    max_tokens_per_request: int | None
    allowed_models: list[str]
    allowed_providers: list[str] = Field(default_factory=list)
    alert_threshold_pct: int = 90
    current_spend: float
    current_rpm: int


class BudgetAlert(BaseModel):
    """A budget threshold crossing reported by a gateway."""

    event_id: str = Field(default="", max_length=64)
    gateway_id: str = ""
    agent_id: str = ""
    threshold: str = ""
    spend_usd: float = 0.0
    budget_usd: float = 0.0
    timestamp: float = 0.0


# Hot cache over runtime_state_records, scoped per org (tenant).
_quotas: dict[str, dict[int, QuotaResponse]] = defaultdict(dict)
_next_id: dict[str, int] = defaultdict(lambda: 1)

# Budget alerts reported by gateways, newest last, per org. Bounded: an alert is
# a notification, not a ledger — the spend itself lives in usage_records. The cap
# is what keeps a chatty fleet from growing this without limit. SQL remains the
# source of truth across restarts.
ALERT_HISTORY = 200
_alerts: dict[str, deque[BudgetAlert]] = defaultdict(lambda: deque(maxlen=ALERT_HISTORY))


def _agent_gateway(quota: QuotaResponse, org: str) -> str:
    """Resolve an agent quota's gateway without making registration mandatory."""
    if quota.gateway_id:
        return quota.gateway_id
    from control_plane.routers.agents import _agents

    agent = _agents[org].get(quota.scope_id)
    return agent.gateway_id if agent else ""


async def _usage_metrics(
    db: AsyncSession, org: str
) -> tuple[dict[str, tuple[float, int]], dict[tuple[str, str], tuple[float, int]]]:
    """Aggregate actual spend and trailing-minute request volume."""
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    gateway_rows = (
        await db.execute(
            select(Gateway.id, Gateway.config).where(Gateway.org_id == org)
        )
    ).all()
    reset_epochs: dict[str, datetime] = {}
    for gateway_id, config in gateway_rows:
        raw = (config or {}).get("budget_reset", {}).get("last_reset_at")
        if not raw:
            continue
        try:
            epoch = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if epoch.tzinfo is None:
                epoch = epoch.replace(tzinfo=timezone.utc)
            reset_epochs[gateway_id] = epoch
        except (TypeError, ValueError):
            log.warning("Ignoring invalid budget reset timestamp for %s: %r", gateway_id, raw)

    spend_query = select(
        UsageRecord.gateway_id,
        UsageRecord.agent_id,
        func.sum(UsageRecord.cost_usd),
    )
    if reset_epochs:
        reset_gateways = list(reset_epochs)
        spend_query = spend_query.where(or_(
            UsageRecord.gateway_id.not_in(reset_gateways),
            *[
                and_(
                    UsageRecord.gateway_id == gateway_id,
                    UsageRecord.timestamp >= epoch,
                )
                for gateway_id, epoch in reset_epochs.items()
            ],
        ))
    spend_rows = (
        await db.execute(
            scoped(
                spend_query.group_by(
                    UsageRecord.gateway_id,
                    UsageRecord.agent_id,
                ),
                UsageRecord,
                org,
            )
        )
    ).all()
    rpm_rows = (await db.execute(
        scoped(
            select(
                UsageRecord.gateway_id,
                UsageRecord.agent_id,
                func.count(UsageRecord.id),
            ).where(UsageRecord.timestamp >= since)
            .group_by(UsageRecord.gateway_id, UsageRecord.agent_id),
            UsageRecord,
            org,
        )
    )).all()

    agent_metrics: dict[tuple[str, str], tuple[float, int]] = {}
    gateway_metrics: dict[str, tuple[float, int]] = {}
    rpm_by_agent = {(gateway, agent): int(count) for gateway, agent, count in rpm_rows}
    for gateway, agent, spend in spend_rows:
        key = (gateway, agent)
        cost = float(spend or 0.0)
        rpm = rpm_by_agent.get(key, 0)
        agent_metrics[key] = (cost, rpm)
        gateway_cost, gateway_rpm = gateway_metrics.get(gateway, (0.0, 0))
        gateway_metrics[gateway] = (gateway_cost + cost, gateway_rpm + rpm)

    # A recent request with zero cost still counts toward RPM.
    for key, rpm in rpm_by_agent.items():
        if key not in agent_metrics:
            agent_metrics[key] = (0.0, rpm)
        gateway, _agent = key
        if gateway not in gateway_metrics:
            gateway_metrics[gateway] = (0.0, rpm)
    return gateway_metrics, agent_metrics


async def _with_actual_metrics(
    quotas: list[QuotaResponse], db: AsyncSession, org: str
) -> list[QuotaResponse]:
    gateway_metrics, agent_metrics = await _usage_metrics(db, org)
    out: list[QuotaResponse] = []
    for quota in quotas:
        if quota.scope == "gateway":
            spend, rpm = gateway_metrics.get(quota.scope_id, (0.0, 0))
        elif quota.scope == "agent":
            gateway_id = _agent_gateway(quota, org)
            if gateway_id:
                spend, rpm = agent_metrics.get((gateway_id, quota.scope_id), (0.0, 0))
            else:
                matching = [
                    values for (gateway, agent), values in agent_metrics.items()
                    if agent == quota.scope_id
                ]
                spend = sum(values[0] for values in matching)
                rpm = sum(values[1] for values in matching)
        else:
            spend, rpm = quota.current_spend, quota.current_rpm
        out.append(quota.model_copy(update={
            "gateway_id": _agent_gateway(quota, org) if quota.scope == "agent" else quota.gateway_id,
            "current_spend": round(spend, 6),
            "current_rpm": rpm,
        }))
    return out


@router.get("", response_model=list[QuotaResponse])
async def list_quotas(
    scope: str | None = None,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    quotas = list(_quotas[org].values())
    if scope:
        quotas = [quota for quota in quotas if quota.scope == scope]
    return await _with_actual_metrics(quotas, db, org)


@router.post("", response_model=QuotaResponse)
async def create_quota(
    body: QuotaCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    gateway_id = body.gateway_id
    if body.scope == "agent":
        from control_plane.routers.agents import _agents

        gateway_id = gateway_id or (
            _agents[org].get(body.scope_id).gateway_id
            if _agents[org].get(body.scope_id)
            else ""
        )
        if not body.scope_id or not gateway_id:
            raise HTTPException(
                status_code=422,
                detail="Agent quotas require an agent scope_id and gateway_id",
            )
        duplicate = next((
            quota for quota in _quotas[org].values()
            if quota.scope == "agent"
            and quota.scope_id == body.scope_id
            and _agent_gateway(quota, org) == gateway_id
        ), None)
        if duplicate:
            raise HTTPException(
                status_code=409,
                detail=f"Agent quota already exists for '{body.scope_id}' on '{gateway_id}'",
            )

    quota_id = await allocate_runtime_id(db, org, "quotas")
    quota = QuotaResponse(
        id=quota_id,
        name=body.name,
        scope=body.scope,
        scope_id=body.scope_id,
        gateway_id=gateway_id,
        rate_limit_rpm=body.rate_limit_rpm,
        budget_limit_usd=body.budget_limit_usd,
        max_tokens_per_request=body.max_tokens_per_request,
        allowed_models=body.allowed_models,
        allowed_providers=body.allowed_providers,
        alert_threshold_pct=body.alert_threshold_pct,
        current_spend=0.0,
        current_rpm=0,
    )
    _quotas[org][quota_id] = quota
    _next_id[org] = max(_next_id[org], quota_id + 1)
    await put_runtime_state(
        db,
        org,
        "quotas",
        str(quota_id),
        quota.model_dump(mode="json"),
    )
    # Quotas are spend and rate controls — a change here is exactly the kind of
    # thing an audit asks "who loosened this, and when".
    await audit.log(db, actor_of(request), "create", "quota", str(quota.id), {
        "name": quota.name, "scope": quota.scope, "scope_id": quota.scope_id,
        "rate_limit_rpm": quota.rate_limit_rpm,
        "budget_limit_usd": quota.budget_limit_usd,
        "max_tokens_per_request": quota.max_tokens_per_request,
        "allowed_models": quota.allowed_models,
        "allowed_providers": quota.allowed_providers,
        "alert_threshold_pct": quota.alert_threshold_pct,
        "gateway_id": quota.gateway_id,
    }, org=org)
    await db.commit()
    return quota


@router.post("/alerts")
async def ingest_budget_alert(request: Request, body: BudgetAlert):
    """Receive a budget threshold crossing (80/90/100%) from a gateway.

    Unauthenticated gateway path, like payment/approval ingest: the org comes from
    the reporting gateway's row, never from the payload. Alerts are stored
    immutably by event ID and restored into the bounded hot cache after restarts.
    """
    from control_plane.database import async_session
    async with async_session() as db:
        gateway = await authorize_reported_gateway(request, db, body.gateway_id)
        alert_org = gateway.org_id if gateway is not None else "default"
        if not body.event_id:
            body.event_id = uuid.uuid4().hex
        if not body.timestamp:
            body.timestamp = time.time()
        payload = body.model_dump(mode="json")
        inserted, stored = await put_runtime_state_once(
            db,
            alert_org,
            "budget_alerts",
            body.event_id,
            payload,
        )
        if not inserted:
            if stored != payload:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Budget alert event '{body.event_id}' was already "
                        "recorded with different data"
                    ),
                )
            if not any(
                alert.event_id == body.event_id for alert in _alerts[alert_org]
            ):
                _alerts[alert_org].append(BudgetAlert(**stored))
            return {"recorded": True, "duplicate": True, "event_id": body.event_id}
        await db.commit()
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
async def clear_budget_alerts(
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    """Acknowledge (clear) this org's budget alerts."""
    count = len(_alerts[org])
    _alerts[org].clear()
    await clear_runtime_namespace(db, org, "budget_alerts")
    await audit.log(
        db,
        actor_of(request),
        "clear",
        "budget_alerts",
        "*",
        {"cleared": count},
        org=org,
    )
    await db.commit()
    return {"cleared": count}


async def build_agent_auth_payload(
    gateway: Gateway,
    db: AsyncSession,
    org: str,
) -> dict[str, Any]:
    """Layer runtime quotas over the gateway's durable authorization policy."""
    gateway_id = gateway.id
    quotas = [
        quota for quota in _quotas[org].values()
        if quota.scope == "agent" and _agent_gateway(quota, org) == gateway_id
    ]
    measured = {
        quota.id: quota
        for quota in await _with_actual_metrics(quotas, db, org)
    }
    gateway_config = gateway.config or {}
    stored_spend = gateway_config.get("agent_spend", {})
    # Agent authorization also carries tool grants. Preserve the policy that
    # existed before quotas began managing runtime limits and layer quota fields
    # over it; otherwise changing a budget could silently widen allowed_tools to
    # "*" or delete unrelated agents.
    base_auth = deepcopy(
        gateway_config.get("agent_auth_base", gateway_config.get("agent_auth", {}))
    )
    base_agents = base_auth.get("agents", {})
    agents: dict[str, dict[str, Any]] = (
        deepcopy(base_agents) if isinstance(base_agents, dict) else {}
    )
    for quota in quotas:
        actual = measured[quota.id]
        base_agent = agents.get(quota.scope_id, {})
        agents[quota.scope_id] = {
            **base_agent,
            "allowed_tools": base_agent.get(
                "allowed_tools", base_auth.get("default_grants", [])
            ),
            "allowed_models": quota.allowed_models or ["*"],
            "allowed_providers": quota.allowed_providers or ["*"],
            "budget_usd": quota.budget_limit_usd,
            "spend_usd": max(
                actual.current_spend,
                float(stored_spend.get(quota.scope_id, 0.0) or 0.0),
            ),
            "rate_limit_rpm": quota.rate_limit_rpm,
            "max_tokens_per_request": quota.max_tokens_per_request,
            "alert_threshold_pct": quota.alert_threshold_pct,
            "description": quota.name,
        }

    payload = {
        "enabled": bool(base_auth.get("enabled", False)),
        "quota_enabled": bool(quotas) or bool(
            base_auth.get("quota_enabled", base_auth.get("enabled", False))
        ),
        "default_grants": base_auth.get("default_grants", []),
        "default_models": base_auth.get("default_models", ["*"]),
        "default_providers": base_auth.get("default_providers", ["*"]),
        "agents": agents,
    }
    gateway.config = {
        **gateway_config,
        "agent_auth_base": base_auth,
        "agent_auth": payload,
    }
    return payload


async def _push_agent_quotas(
    gateway_id: str,
    request: Request,
    db: AsyncSession,
    org: str,
) -> dict[str, Any]:
    """Persist and deliver the complete per-agent auth/quota map for one gateway."""
    import httpx

    gateway = await get_scoped(db, Gateway, gateway_id, org)
    if not gateway:
        raise HTTPException(status_code=404, detail=f"Gateway '{gateway_id}' not found")

    payload = await build_agent_auth_payload(gateway, db, org)
    agents = payload["agents"]
    await audit.log(
        db,
        actor_of(request),
        "push",
        "agent_quotas",
        gateway_id,
        {"gateway_id": gateway_id, "agents": sorted(agents)},
        org=org,
    )
    await db.commit()

    async with httpx.AsyncClient(
        timeout=10.0, headers=gateway_config_headers()
    ) as client:
        try:
            response = await client.post(
                f"{gateway.endpoint}/config/agent-auth", json=payload
            )
        except (httpx.ConnectError, httpx.TimeoutException):
            return {
                "status": "queued",
                "gateway": gateway_id,
                "agents": len(agents),
                "reason": "gateway_offline",
            }
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    if response.status_code == 200:
        return {
            "status": "pushed",
            "gateway": gateway_id,
            "agents": len(agents),
            "agent_auth": response.json(),
        }
    return {
        "status": "error",
        "gateway": gateway_id,
        "agents": len(agents),
        "detail": response.text[:200],
    }


@router.post("/agents/push")
async def push_agent_quotas(
    gateway_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    """Push every persisted agent quota assigned to a gateway."""
    return await _push_agent_quotas(gateway_id, request, db, org)


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
    if (
        quota.scope == "agent"
        and "gateway_id" in changes
        and changes["gateway_id"] != quota.gateway_id
    ):
        raise HTTPException(
            status_code=409,
            detail="Move an agent quota by deleting and recreating it on the new gateway",
        )
    updated = quota.model_copy(update=changes)
    if updated.scope == "agent" and not _agent_gateway(updated, org):
        raise HTTPException(status_code=422, detail="Agent quotas require a gateway_id")
    if updated.scope == "agent":
        duplicate = next((
            other for other in _quotas[org].values()
            if other.id != quota_id
            and other.scope == "agent"
            and other.scope_id == updated.scope_id
            and _agent_gateway(other, org) == _agent_gateway(updated, org)
        ), None)
        if duplicate:
            raise HTTPException(
                status_code=409,
                detail=f"Agent quota already exists for '{updated.scope_id}' on "
                       f"'{_agent_gateway(updated, org)}'",
            )
    _quotas[org][quota_id] = updated
    await put_runtime_state(
        db,
        org,
        "quotas",
        str(quota_id),
        updated.model_dump(mode="json"),
    )

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
    await delete_runtime_state(db, org, "quotas", str(quota_id))
    await audit.log(db, actor_of(request), "delete", "quota", str(quota_id),
                    {
                        "name": quota.name,
                        "scope_id": quota.scope_id,
                        "gateway_id": _agent_gateway(quota, org),
                    }, org=org)
    await db.commit()
    return {
        "deleted": quota_id,
        "scope": quota.scope,
        "gateway_id": _agent_gateway(quota, org),
    }


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
    if quota.scope == "agent":
        gateway_id = _agent_gateway(quota, org)
        if not gateway_id:
            raise HTTPException(status_code=422, detail="Agent quota has no gateway")
        return await _push_agent_quotas(gateway_id, request, db, org)
    if quota.scope != "gateway" or not quota.scope_id:
        return {"status": "skipped", "reason": "Quota scope cannot be pushed directly"}

    # Get gateway endpoint from the database
    gateway = await get_scoped(db, Gateway, quota.scope_id, org)
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

    async with httpx.AsyncClient(
        timeout=10.0, headers=gateway_config_headers()
    ) as client:
        try:
            resp = await client.post(
                f"{gateway.endpoint}/config/quota", json=payload,
            )
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
            raise HTTPException(status_code=502, detail=str(e)) from e
