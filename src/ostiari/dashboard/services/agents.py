"""Agent service — agent listing and per-agent statistics."""

from __future__ import annotations

from typing import Any

from ostiari.dashboard.storage_async import AsyncStorageWrapper
from ostiari.models import TraceFilters


class AgentService:
    """Provides agent listing and per-agent statistics."""

    def __init__(self, storage: AsyncStorageWrapper) -> None:
        self._storage = storage

    async def list_agents(self) -> list[dict[str, Any]]:
        traces = await self._storage.get_traces(TraceFilters(limit=1000))
        agents: dict[str, dict[str, Any]] = {}

        for t in traces:
            cid = t.correlation_id or "default"
            if cid not in agents:
                agents[cid] = {
                    "id": cid,
                    "first_seen": t.timestamp.isoformat(),
                    "last_seen": t.timestamp.isoformat(),
                    "total": 0,
                }
            agents[cid]["total"] += 1
            agents[cid]["last_seen"] = t.timestamp.isoformat()

        return list(agents.values())

    async def agent_stats(self, agent_id: str) -> dict[str, Any]:
        traces = await self._storage.get_traces(TraceFilters(correlation_id=agent_id, limit=1000))
        total = len(traces)
        return {
            "agent_id": agent_id,
            "total_actions": total,
            "allowed": sum(1 for t in traces if t.tier == "allow"),
            "blocked": sum(1 for t in traces if t.tier == "block"),
            "intervened": sum(1 for t in traces if t.tier == "intervene"),
            "avg_risk": round(sum(t.risk_score for t in traces) / max(total, 1), 2),
        }
