"""MCP bridge server — expose an Ostiari gateway's tools over the MCP protocol.

The OpenAPI importer turns REST endpoints into governed Ostiari tools. This
bridge makes those same tools reachable by *external* MCP clients (Claude
Desktop, IDEs, other agents) without giving up governance: every ``tools/call``
is forwarded to the gateway's ``POST /tool/{name}`` and therefore passes through
the full gate chain (risk, quota, HITL, anomaly, trust, tracing).

MCP's Streamable-HTTP transport is JSON-RPC 2.0 over a single POST endpoint, so
this is implemented directly — no MCP SDK dependency. It handles the minimal
method set an MCP client needs: ``initialize``, ``notifications/initialized``,
``tools/list``, and ``tools/call``.

Run standalone::

    python -m ostiari_gateway.mcp_bridge --gateway http://localhost:8421 --port 8600
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("ostiari.sidecar.mcp_bridge")

_PROTOCOL_VERSION = "2024-11-05"
_JSONRPC = "2.0"


def _result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": _JSONRPC, "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": _JSONRPC, "id": req_id, "error": {"code": code, "message": message}}


class MCPBridge:
    """Translates MCP JSON-RPC calls into Ostiari gateway tool calls."""

    def __init__(self, gateway_url: str, server_name: str = "ostiari-bridge",
                 agent_id: str = "mcp-bridge") -> None:
        self._gateway = gateway_url.rstrip("/")
        self._name = server_name
        self._agent_id = agent_id

    async def _list_tools(self) -> list[dict[str, Any]]:
        """Fetch the gateway's registered tools and shape them as MCP tool specs."""
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{self._gateway}/tools")
            r.raise_for_status()
            data = r.json()
        tools = data.get("tools", data) if isinstance(data, dict) else data
        out: list[dict[str, Any]] = []
        for t in tools:
            out.append({
                "name": t["name"],
                "description": t.get("description", ""),
                # MCP expects an inputSchema; fall back to a permissive object.
                "inputSchema": t.get("schema") or {"type": "object", "properties": {}},
            })
        return out

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Forward a tool call through the governed gateway proxy."""
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(
                f"{self._gateway}/tool/{name}",
                json=arguments or {},
                headers={"X-Agent-Id": self._agent_id, "X-Framework": "mcp-bridge"},
            )
        # 403/402/429 → the gate chain blocked it. Surface as an MCP tool error
        # (isError) so the calling model sees the governance decision, not a crash.
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = {"error": r.text[:300]}
            reason = detail.get("reason") or detail.get("error") or f"HTTP {r.status_code}"
            return {
                "content": [{"type": "text", "text": f"BLOCKED by Ostiari: {reason}"}],
                "isError": True,
            }
        body = r.json()
        result = body.get("result", body)
        text = result if isinstance(result, str) else _json_dumps(result)
        return {"content": [{"type": "text", "text": text}], "isError": False}

    async def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch a single JSON-RPC message. Returns None for notifications."""
        method = message.get("method")
        req_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            return _result(req_id, {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self._name, "version": "0.1.0"},
            })

        if method == "notifications/initialized" or req_id is None:
            return None  # notification — no response

        if method == "tools/list":
            try:
                tools = await self._list_tools()
            except Exception as e:  # noqa: BLE001
                return _error(req_id, -32603, f"could not list tools: {e}")
            return _result(req_id, {"tools": tools})

        if method == "tools/call":
            name = params.get("name")
            if not name:
                return _error(req_id, -32602, "missing tool name")
            try:
                res = await self._call_tool(name, params.get("arguments") or {})
            except Exception as e:  # noqa: BLE001
                return _error(req_id, -32603, f"tool call failed: {e}")
            return _result(req_id, res)

        if method == "ping":
            return _result(req_id, {})

        return _error(req_id, -32601, f"method not found: {method}")


def _json_dumps(obj: Any) -> str:
    import json
    try:
        return json.dumps(obj)
    except Exception:
        return str(obj)


def create_bridge_app(gateway_url: str, server_name: str = "ostiari-bridge",
                      agent_id: str = "mcp-bridge") -> FastAPI:
    """Create the MCP bridge FastAPI app for a given Ostiari gateway."""
    bridge = MCPBridge(gateway_url, server_name=server_name, agent_id=agent_id)
    app = FastAPI(title="Ostiari MCP Bridge")

    @app.post("/mcp")
    async def mcp_endpoint(request: Request) -> Any:
        """MCP Streamable-HTTP endpoint (JSON-RPC 2.0, single or batch)."""
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(status_code=400,
                                content=_error(None, -32700, "parse error"))

        if isinstance(payload, list):  # JSON-RPC batch
            responses = [r for msg in payload if (r := await bridge.handle(msg)) is not None]
            return JSONResponse(content=responses) if responses else JSONResponse(content=[], status_code=202)

        response = await bridge.handle(payload)
        if response is None:
            return JSONResponse(content={}, status_code=202)  # notification ack
        return JSONResponse(content=response)

    @app.get("/health")
    async def health() -> Any:
        return {"status": "ok", "bridge_for": bridge._gateway, "server": server_name}

    return app


def main() -> None:
    import click
    import uvicorn

    @click.command()
    @click.option("--gateway", required=True, help="Ostiari gateway base URL (e.g. http://localhost:8421)")
    @click.option("--host", default="0.0.0.0", help="Bind host")
    @click.option("--port", default=8600, type=int, help="Bind port")
    @click.option("--agent-id", default="mcp-bridge", help="X-Agent-Id used for governed calls")
    @click.option("--name", "server_name", default="ostiari-bridge", help="MCP server name")
    def cli(gateway: str, host: str, port: int, agent_id: str, server_name: str) -> None:
        app = create_bridge_app(gateway, server_name=server_name, agent_id=agent_id)
        click.echo(f"Ostiari MCP Bridge → {gateway}")
        click.echo(f"  MCP endpoint: http://{host}:{port}/mcp")
        uvicorn.run(app, host=host, port=port, log_level="info")

    cli()


if __name__ == "__main__":
    main()
