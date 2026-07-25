"""Behavior-derived trust API.

Shadow-first: GET /scores computes derived trust from recent traces and shows
it alongside each agent's *configured* score (what would change). Enforcement
is opt-in — POST /apply pushes the derived scores into the gateway's
cross-agent policy. Nothing is enforced automatically.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane import trust
from control_plane.auth.dependencies import get_current_org
from control_plane.database import get_db
from control_plane.models.database import Gateway
from control_plane.models.scoping import get_scoped
from control_plane.routers.traces import recent_traces_for

router = APIRouter(prefix="/api/trust", tags=["trust"])

# Whether derived-trust enforcement has been turned on, per gateway. Tracked in
# the control plane (enforcement is a CP concern) rather than round-tripped
# through the gateway config. Defaults off — shadow-only.
_enforced: dict[str, bool] = {}


async def _get_cross_agent(gateway) -> dict:
    """Fetch a gateway's current cross-agent policy (for configured scores)."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{gateway.endpoint}/config/cross-agent")
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return {}


@router.get("/scores")
async def scores(gateway_id: str = "crm-agent", db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    """Derived-vs-configured trust per agent (shadow view — computes only)."""
    gateway = await get_scoped(db, Gateway, gateway_id, org)
    configured: dict[str, int] = {}
    if gateway is not None:
        policy = await _get_cross_agent(gateway)
        configured = policy.get("trust_scores", {}) or {}
    enforced = _enforced.get(gateway_id, False)

    rows = trust.score_agents(recent_traces_for(org), configured=configured)
    would_change = [r for r in rows if r["delta"] is not None and abs(r["delta"]) >= 10]
    return {
        "gateway_id": gateway_id,
        "enforced": enforced,
        "agents": rows,
        "would_change_count": len(would_change),
        "baseline": trust.BASELINE,
    }


@router.post("/apply")
async def apply(gateway_id: str = "crm-agent", db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    """Opt-in: push derived scores into the gateway's cross-agent trust_scores.

    Merges derived scores over the existing policy (preserving edges/min_trust)
    and marks enforcement on. Manual config can still be re-applied to override.
    """
    gateway = await get_scoped(db, Gateway, gateway_id, org)
    if gateway is None:
        raise HTTPException(status_code=404, detail="Gateway not found")

    policy = await _get_cross_agent(gateway)
    rows = trust.score_agents(recent_traces_for(org), configured=policy.get("trust_scores", {}))
    derived = {r["agent_id"]: r["derived_score"] for r in rows if r["agent_id"] != "unknown"}

    if not derived:
        raise HTTPException(status_code=400, detail="No agent activity to derive scores from")

    policy["trust_scores"] = {**policy.get("trust_scores", {}), **derived}

    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            r = await client.post(f"{gateway.endpoint}/config/cross-agent", json=policy)
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Failed to push to gateway: {exc}") from None

    _enforced[gateway_id] = True
    return {"gateway_id": gateway_id, "applied": derived, "count": len(derived)}


@router.post("/disable")
async def disable(gateway_id: str = "crm-agent"):
    """Turn off derived-trust enforcement (back to shadow-only).

    Leaves already-pushed scores in place but marks the fleet as no longer
    auto-enforcing; re-apply a manual policy to fully revert values.
    """
    _enforced[gateway_id] = False
    return {"gateway_id": gateway_id, "enforced": False}
