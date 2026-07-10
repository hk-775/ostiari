"""Embedded MCP client — loads MCP server as a Python package in-process."""

import importlib
import logging
from typing import Any

from ostiari_gateway.mcp.models import MCPServerConfig

log = logging.getLogger("ostiari.sidecar.mcp.embedded")


class EmbeddedMCPClient:
    """Runs an MCP server in-process by importing it as a Python module.

    Zero network hops — tool calls are direct function calls.
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config
        self._server: Any = None
        self._tools: list[dict[str, Any]] = []

    async def initialize(self) -> dict[str, Any]:
        """Load the MCP server module and initialize it."""
        module_path = self._config.module or self._resolve_module(self._config.package)

        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            raise ImportError(
                f"Cannot load MCP server package '{self._config.package}': {e}. "
                f"Install with: pip install {self._config.package}"
            ) from e

        # Look for standard MCP server entry points
        if hasattr(module, "create_server"):
            self._server = module.create_server(**self._config.config)
        elif hasattr(module, "Server"):
            self._server = module.Server(**self._config.config)
        elif hasattr(module, "server"):
            self._server = module.server
        else:
            raise AttributeError(
                f"Module '{module_path}' has no 'create_server', 'Server', or 'server' attribute"
            )

        # Initialize the server if it has an init method
        if hasattr(self._server, "initialize"):
            info = await self._server.initialize()
            log.info("Embedded MCP server '%s' initialized", self._config.name)
            return info if isinstance(info, dict) else {"name": self._config.name}

        log.info("Embedded MCP server '%s' loaded", self._config.name)
        return {"name": self._config.name}

    async def list_tools(self) -> list[dict[str, Any]]:
        """Get tools from the embedded server."""
        if self._server is None:
            return []

        if hasattr(self._server, "list_tools"):
            result = self._server.list_tools()
            if hasattr(result, "__await__"):
                result = await result
            # Handle different return formats
            if isinstance(result, dict) and "tools" in result:
                self._tools = result["tools"]
            elif isinstance(result, list):
                self._tools = result
            else:
                self._tools = []
        elif hasattr(self._server, "tools"):
            tools_attr = self._server.tools
            if callable(tools_attr):
                self._tools = tools_attr()
            else:
                self._tools = tools_attr
        else:
            self._tools = []

        return self._tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool on the embedded server — direct function call, no network."""
        if self._server is None:
            return {"error": f"Server '{self._config.name}' not initialized"}

        if hasattr(self._server, "call_tool"):
            result = self._server.call_tool(name, arguments)
            if hasattr(result, "__await__"):
                result = await result
            return self._normalize_result(result)

        # Try calling the tool as a method on the server
        tool_fn = getattr(self._server, name, None)
        if tool_fn is not None and callable(tool_fn):
            result = tool_fn(**arguments)
            if hasattr(result, "__await__"):
                result = await result
            return self._normalize_result(result)

        return {"error": f"Tool '{name}' not found on server '{self._config.name}'"}

    async def close(self) -> None:
        """Shut down the embedded server."""
        if self._server is not None and hasattr(self._server, "close"):
            close_result = self._server.close()
            if hasattr(close_result, "__await__"):
                await close_result
        self._server = None

    def _resolve_module(self, package: str) -> str:
        """Convert package name to importable module path."""
        # mcp-server-github → mcp_server_github
        return package.replace("-", "_")

    def _normalize_result(self, result: Any) -> dict[str, Any]:
        """Normalize MCP call result to a standard dict format."""
        if isinstance(result, dict):
            return result
        if isinstance(result, list):
            # MCP content blocks format
            texts = []
            for item in result:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(item.get("text", ""))
                elif isinstance(item, str):
                    texts.append(item)
            return {"content": "\n".join(texts) if texts else str(result)}
        if isinstance(result, str):
            return {"content": result}
        return {"content": str(result)}
