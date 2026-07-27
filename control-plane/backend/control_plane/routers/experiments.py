"""A/B experiment management API."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org
from control_plane.database import get_db
from control_plane.models.database import UsageRecord
from control_plane.models.scoping import scoped

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


@router.get("")
async def list_experiments(org: str = Depends(get_current_org)) -> list[ExperimentResponse]:
    return list(_experiments[org].values())


@router.post("", response_model=ExperimentResponse)
async def create_experiment(body: ExperimentCreate, org: str = Depends(get_current_org)):
    if body.name in _experiments[org]:
        raise HTTPException(status_code=409, detail=f"Experiment '{body.name}' already exists")
    exp = ExperimentResponse(
        name=body.name, model_a=body.model_a, model_b=body.model_b,
        traffic_pct_b=body.traffic_pct_b, gateway_id=body.gateway_id,
    )
    _experiments[org][body.name] = exp
    return exp


@router.delete("/{name}")
async def delete_experiment(name: str, org: str = Depends(get_current_org)):
    if name not in _experiments[org]:
        raise HTTPException(status_code=404, detail="Experiment not found")
    del _experiments[org][name]
    return {"deleted": name}


@router.patch("/{name}/toggle")
async def toggle_experiment(name: str, org: str = Depends(get_current_org)):
    exp = _experiments[org].get(name)
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")
    exp.enabled = not exp.enabled
    return exp


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
