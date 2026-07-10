"""Agents API router."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ostiari.dashboard.cache import QueryCache
from ostiari.dashboard.dependencies import get_agent_service, get_cache
from ostiari.dashboard.services.agents import AgentService

router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.get("")
async def list_agents(
    agent_service: AgentService = Depends(get_agent_service),
    cache: QueryCache = Depends(get_cache),
) -> list[dict[str, Any]]:
    return await cache.get_or_compute(
        "agents:list",
        agent_service.list_agents,
        ttl=30,
    )


@router.get("/{agent_id}/stats")
async def agent_stats(
    agent_id: str,
    agent_service: AgentService = Depends(get_agent_service),
    cache: QueryCache = Depends(get_cache),
) -> dict[str, Any]:
    return await cache.get_or_compute(
        f"agents:{agent_id}:stats",
        lambda: agent_service.agent_stats(agent_id),
        ttl=10,
    )
