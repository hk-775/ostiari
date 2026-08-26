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
import os
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ostiari.bounded_http import (
    decode_json_or_text,
    max_response_bytes,
    request_limited,
    timeout_seconds,
)
from ostiari.http_limits import BodySizeLimitMiddleware
from ostiari_gateway import __version__, oidc

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

    async def _request(
        self,
        method: str,
        path: str,
        *,
        agent_id: str,
        authorization: str | None,
        json_body: dict[str, Any] | None = None,
    ):
        headers = {
            "X-Agent-Id": agent_id,
            "X-Framework": "mcp-bridge",
        }
        if authorization:
            headers["Authorization"] = authorization
        async with httpx.AsyncClient(follow_redirects=False) as client:
            return await request_limited(
                client,
                method,
                f"{self._gateway}{path}",
                headers=headers,
                json=json_body,
                deadline_seconds=timeout_seconds(
                    "OSTIARI_MCP_GATEWAY_TIMEOUT_SECONDS",
                    default=30.0,
                ),
                max_bytes=max_response_bytes("OSTIARI_MCP_MAX_RESPONSE_BYTES"),
            )

    async def _list_tools(
        self, agent_id: str, authorization: str | None
    ) -> list[dict[str, Any]]:
        """Fetch the gateway's registered tools and shape them as MCP tool specs."""
        response = await self._request(
            "GET",
            "/tools",
            agent_id=agent_id,
            authorization=authorization,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"gateway returned HTTP {response.status_code}")
        data = decode_json_or_text(response.content)
        if not isinstance(data, (dict, list)):
            raise RuntimeError("gateway returned an invalid tool catalog")
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

    async def _call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        agent_id: str,
        authorization: str | None,
    ) -> dict[str, Any]:
        """Forward a tool call through the governed gateway proxy."""
        response = await self._request(
            "POST",
            f"/tool/{name}",
            agent_id=agent_id,
            authorization=authorization,
            json_body=arguments or {},
        )
        # 403/402/429 → the gate chain blocked it. Surface as an MCP tool error
        # (isError) so the calling model sees the governance decision, not a crash.
        body = decode_json_or_text(response.content)
        if response.status_code >= 400:
            detail = body if isinstance(body, dict) else {"error": str(body)[:300]}
            reason = (
                detail.get("reason")
                or detail.get("error")
                or f"HTTP {response.status_code}"
            )
            return {
                "content": [{"type": "text", "text": f"BLOCKED by Ostiari: {reason}"}],
                "isError": True,
            }
        if not isinstance(body, dict):
            body = {"result": body}
        result = body.get("result", body)
        text = result if isinstance(result, str) else _json_dumps(result)
        return {"content": [{"type": "text", "text": text}], "isError": False}

    async def handle(
        self,
        message: dict[str, Any],
        *,
        agent_id: str | None = None,
        authorization: str | None = None,
    ) -> dict[str, Any] | None:
        """Dispatch a single JSON-RPC message. Returns None for notifications."""
        effective_agent_id = agent_id or self._agent_id
        method = message.get("method")
        req_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            return _result(req_id, {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self._name, "version": __version__},
            })

        if method == "notifications/initialized" or req_id is None:
            return None  # notification — no response

        if method == "tools/list":
            try:
                tools = await self._list_tools(effective_agent_id, authorization)
            except Exception as e:  # noqa: BLE001
                return _error(req_id, -32603, f"could not list tools: {e}")
            return _result(req_id, {"tools": tools})

        if method == "tools/call":
            name = params.get("name")
            if not name:
                return _error(req_id, -32602, "missing tool name")
            try:
                res = await self._call_tool(
                    name,
                    params.get("arguments") or {},
                    effective_agent_id,
                    authorization,
                )
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


def _check_production_posture() -> None:
    if os.environ.get("OSTIARI_ENV", "").strip().lower() not in {"production", "prod"}:
        return
    errors = []
    if not oidc.auth_required():
        errors.append("OSTIARI_GATEWAY_AUTH must be exactly 'required'")
    if not os.environ.get("OSTIARI_OIDC_ISSUER", "").startswith("https://"):
        errors.append("OSTIARI_OIDC_ISSUER must be an HTTPS issuer")
    if not os.environ.get("OSTIARI_OIDC_AUDIENCE", "").strip():
        errors.append("OSTIARI_OIDC_AUDIENCE must be set")
    if errors:
        raise RuntimeError(
            "Refusing insecure production MCP bridge configuration: "
            + "; ".join(errors)
        )


def _authenticate(request: Request, default_agent_id: str):
    """Authenticate an MCP caller and return identity plus its verified token."""
    validator = oidc.get_validator()
    if validator is None:
        if oidc.auth_required():
            return JSONResponse(
                status_code=503,
                content={"error": "MCP authentication is misconfigured"},
            )
        return default_agent_id, None

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content={"error": "authentication required"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = validator.validate(header.removeprefix("Bearer ").strip())
    except oidc.OIDCError as exc:
        return JSONResponse(
            status_code=401,
            content={"error": "invalid token", "detail": str(exc)},
            headers={"WWW-Authenticate": "Bearer"},
        )

    agent_id = oidc.agent_id_from_claims(claims)
    if not agent_id:
        return JSONResponse(
            status_code=403,
            content={"error": "validated token has no agent identity claim"},
        )
    claimed_agent = request.headers.get("X-Agent-Id", "").strip()
    if claimed_agent and claimed_agent != agent_id:
        return JSONResponse(
            status_code=403,
            content={"error": "token identity does not match X-Agent-Id"},
        )
    return agent_id, header


def create_bridge_app(gateway_url: str, server_name: str = "ostiari-bridge",
                      agent_id: str = "mcp-bridge") -> FastAPI:
    """Create the MCP bridge FastAPI app for a given Ostiari gateway."""
    _check_production_posture()
    bridge = MCPBridge(gateway_url, server_name=server_name, agent_id=agent_id)
    app = FastAPI(title="Ostiari MCP Bridge")
    app.add_middleware(BodySizeLimitMiddleware)

    @app.post("/mcp")
    async def mcp_endpoint(request: Request) -> Any:
        """MCP Streamable-HTTP endpoint (JSON-RPC 2.0, single or batch)."""
        identity = _authenticate(request, bridge._agent_id)
        if isinstance(identity, JSONResponse):
            return identity
        request_agent_id, authorization = identity
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(status_code=400,
                                content=_error(None, -32700, "parse error"))

        if isinstance(payload, list):  # JSON-RPC batch
            if not all(isinstance(message, dict) for message in payload):
                return JSONResponse(
                    status_code=400,
                    content=_error(None, -32600, "invalid request"),
                )
            responses = [
                response
                for message in payload
                if (
                    response := await bridge.handle(
                        message,
                        agent_id=request_agent_id,
                        authorization=authorization,
                    )
                )
                is not None
            ]
            return JSONResponse(content=responses) if responses else JSONResponse(content=[], status_code=202)

        if not isinstance(payload, dict):
            return JSONResponse(
                status_code=400,
                content=_error(None, -32600, "invalid request"),
            )
        response = await bridge.handle(
            payload,
            agent_id=request_agent_id,
            authorization=authorization,
        )
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
