"""A/B experiment management API.

An experiment is only real once the gateway knows about it: the traffic split
lives in the gateway's router (consistent hash on agent_id), so every mutation
here pushes the owning gateway's full experiment set to
``POST /config/ab-experiments``. That endpoint is a partial update — pushing
through ``/config/llm`` would replace the whole LLM document, wiping the
provider credentials the gateway loaded at startup.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org
from control_plane.database import get_db
from control_plane.models.database import Gateway, UsageRecord
from control_plane.models.scoping import scoped
from control_plane.services.audit_service import actor_of, audit

router = APIRouter(prefix="/api/experiments", tags=["experiments"])


class ExperimentCreate(BaseModel):
    name: str
    model_a: str
    model_b: str
    traffic_pct_b: int = Field(default=10, ge=1, le=99)
    gateway_id: str


class ExperimentResponse(BaseModel):
    name: str
    model_a: str
    model_b: str
    traffic_pct_b: int
    gateway_id: str
    enabled: bool = True


class ExperimentResults(BaseModel):
    experiment_name: str
    period_days: int
    model_a: dict
    model_b: dict


# In-memory store (production would use DB), scoped per org (tenant):
# org -> name -> experiment. Single-org dev/demo uses only the "default" org.
_experiments: dict[str, dict[str, ExperimentResponse]] = defaultdict(dict)


def _for_gateway(org: str, gateway_id: str) -> list[dict]:
    """This org's experiments for one gateway, in the shape the gateway expects.

    Sent as a complete set (not a delta) because the gateway's endpoint replaces
    ``ab_experiments`` wholesale — that is what makes a delete take effect.
    """
    return [
        {
            "name": e.name, "enabled": e.enabled,
            "model_a": e.model_a, "model_b": e.model_b,
            "traffic_pct_b": e.traffic_pct_b,
            # The CP model has no per-agent scoping, so every experiment applies
            # to all of the gateway's agents. Sent explicitly rather than left to
            # the gateway's default, so the pushed document is unambiguous.
            "agents": [],
        }
        for e in _experiments[org].values()
        if e.gateway_id == gateway_id
    ]


async def _push(org: str, gateway_id: str, db: AsyncSession) -> tuple[bool, str]:
    """Push this gateway's full experiment set to it. Never raises."""
    gateway = await db.get(Gateway, gateway_id)
    if not gateway:
        return False, f"Gateway '{gateway_id}' not found"
    payload = {"ab_experiments": _for_gateway(org, gateway_id)}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{gateway.endpoint}/config/ab-experiments", json=payload
            )
            if resp.status_code == 200:
                return True, ""
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:  # noqa: BLE001
        # A stored-but-unpushed experiment is reported, not hidden: the caller
        # gets pushed=False so the UI can say "saved, not live" rather than
        # implying the split is running.
        return False, str(e)


@router.get("")
async def list_experiments(org: str = Depends(get_current_org)) -> list[ExperimentResponse]:
    return list(_experiments[org].values())


@router.post("")
async def create_experiment(
    body: ExperimentCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
) -> dict:
    if body.name in _experiments[org]:
        raise HTTPException(status_code=409, detail=f"Experiment '{body.name}' already exists")
    exp = ExperimentResponse(
        name=body.name, model_a=body.model_a, model_b=body.model_b,
        traffic_pct_b=body.traffic_pct_b, gateway_id=body.gateway_id,
    )
    _experiments[org][body.name] = exp
    pushed, err = await _push(org, exp.gateway_id, db)
    # An experiment silently sends a share of live traffic to a different model —
    # who started it, on which gateway, and at what split all belong in the trail.
    await audit.log(db, actor_of(request), "create", "experiment", exp.name, {
        "gateway_id": exp.gateway_id, "model_a": exp.model_a, "model_b": exp.model_b,
        "traffic_pct_b": exp.traffic_pct_b, "pushed": pushed,
    }, org=org)
    await db.commit()
    return {**exp.model_dump(), "pushed": pushed, "push_error": err or None}


@router.delete("/{name}")
async def delete_experiment(
    name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    exp = _experiments[org].get(name)
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")
    gateway_id = exp.gateway_id
    del _experiments[org][name]
    # Push after the delete so the gateway receives the set without this one.
    pushed, err = await _push(org, gateway_id, db)
    await audit.log(db, actor_of(request), "delete", "experiment", name,
                    {"gateway_id": gateway_id, "pushed": pushed}, org=org)
    await db.commit()
    return {"deleted": name, "pushed": pushed, "push_error": err or None}


@router.patch("/{name}/toggle")
async def toggle_experiment(
    name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
) -> dict:
    exp = _experiments[org].get(name)
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")
    exp.enabled = not exp.enabled
    pushed, err = await _push(org, exp.gateway_id, db)
    await audit.log(db, actor_of(request), "toggle", "experiment", name,
                    {"enabled": exp.enabled, "gateway_id": exp.gateway_id,
                     "pushed": pushed}, org=org)
    await db.commit()
    return {**exp.model_dump(), "pushed": pushed, "push_error": err or None}


@router.post("/{name}/push")
async def push_experiment(
    name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
) -> dict:
    """Re-push this experiment's gateway set — for a gateway that was down."""
    exp = _experiments[org].get(name)
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")
    pushed, err = await _push(org, exp.gateway_id, db)
    if not pushed:
        raise HTTPException(status_code=502, detail=err)
    await audit.log(db, actor_of(request), "push", "experiment", name,
                    {"gateway_id": exp.gateway_id}, org=org)
    await db.commit()
    return {"status": "pushed", "gateway": exp.gateway_id,
            "experiments": _for_gateway(org, exp.gateway_id)}


@router.get("/{name}/results", response_model=ExperimentResults)
async def get_experiment_results(
    name: str,
    period_days: int = Query(default=7, le=30),
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    """Compare performance between model A and model B for this experiment."""
    exp = _experiments[org].get(name)
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")

    since = datetime.now(timezone.utc) - timedelta(days=period_days)
    # The experiment is already org-keyed, so its gateway_id belongs to this org;
    # scope the usage query too rather than relying on that indirection.
    result = await db.execute(
        scoped(
            select(UsageRecord).where(
                UsageRecord.timestamp >= since,
                UsageRecord.gateway_id == exp.gateway_id,
                UsageRecord.model.in_([exp.model_a, exp.model_b]),
            ),
            UsageRecord, org,
        )
    )
    records = result.scalars().all()

    model_a_records = [r for r in records if r.model == exp.model_a]
    model_b_records = [r for r in records if r.model == exp.model_b]

    def _stats(recs: list) -> dict:
        if not recs:
            return {"requests": 0, "total_tokens": 0, "total_cost": 0.0, "avg_tokens": 0, "avg_cost": 0.0}
        total_tokens = sum(r.total_tokens for r in recs)
        total_cost = sum(r.cost_usd for r in recs)
        return {
            "requests": len(recs),
            "total_tokens": total_tokens,
            "total_cost": round(total_cost, 4),
            "avg_tokens": round(total_tokens / len(recs)),
            "avg_cost": round(total_cost / len(recs), 6),
        }

    return ExperimentResults(
        experiment_name=name,
        period_days=period_days,
        model_a={"model": exp.model_a, **_stats(model_a_records)},
        model_b={"model": exp.model_b, **_stats(model_b_records)},
    )
