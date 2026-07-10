"""Tool proxy — executes tools by forwarding HTTP requests to remote endpoints."""

import logging
import time
from typing import Any

import httpx

from ostiari_gateway.models import ToolDefinition

log = logging.getLogger("ostiari.sidecar")


class ToolProxy:
    """Manages tool definitions and proxies calls to their endpoints."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._client = httpx.AsyncClient()

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool
        log.info("Registered tool: %s → %s %s", tool.name, tool.method, tool.endpoint)

    def unregister(self, name: str) -> bool:
        if name in self._tools:
            del self._tools[name]
            log.info("Unregistered tool: %s", name)
            return True
        return False

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": t.name,
                "endpoint": t.endpoint,
                "method": t.method,
                "description": t.description,
                "timeout_seconds": t.timeout_seconds,
            }
            for t in self._tools.values()
        ]

    def clear(self) -> None:
        self._tools.clear()

    async def execute(
        self, name: str, params: dict[str, Any], propagate_headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Proxy a tool call to its remote endpoint.

        Args:
            name: Tool name to execute.
            params: Parameters to send as JSON body.
            propagate_headers: Extra headers to forward (e.g., traceparent for OTel).
                              Passed through regardless of whether the tool supports them.
        """
        tool = self._tools.get(name)
        if tool is None:
            return {
                "error": f"Unknown tool: {name}",
                "available": [t.name for t in self._tools.values()],
                "status_code": 404,
            }

        headers = {"Content-Type": "application/json", **tool.headers}
        if propagate_headers:
            headers.update(propagate_headers)

        start = time.monotonic()

        try:
            response = await self._client.request(
                method=tool.method,
                url=tool.endpoint,
                json=params,
                headers=headers,
                timeout=tool.timeout_seconds,
            )
            duration_ms = (time.monotonic() - start) * 1000

            try:
                body = response.json()
            except Exception:
                body = response.text

            return {
                "result": body,
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 2),
            }
        except httpx.TimeoutException:
            duration_ms = (time.monotonic() - start) * 1000
            return {
                "error": f"Tool {name} timed out after {tool.timeout_seconds}s",
                "status_code": 504,
                "duration_ms": round(duration_ms, 2),
            }
        except httpx.ConnectError as e:
            duration_ms = (time.monotonic() - start) * 1000
            return {
                "error": f"Cannot reach tool endpoint: {e}",
                "status_code": 502,
                "duration_ms": round(duration_ms, 2),
            }

    async def close(self) -> None:
        await self._client.aclose()
