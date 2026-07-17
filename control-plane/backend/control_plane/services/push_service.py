"""Push service — syncs configuration to gateways."""

import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.models.database import Gateway, McpServer, Policy, Tool
from control_plane.models.schemas import PushResponse, PushResult

log = logging.getLogger("control_plane.push")


class PushService:
    """Pushes configuration to registered gateways."""

    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout

    async def push_to_gateway(self, db: AsyncSession, gateway_id: str) -> PushResult:
        """Build and push full config to a single gateway."""
        gateway = await db.get(Gateway, gateway_id)
        if gateway is None:
            return PushResult(gateway_id=gateway_id, status="error", message="Gateway not found")

        config = await self._build_config(db, gateway)

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(f"{gateway.endpoint}/config", json=config)
                if resp.status_code == 200:
                    return PushResult(gateway_id=gateway_id, status="success")
                else:
                    return PushResult(
                        gateway_id=gateway_id, status="error",
                        message=f"HTTP {resp.status_code}: {resp.text[:200]}"
                    )
            except httpx.ConnectError as e:
                return PushResult(gateway_id=gateway_id, status="error", message=f"Unreachable: {e}")
            except httpx.TimeoutException:
                return PushResult(gateway_id=gateway_id, status="error", message="Timeout")

    async def push_to_all(self, db: AsyncSession) -> PushResponse:
        """Push config to all registered gateways."""
        result = await db.execute(select(Gateway))
        gateways = result.scalars().all()

        results = []
        for gateway in gateways:
            r = await self.push_to_gateway(db, gateway.id)
            results.append(r)

        succeeded = sum(1 for r in results if r.status == "success")
        return PushResponse(
            results=results,
            total=len(results),
            succeeded=succeeded,
            failed=len(results) - succeeded,
        )

    async def push_policy(self, db: AsyncSession, gateway_id: str, policy: Policy) -> PushResult:
        """Push only policy to a gateway."""
        gateway = await db.get(Gateway, gateway_id)
        if gateway is None:
            return PushResult(gateway_id=gateway_id, status="error", message="Gateway not found")

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(
                    f"{gateway.endpoint}/config/policy", json=policy.content
                )
                if resp.status_code == 200:
                    return PushResult(gateway_id=gateway_id, status="success")
                else:
                    return PushResult(
                        gateway_id=gateway_id, status="error",
                        message=f"HTTP {resp.status_code}"
                    )
            except Exception as e:
                return PushResult(gateway_id=gateway_id, status="error", message=str(e))

    async def _build_config(self, db: AsyncSession, gateway: Gateway) -> dict[str, Any]:
        """Build the full gateway config from database state."""
        # Get tools for this gateway
        result = await db.execute(select(Tool).where(Tool.gateway_id == gateway.id))
        tools = result.scalars().all()

        # Get active policy for this gateway
        result = await db.execute(
            select(Policy).where(
                (Policy.gateway_id == gateway.id) | (Policy.gateway_id.is_(None)),
                Policy.is_active == True,
            )
        )
        policies = result.scalars().all()

        # Merge policies (gateway-specific overrides global)
        merged_policy: dict[str, Any] = {}
        for p in sorted(policies, key=lambda x: x.gateway_id is not None):
            merged_policy.update(p.content)

        # Get MCP servers for this gateway
        result = await db.execute(select(McpServer).where(McpServer.gateway_id == gateway.id))
        mcp_servers = result.scalars().all()

        config: dict[str, Any] = {
            "gateway_id": gateway.id,
            "tools": [
                {
                    "name": t.name,
                    "endpoint": t.endpoint,
                    "method": t.method,
                    "description": t.description,
                    "timeout_seconds": t.timeout_seconds,
                    "schema": t.schema_json,
                }
                for t in tools
            ],
            "policy": merged_policy,
            "mcp_servers": [
                {
                    "name": m.name,
                    "mode": m.mode,
                    "package": m.package,
                    "module": m.module,
                    "url": m.url,
                    "command": m.command,
                    "config": m.config,
                    "allowed_tools": m.allowed_tools,
                    "blocked_tools": m.blocked_tools,
                    "prefix": m.prefix,
                }
                for m in mcp_servers
            ],
        }

        # Payment config (x402 pricing + wallet balances), if any.
        from control_plane.routers.payments import build_payment_config
        payments = await build_payment_config(db, gateway.id)
        if payments.get("mode", "off") != "off" or payments.get("wallets"):
            config["payments"] = payments

        # Include any stored gateway-level config (modules, llm, mode, etc.)
        if gateway.config:
            config.update(gateway.config)

        # Enforcement mode is always sent explicitly (defaults to enforce) so a
        # gateway can never be left on a stale shadow setting after a push.
        config["mode"] = (gateway.config or {}).get("mode", "enforce")

        return config
