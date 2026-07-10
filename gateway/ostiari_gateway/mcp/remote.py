"""Remote MCP client — connects to an external MCP server over HTTP/SSE."""

import logging
from typing import Any

import httpx

from ostiari_gateway.mcp.models import MCPServerConfig

log = logging.getLogger("ostiari.sidecar.mcp.remote")


class RemoteMCPClient:
    """Connects to a remote MCP server via HTTP/SSE transport.

    Use when the MCP server runs as a separate service (different container,
    different machine, managed service).
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config
        self._url = config.url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=30.0)
        self._session_id: str | None = None

    async def initialize(self) -> dict[str, Any]:
        """Initialize connection to the remote MCP server."""
        resp = await self._client.post(
            self._url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "ostiari-sidecar", "version": "0.1.0"},
                },
            },
        )
        resp.raise_for_status()
        result = resp.json()

        # Send initialized notification
        await self._client.post(
            self._url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

        log.info("Remote MCP server '%s' connected at %s", self._config.name, self._url)
        return result.get("result", {})

    async def list_tools(self) -> list[dict[str, Any]]:
        """List tools from the remote MCP server."""
        resp = await self._client.post(
            self._url,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
        )
        resp.raise_for_status()
        result = resp.json()
        return result.get("result", {}).get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool on the remote MCP server."""
        resp = await self._client.post(
            self._url,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        resp.raise_for_status()
        result = resp.json()

        if "error" in result:
            return {"error": result["error"].get("message", "MCP error")}

        mcp_result = result.get("result", {})
        content = mcp_result.get("content", [])

        # Extract text from content blocks
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
        if texts:
            return {"content": "\n".join(texts)}
        return mcp_result

    async def close(self) -> None:
        """Close the remote connection."""
        await self._client.aclose()
        log.info("Remote MCP server '%s' disconnected", self._config.name)
