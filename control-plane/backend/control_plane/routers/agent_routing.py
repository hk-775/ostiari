"""Agent routing policy API — per-agent LLM model-rotation, pushed to gateways.

This is *model selection* (which LLM an agent's calls use), distinct from the
per-model backend load-balancing shown on the Models page. An operator sets a
policy like "claude-code round-robins across [claude-sonnet, gpt-4o]"; the
control plane stores it and pushes it to the agent's gateway so it is actually
enforced at runtime (via the gateway's /config/agent-routing endpoint).
"""

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org
from control_plane.database import get_db
from control_plane.models.database import Gateway
from control_plane.models.scoping import get_scoped
from control_plane.services.audit_service import actor_of, audit
from control_plane.services.push_service import gateway_config_headers
from control_plane.services.runtime_state import (
    delete_runtime_state,
    put_runtime_state,
)

router = APIRouter(prefix="/api/agent-routing", tags=["agent-routing"])


class RoutingPolicy(BaseModel):
    agent_id: str
    gateway_id: str
    strategy: str = "round_robin"          # round_robin
    models: list[str] = Field(default_factory=list)
    scope: str = "request"                 # request | session


# Hot cache over tenant-scoped runtime_state_records, keyed by
# (org, gateway_id, agent_id).
_policies: dict[tuple[str, str, str], RoutingPolicy] = {}


def _by_gateway(org: str, gateway_id: str) -> dict[str, dict]:
    """Build the gateway-shaped agent_routing dict for one org's gateway."""
    return {
        p.agent_id: {"strategy": p.strategy, "models": p.models, "scope": p.scope}
        for (o, gid, aid), p in _policies.items()
        if o == org and gid == gateway_id
    }


async def _push(org: str, gateway: Gateway) -> tuple[bool, str]:
    """Push this gateway's full agent_routing map to it."""
    payload = {"agent_routing": _by_gateway(org, gateway.id)}
    try:
        async with httpx.AsyncClient(
            timeout=10.0, headers=gateway_config_headers()
        ) as client:
            resp = await client.post(
                f"{gateway.endpoint}/config/agent-routing", json=payload,
            )
            if resp.status_code == 200:
                return True, ""
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


@router.get("")
async def list_policies(org: str = Depends(get_current_org)) -> list[RoutingPolicy]:
    return [p for (o, _, _), p in _policies.items() if o == org]


@router.get("/{gateway_id}")
async def list_for_gateway(gateway_id: str, org: str = Depends(get_current_org)) -> list[RoutingPolicy]:
    return [p for (o, gid, aid), p in _policies.items() if o == org and gid == gateway_id]


@router.post("")
async def set_policy(
    body: RoutingPolicy,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
) -> dict:
    """Create/update a per-agent routing policy and push it to the gateway."""
    gateway = await get_scoped(db, Gateway, body.gateway_id, org)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")
    if body.strategy == "round_robin" and len(body.models) < 1:
        raise HTTPException(status_code=400, detail="round_robin needs at least one model")

    _policies[(org, body.gateway_id, body.agent_id)] = body
    state_key = f"{body.gateway_id}:{body.agent_id}"
    await put_runtime_state(
        db,
        org,
        "agent_routing",
        state_key,
        body.model_dump(mode="json"),
    )
    await audit.log(
        db,
        actor_of(request),
        "set",
        "agent_routing",
        state_key,
        body.model_dump(mode="json"),
        org=org,
    )
    await db.commit()
    pushed, err = await _push(org, gateway)
    return {"status": "saved", "pushed": pushed, "push_error": err or None, "policy": body.model_dump()}


@router.delete("/{gateway_id}/{agent_id}")
async def delete_policy(
    gateway_id: str,
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
) -> dict:
    if (org, gateway_id, agent_id) not in _policies:
        raise HTTPException(status_code=404, detail="Policy not found")
    del _policies[(org, gateway_id, agent_id)]
    state_key = f"{gateway_id}:{agent_id}"
    await delete_runtime_state(db, org, "agent_routing", state_key)
    await audit.log(
        db,
        actor_of(request),
        "delete",
        "agent_routing",
        state_key,
        {"gateway_id": gateway_id, "agent_id": agent_id},
        org=org,
    )
    await db.commit()
    gateway = await get_scoped(db, Gateway, gateway_id, org)
    pushed, err = (await _push(org, gateway)) if gateway else (False, "gateway not found")
    return {"status": "deleted", "pushed": pushed, "push_error": err or None}
