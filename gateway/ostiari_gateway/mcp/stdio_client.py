"""Stdio MCP client — runs MCP server as a local subprocess."""

import asyncio
import json
import logging
import os
import re
from typing import Any

from ostiari_gateway.mcp.models import MCPServerConfig

log = logging.getLogger("ostiari.sidecar.mcp.stdio")

_BASE_CHILD_ENV = frozenset(
    {
        "HOME",
        "LANG",
        "LANGUAGE",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "TZ",
    }
)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def child_environment() -> dict[str, str]:
    """Build a minimal environment for an MCP subprocess.

    Provider keys, workload credentials, config-admin keys, and other gateway
    secrets are excluded by default. Operators may explicitly add names with
    ``OSTIARI_MCP_CHILD_ENV_ALLOW`` when a subprocess has a separately scoped
    credential of its own.
    """
    allowed = set(_BASE_CHILD_ENV)
    allowed.update(
        name
        for name in os.environ
        if name.startswith("LC_")
    )
    for name in os.environ.get(
        "OSTIARI_MCP_CHILD_ENV_ALLOW", ""
    ).split(","):
        name = name.strip()
        if name:
            if not _ENV_NAME.fullmatch(name):
                raise ValueError(
                    "OSTIARI_MCP_CHILD_ENV_ALLOW contains an invalid "
                    f"environment variable name: {name!r}"
                )
            allowed.add(name)
    return {
        name: os.environ[name]
        for name in sorted(allowed)
        if name in os.environ
    }


class StdioMCPClient:
    """Runs an MCP server as a subprocess, communicates via stdin/stdout.

    Use for non-Python MCP servers (Node.js, Go, etc.) that run locally.
    No network hop — IPC via pipes.
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0

    async def initialize(self) -> dict[str, Any]:
        """Spawn the MCP server subprocess and initialize."""
        if not self._config.command:
            raise ValueError(f"MCP server '{self._config.name}' has no command configured")

        self._process = await asyncio.create_subprocess_exec(
            *self._config.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_environment(),
        )

        result = await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "ostiari-sidecar", "version": "0.1.0"},
        })

        # Send initialized notification
        await self._send_notification("notifications/initialized")

        log.info("Stdio MCP server '%s' started (pid=%d)", self._config.name, self._process.pid)
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        """List tools from the subprocess MCP server."""
        result = await self._send_request("tools/list", {})
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool via the subprocess."""
        result = await self._send_request("tools/call", {"name": name, "arguments": arguments})

        if "content" in result:
            texts = []
            for block in result["content"]:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
            if texts:
                return {"content": "\n".join(texts)}
        return result

    async def close(self) -> None:
        """Terminate the subprocess."""
        if self._process is not None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
            log.info("Stdio MCP server '%s' stopped", self._config.name)
            self._process = None

    async def _send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for response."""
        if self._process is None or self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError(f"MCP server '{self._config.name}' not running")

        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }

        line = json.dumps(request) + "\n"
        self._process.stdin.write(line.encode())
        await self._process.stdin.drain()

        response_line = await asyncio.wait_for(
            self._process.stdout.readline(), timeout=30.0
        )
        response = json.loads(response_line.decode())

        if "error" in response:
            raise RuntimeError(f"MCP error: {response['error'].get('message', 'Unknown')}")

        return response.get("result", {})

    async def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if self._process is None or self._process.stdin is None:
            return

        notification = {"jsonrpc": "2.0", "method": method}
        if params:
            notification["params"] = params

        line = json.dumps(notification) + "\n"
        self._process.stdin.write(line.encode())
        await self._process.stdin.drain()
