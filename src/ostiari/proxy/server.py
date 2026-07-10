"""Proxy server — validates tool calls via Guard then executes them."""

import asyncio
import logging
import time
import traceback
from pathlib import Path
from typing import Any

from ostiari import Guard
from ostiari.exceptions import ActionBlockedError
from ostiari.models import OstiariConfig
from ostiari.proxy.registry import ToolRegistry

log = logging.getLogger("ostiari.proxy")


def create_app(
    policy_path: str | Path = "policy.yaml",
    tools_config: str | Path | None = None,
    config: OstiariConfig | None = None,
    registry: ToolRegistry | None = None,
) -> Any:
    """Create the proxy FastAPI app.

    Args:
        policy_path: Path to the Ostiari policy YAML.
        tools_config: Path to the tool registry YAML config.
        config: Optional OstiariConfig override.
        registry: Optional pre-built ToolRegistry (overrides tools_config).
    """
    from contextlib import asynccontextmanager

    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    guard = Guard(config=config)
    if Path(policy_path).exists():
        guard.configure(str(policy_path))
    guard.start()

    @asynccontextmanager
    async def lifespan(app: Any) -> Any:
        yield
        guard.shutdown()

    app = FastAPI(title="Ostiari Proxy", lifespan=lifespan)

    if registry is not None:
        tool_registry = registry
    elif tools_config is not None:
        tool_registry = ToolRegistry.from_config(tools_config)
    else:
        tool_registry = ToolRegistry()

    @app.post("/tool/{action}")
    async def proxy_tool(action: str, request: Request) -> Any:
        """Validate and execute a tool call.

        The agent sends: POST /tool/<action> with JSON body of params.
        Response:
          200 — tool executed, result in body
          403 — blocked by policy
          404 — unknown tool
          500 — tool execution error
        """
        params: dict[str, Any] = await request.json()
        agent_id = request.headers.get("X-Agent-Id", "unknown")
        framework = request.headers.get("X-Framework", "unknown")

        try:
            guard.validate(
                action=action,
                params=params,
                context={"agent_id": agent_id, "framework": framework},
            )
        except ActionBlockedError as e:
            return JSONResponse(
                status_code=403,
                content={
                    "blocked": True,
                    "action": action,
                    "score": e.score,
                    "reason": e.reason,
                    "rule_id": e.rule_id,
                },
            )

        tool_fn = tool_registry.get(action)
        if tool_fn is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": f"Unknown tool: {action}",
                    "available": tool_registry.list_tools(),
                },
            )

        start = time.monotonic()
        try:
            if asyncio.iscoroutinefunction(tool_fn):
                result = await tool_fn(params)
            else:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, tool_fn, params)
            duration_ms = (time.monotonic() - start) * 1000
            return {
                "result": result,
                "action": action,
                "duration_ms": round(duration_ms, 2),
            }
        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            log.error("Tool %s failed: %s", action, e)
            return JSONResponse(
                status_code=500,
                content={
                    "error": str(e),
                    "action": action,
                    "duration_ms": round(duration_ms, 2),
                    "traceback": traceback.format_exc()
                    if log.isEnabledFor(logging.DEBUG)
                    else None,
                },
            )

    @app.post("/validate")
    async def validate_only(request: Request) -> Any:
        """Validate a tool call without executing it.

        Useful when the agent executes tools itself but wants policy checks.
        """
        body: dict[str, Any] = await request.json()
        action = body.get("action", "")
        params = body.get("params", {})
        agent_id = request.headers.get("X-Agent-Id", "unknown")
        framework = request.headers.get("X-Framework", "unknown")

        if not action:
            return JSONResponse(status_code=400, content={"error": "action is required"})

        try:
            result = guard.validate(
                action=action,
                params=params,
                context={"agent_id": agent_id, "framework": framework},
            )
            return {
                "allowed": True,
                "action": action,
                "score": result.score,
                "tier": result.tier,
            }
        except ActionBlockedError as e:
            return {
                "allowed": False,
                "action": action,
                "score": e.score,
                "tier": "block",
                "reason": e.reason,
                "rule_id": e.rule_id,
            }

    @app.get("/tools")
    async def list_tools() -> Any:
        """List all registered tools."""
        return {"tools": tool_registry.list_tools()}

    @app.get("/health")
    async def health() -> Any:
        return {"status": "ok", "tools": len(tool_registry.list_tools())}

    return app


def run_proxy(
    policy_path: str = "policy.yaml",
    tools_config: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8421,
) -> None:
    """Run the proxy server (convenience for CLI)."""
    import uvicorn

    app = create_app(policy_path=policy_path, tools_config=tools_config)
    uvicorn.run(app, host=host, port=port)
