"""Agent discovery API — surface agents SEEN across signal sources but not in
the registry (shadow AI), and onboard them.

Discovery here = reconcile (union of collectors) vs. (agents registry). Onboard
= register the discovered agent so it becomes governed. Onboarding does NOT
reach out and reconfigure the agent — it records it + attaches it to a gateway;
actually routing the agent's traffic through that gateway is a separate,
lever-dependent step (see docs/internal/agent-discovery-plan.md).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from control_plane import discovery
from control_plane.discovery_collectors import default_collectors

router = APIRouter(prefix="/api/discovery", tags=["discovery"])


class OnboardRequest(BaseModel):
    agent_id: str
    gateway_id: str = ""
    framework: str = "other"


def _known_agent_ids() -> list[str]:
    from control_plane.routers.agents import _agents
    return list(_agents.keys())


@router.get("/agents")
async def discovered_agents():
    """Reconcile seen-vs-known across all collectors. Shadow AI listed first."""
    results = discovery.reconcile(default_collectors(), _known_agent_ids())
    counts = {"discovered": 0, "governed": 0, "governed_unseen": 0}
    for d in results:
        counts[d.status] = counts.get(d.status, 0) + 1
    return {
        "summary": {
            "total": len(results),
            "shadow": counts["discovered"],       # seen, not governed
            "governed": counts["governed"],
            "stale": counts["governed_unseen"],   # governed, not seen
            "sources": [c.source for c in default_collectors()],
        },
        "agents": [
            {
                "agent_id": d.agent_id, "status": d.status,
                "registered": d.registered, "sources": d.sources,
                "gateways": d.gateways, "call_count": d.call_count,
                "confidence": round(d.confidence, 2), "evidence": d.evidence,
            }
            for d in results
        ],
    }


@router.post("/onboard")
async def onboard(body: OnboardRequest):
    """Register a discovered agent → it becomes 'governed' on the next reconcile.

    Note: this records the agent and its intended gateway. It does not reroute
    the agent's traffic; that requires a routing lever (config/network) applied
    separately. If no lever exists, the record still gives you tracked
    visibility, not enforcement.
    """
    from control_plane.routers.agents import AgentConfig, _agents

    if body.agent_id in _agents:
        raise HTTPException(status_code=409, detail=f"Agent '{body.agent_id}' already registered")

    _agents[body.agent_id] = AgentConfig(
        name=body.agent_id,
        framework=body.framework or "other",
        gateway_id=body.gateway_id,
        description="Onboarded from discovery",
        status="registered",
    )
    return {
        "onboarded": body.agent_id,
        "gateway_id": body.gateway_id,
        "note": "Registered. Route its traffic through the gateway to enforce (see docs).",
    }
