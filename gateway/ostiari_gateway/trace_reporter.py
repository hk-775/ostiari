"""Trace reporter — sends tool call events to the control plane in real-time.

Also handles per-agent spend persistence: pushes snapshots periodically
and restores on gateway startup.
"""

import asyncio
import logging
import time
from typing import Any

import httpx

log = logging.getLogger("ostiari.sidecar.traces")


class TraceReporter:
    """Reports tool call events to the control plane for the live trace viewer.

    Sends immediately (not batched) for real-time visibility.
    Fire-and-forget — never blocks the agent response.

    Also persists per-agent spend to the Control Plane so budgets
    survive gateway restarts.
    """

    def __init__(self, control_plane_url: str = "", sidecar_id: str = "") -> None:
        self._url = control_plane_url.rstrip("/") if control_plane_url else ""
        self._sidecar_id = sidecar_id
        self._client: httpx.AsyncClient | None = None
        self._spend_task: asyncio.Task | None = None
        self._agent_auth: Any = None

    def configure(self, control_plane_url: str, sidecar_id: str) -> None:
        self._url = control_plane_url.rstrip("/")
        self._sidecar_id = sidecar_id

    def set_agent_auth(self, agent_auth: Any) -> None:
        """Wire the AgentAuthPolicy for spend persistence."""
        self._agent_auth = agent_auth

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    async def report(
        self,
        action: str,
        tier: str,
        score: int,
        duration_ms: float,
        agent_id: str = "unknown",
        framework: str = "unknown",
        is_mcp: bool = False,
        blocked_reason: str | None = None,
        endpoint: str = "",
        session_id: str = "",
        plan: str = "",
        step: str = "",
        params: dict | None = None,
        model: str = "",
    ) -> None:
        """Report a single tool call event to the control plane."""
        if not self.enabled:
            return

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=3.0)

        event = {
            "sidecar_id": self._sidecar_id,
            "action": action,
            "tier": tier,
            "score": score,
            "duration_ms": round(duration_ms, 2),
            "agent_id": agent_id,
            "framework": framework,
            "is_mcp": is_mcp,
            "blocked_reason": blocked_reason,
            "endpoint": endpoint,
            "session_id": session_id,
            "plan": plan,
            "step": step,
            "params": params,
            "model": model,
            "timestamp": time.time(),
        }

        try:
            await self._client.post(f"{self._url}/api/traces/ingest", json=event)
        except Exception as e:
            log.debug("Failed to report trace: %s", e)

    async def push_spend_snapshot(self) -> None:
        """Push current per-agent spend to the Control Plane for persistence."""
        if not self.enabled or not self._agent_auth:
            return
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=3.0)

        snapshot = self._agent_auth.get_spend_snapshot()
        if not snapshot:
            return

        try:
            await self._client.post(
                f"{self._url}/api/gateways/{self._sidecar_id}/spend",
                json={"spend": snapshot},
            )
            log.debug("Pushed spend snapshot: %d agents", len(snapshot))
        except Exception as e:
            log.debug("Failed to push spend snapshot: %s", e)

    async def restore_spend_from_control_plane(self) -> None:
        """Restore per-agent spend from the Control Plane on startup."""
        if not self.enabled or not self._agent_auth:
            return
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=3.0)

        try:
            resp = await self._client.get(
                f"{self._url}/api/gateways/{self._sidecar_id}/spend"
            )
            if resp.status_code == 200:
                data = resp.json()
                snapshot = data.get("spend", {})
                if snapshot:
                    self._agent_auth.restore_spend(snapshot)
                    log.info("Restored spend from Control Plane: %d agents", len(snapshot))
        except Exception as e:
            log.debug("Failed to restore spend: %s (starting fresh)", e)

    async def start_spend_persistence(self, interval_seconds: float = 30.0) -> None:
        """Start a background task that pushes spend snapshots periodically."""
        await self.restore_spend_from_control_plane()

        async def _loop():
            while True:
                await asyncio.sleep(interval_seconds)
                await self.push_spend_snapshot()

        self._spend_task = asyncio.create_task(_loop())
        log.info("Spend persistence started (every %.0fs)", interval_seconds)

    async def close(self) -> None:
        if self._spend_task:
            self._spend_task.cancel()
            self._spend_task = None
        if self._agent_auth:
            await self.push_spend_snapshot()
        if self._client:
            await self._client.aclose()
            self._client = None
