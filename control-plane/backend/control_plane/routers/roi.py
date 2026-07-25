"""ROI / savings API — damage-prevented estimate from blocked actions.

The dollar figure is an *estimate* built from a cost model the CIO controls:
each blocked action type maps to an assumed incident cost, risk-weighted by the
block's score. Counts and scores are real (from the trace buffer); the costs
are editable assumptions, persisted in the state file.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from control_plane import roi
from control_plane.auth.dependencies import get_current_org
from control_plane.routers.traces import recent_traces_for

router = APIRouter(prefix="/api/roi", tags=["roi"])

# CIO-editable cost model. None => use roi.DEFAULT_COST_MODEL. Stored as an
# ordered list of {pattern, cost} so the UI can edit/reorder; persisted via the
# state file (see app.py lifespan).
_cost_model: dict = {"entries": None, "fallback": roi.DEFAULT_FALLBACK_COST}


def _model_entries() -> list[tuple[str, float]] | None:
    entries = _cost_model.get("entries")
    if not entries:
        return None
    return [(e["pattern"], float(e["cost"])) for e in entries]


def _default_entries() -> list[dict]:
    return [{"pattern": p, "cost": c} for p, c in roi.DEFAULT_COST_MODEL]


class CostModel(BaseModel):
    entries: list[dict]          # [{pattern, cost}, ...] — order = match priority
    fallback: float = roi.DEFAULT_FALLBACK_COST


@router.get("/cost-model")
async def get_cost_model():
    """Current editable cost model (defaults if the CIO hasn't customized it)."""
    entries = _cost_model.get("entries")
    return {
        "entries": entries if entries else _default_entries(),
        "fallback": _cost_model.get("fallback", roi.DEFAULT_FALLBACK_COST),
        "customized": bool(entries),
    }


@router.post("/cost-model")
async def set_cost_model(body: CostModel):
    """Replace the cost model with the CIO's assumptions."""
    _cost_model["entries"] = [{"pattern": e["pattern"], "cost": float(e["cost"])} for e in body.entries]
    _cost_model["fallback"] = float(body.fallback)
    return {"entries": _cost_model["entries"], "fallback": _cost_model["fallback"], "customized": True}


@router.post("/cost-model/reset")
async def reset_cost_model():
    """Revert to the default cost model."""
    _cost_model["entries"] = None
    _cost_model["fallback"] = roi.DEFAULT_FALLBACK_COST
    return {"entries": _default_entries(), "fallback": roi.DEFAULT_FALLBACK_COST, "customized": False}


@router.get("/report")
async def report(weight_by_score: bool = True, org: str = Depends(get_current_org)):
    """Damage-prevented estimate from blocked actions in the trace buffer."""
    rep = roi.compute_roi(
        recent_traces_for(org),
        cost_model=_model_entries(),
        fallback_cost=_cost_model.get("fallback", roi.DEFAULT_FALLBACK_COST),
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
