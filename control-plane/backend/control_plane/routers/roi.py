"""ROI / savings API — damage-prevented estimate from blocked actions.

The dollar figure is an *estimate* built from a cost model the CIO controls:
each blocked action type maps to an assumed incident cost, risk-weighted by the
block's score. Counts and scores are real; editable assumptions are SQL-backed.
"""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane import roi
from control_plane.auth.dependencies import get_current_org
from control_plane.database import get_db
from control_plane.routers.traces import recent_traces_for
from control_plane.services.audit_service import actor_of, audit
from control_plane.services.runtime_state import (
    delete_runtime_state,
    put_runtime_state,
)

router = APIRouter(prefix="/api/roi", tags=["roi"])

# CIO-editable cost model. None => use roi.DEFAULT_COST_MODEL. Stored as an
# ordered list of {pattern, cost} so the UI can edit/reorder; persisted in SQL.
_cost_model: dict[str, dict] = defaultdict(
    lambda: {"entries": None, "fallback": roi.DEFAULT_FALLBACK_COST}
)


def _model_entries(org: str) -> list[tuple[str, float]] | None:
    entries = _cost_model[org].get("entries")
    if not entries:
        return None
    return [(e["pattern"], float(e["cost"])) for e in entries]


def _default_entries() -> list[dict]:
    return [{"pattern": p, "cost": c} for p, c in roi.DEFAULT_COST_MODEL]


class CostModel(BaseModel):
    entries: list[dict]          # [{pattern, cost}, ...] — order = match priority
    fallback: float = roi.DEFAULT_FALLBACK_COST


@router.get("/cost-model")
async def get_cost_model(org: str = Depends(get_current_org)):
    """Current editable cost model (defaults if the CIO hasn't customized it)."""
    entries = _cost_model[org].get("entries")
    return {
        "entries": entries if entries else _default_entries(),
        "fallback": _cost_model[org].get("fallback", roi.DEFAULT_FALLBACK_COST),
        "customized": bool(entries),
    }


@router.post("/cost-model")
async def set_cost_model(
    body: CostModel,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    """Replace the cost model with the CIO's assumptions."""
    _cost_model[org]["entries"] = [{"pattern": e["pattern"], "cost": float(e["cost"])} for e in body.entries]
    _cost_model[org]["fallback"] = float(body.fallback)
    await put_runtime_state(
        db,
        org,
        "roi_cost_model",
        "config",
        dict(_cost_model[org]),
    )
    await audit.log(
        db,
        actor_of(request),
        "update",
        "roi_cost_model",
        "config",
        {"entries": len(body.entries), "fallback": body.fallback},
        org=org,
    )
    await db.commit()
    return {"entries": _cost_model[org]["entries"], "fallback": _cost_model[org]["fallback"], "customized": True}


@router.post("/cost-model/reset")
async def reset_cost_model(
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    """Revert to the default cost model."""
    _cost_model[org]["entries"] = None
    _cost_model[org]["fallback"] = roi.DEFAULT_FALLBACK_COST
    await delete_runtime_state(db, org, "roi_cost_model", "config")
    await audit.log(
        db,
        actor_of(request),
        "reset",
        "roi_cost_model",
        "config",
        {},
        org=org,
    )
    await db.commit()
    return {"entries": _default_entries(), "fallback": roi.DEFAULT_FALLBACK_COST, "customized": False}


@router.get("/report")
async def report(weight_by_score: bool = True, org: str = Depends(get_current_org)):
    """Damage-prevented estimate from blocked actions in the trace buffer."""
    rep = roi.compute_roi(
        recent_traces_for(org),
        cost_model=_model_entries(org),
        fallback_cost=_cost_model[org].get("fallback", roi.DEFAULT_FALLBACK_COST),
        weight_by_score=weight_by_score,
    )
    return {
        "blocked_count": rep.blocked_count,
        "distinct_actions": rep.distinct_actions,
        "total_prevented_usd": rep.total_prevented_usd,
        "fallback_cost": rep.fallback_cost,
        "weight_by_score": weight_by_score,
        "actions": [
            {
                "action": a.action, "count": a.count, "unit_cost": a.unit_cost,
                "prevented_usd": a.prevented_usd, "max_score": a.max_score,
            }
            for a in rep.actions
        ],
    }
