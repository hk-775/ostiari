"""Agent discovery and governed onboarding."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane import discovery
from control_plane.auth.dependencies import get_current_org
from control_plane.database import get_db
from control_plane.discovery_collectors import default_collectors
from control_plane.models.database import Gateway
from control_plane.models.scoping import get_scoped
from control_plane.services.audit_service import actor_of, audit

router = APIRouter(prefix="/api/discovery", tags=["discovery"])


class OnboardRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    gateway_id: str = Field(min_length=1, max_length=64)
    framework: str = Field(default="other", max_length=64)
    allowed_tools: list[str] = Field(default_factory=list)
    allowed_models: list[str] = Field(default_factory=lambda: ["*"])
    allowed_providers: list[str] = Field(default_factory=lambda: ["*"])

    @field_validator("agent_id", "gateway_id", "framework")
    @classmethod
    def strip_identifier(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


def _known_agents(org: str) -> dict[str, str]:
    """Registered agent names and their assigned gateway for one tenant."""
    from control_plane.routers.agents import _agents

    return {
        agent.name: agent.gateway_id
        for agent in _agents[org].values()
    }


def _normalize(agent_id: str) -> str:
    value = agent_id.strip().lower()
    return value[4:] if value.startswith("a2a.") else value


async def _run_discovery(
    org: str,
) -> tuple[list[Any], list[discovery.DiscoveredAgent]]:
    collectors = default_collectors(org)
    results = await asyncio.to_thread(
        discovery.reconcile,
        collectors,
        _known_agents(org),
    )
    return collectors, results


@router.get("/agents")
async def discovered_agents(org: str = Depends(get_current_org)):
    """Reconcile observed identities against the tenant's agent registry."""
    collectors, results = await _run_discovery(org)
    counts = {
        "discovered": 0,
        "registered_off_gateway": 0,
        "governed": 0,
        "governed_unseen": 0,
    }
    for item in results:
        counts[item.status] = counts.get(item.status, 0) + 1

    source_status = []
    for collector in collectors:
        error = str(getattr(collector, "last_error", "") or "")
        source_status.append({
            "source": collector.source,
            "status": "error" if error else "ok",
            "detail": error[:300],
        })

    return {
        "summary": {
            "total": len(results),
            "shadow": counts["discovered"],
            "off_gateway": counts["registered_off_gateway"],
            "governed": counts["governed"],
            "stale": counts["governed_unseen"],
            "sources": [collector.source for collector in collectors],
            "source_status": source_status,
        },
        "agents": [
            {
                "agent_id": item.agent_id,
                "status": item.status,
                "registered": item.registered,
                "sources": item.sources,
                "gateways": item.gateways,
                "governed_gateways": item.governed_gateways,
                "assigned_gateway": item.assigned_gateway,
                "call_count": item.call_count,
                "confidence": round(item.confidence, 2),
                "evidence": item.evidence,
            }
            for item in results
        ],
    }


@router.post("/onboard")
async def onboard(
    body: OnboardRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    """Register a discovered identity and install its gateway authorization."""
    from control_plane.routers.agents import AgentConfig, _agents
    from control_plane.routers.quotas import _push_agent_quotas

    agent_id = body.agent_id
    existing_name = next(
        (
            name
            for name in _agents[org]
            if _normalize(name) == _normalize(agent_id)
        ),
        None,
    )
    if existing_name:
        raise HTTPException(
            status_code=409,
            detail=f"Agent '{existing_name}' already registered",
        )

    gateway = await get_scoped(db, Gateway, body.gateway_id, org)
    if not gateway:
        raise HTTPException(
            status_code=404,
            detail=f"Gateway '{body.gateway_id}' not found",
        )

    _collectors, results = await _run_discovery(org)
    sighting = next(
        (
            item
            for item in results
            if _normalize(item.agent_id) == _normalize(agent_id)
            and not item.registered
        ),
        None,
    )
    if not sighting:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{agent_id}' is not in the current discovery report",
        )

    gateway_config = deepcopy(gateway.config or {})
    base_auth = deepcopy(
        gateway_config.get(
            "agent_auth_base",
            gateway_config.get("agent_auth", {}),
        )
    )
    if not isinstance(base_auth, dict):
        base_auth = {}
    auth_was_enabled = bool(base_auth.get("enabled", False))
    base_agents = base_auth.get("agents", {})
    base_agents = (
        deepcopy(base_agents)
        if isinstance(base_agents, dict)
        else {}
    )

    existing_grants = base_agents.get(agent_id)
    if isinstance(existing_grants, dict):
        agent_grants = deepcopy(existing_grants)
        agent_grants.setdefault("allowed_tools", body.allowed_tools)
        agent_grants.setdefault("allowed_models", body.allowed_models)
        agent_grants.setdefault("allowed_providers", body.allowed_providers)
        agent_grants.setdefault("description", "Onboarded from discovery")
    else:
        agent_grants = {
            "allowed_tools": body.allowed_tools,
            "allowed_models": body.allowed_models,
            "allowed_providers": body.allowed_providers,
            "description": "Onboarded from discovery",
        }
    base_agents[agent_id] = agent_grants

    # Activating authorization for the first discovered identity must not
    # abruptly deny unrelated agents. Existing enabled policies retain their
    # defaults; a newly enabled policy leaves unknown identities unchanged while
    # the onboarded identity receives its explicit least-privilege entry.
    default_grants = base_auth.get("default_grants", [])
    if not auth_was_enabled:
        default_grants = ["*"]
    base_auth = {
        **base_auth,
        "enabled": True,
        "quota_enabled": bool(
            base_auth.get("quota_enabled", base_auth.get("enabled", False))
        ),
        "default_grants": default_grants,
        "default_models": base_auth.get("default_models", ["*"]),
        "default_providers": base_auth.get("default_providers", ["*"]),
        "agents": base_agents,
    }
    gateway.config = {
        **gateway_config,
        "agent_auth_base": base_auth,
    }

    traffic_routed = body.gateway_id in sighting.governed_gateways
    registered = AgentConfig(
        name=agent_id,
        framework=body.framework or "other",
        gateway_id=body.gateway_id,
        tools=body.allowed_tools,
        description="Onboarded from discovery",
        status="governed" if traffic_routed else "registered_off_gateway",
    )
    await audit.log(
        db,
        actor_of(request),
        "onboard",
        "agent",
        agent_id,
        {
            "framework": registered.framework,
            "gateway_id": body.gateway_id,
            "allowed_tools": body.allowed_tools,
            "allowed_models": body.allowed_models,
            "allowed_providers": body.allowed_providers,
            "traffic_routed": traffic_routed,
            "sources": sighting.sources,
        },
        org=org,
    )
    await db.commit()
    _agents[org][agent_id] = registered

    try:
        policy_result = await _push_agent_quotas(
            body.gateway_id,
            request,
            db,
            org,
        )
    except HTTPException as exc:
        policy_result = {
            "status": "error",
            "gateway": body.gateway_id,
            "detail": str(exc.detail),
        }

    return {
        "onboarded": agent_id,
        "registered": True,
        "gateway_id": body.gateway_id,
        "status": "governed" if traffic_routed else "registered_off_gateway",
        "traffic_routed": traffic_routed,
        "gateway_policy": policy_result,
    }
