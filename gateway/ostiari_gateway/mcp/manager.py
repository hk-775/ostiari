"""MCP Manager — discovers, connects, and manages MCP server lifecycle."""

import logging
import os
from typing import Any

from ostiari_gateway.mcp.models import MCPServerConfig, MCPTool
from ostiari_gateway.mcp.protocol import MCPServerInterface

log = logging.getLogger("ostiari.sidecar.mcp")


def _production_local_mcp_allowed() -> bool:
    production = os.environ.get("OSTIARI_ENV", "").strip().lower() in {
        "prod",
        "production",
    }
    explicitly_allowed = os.environ.get(
        "OSTIARI_ALLOW_LOCAL_MCP", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    return not production or explicitly_allowed


class MCPManager:
    """Manages MCP server connections and tool discovery.

    Supports three modes per server:
      - embedded: Python MCP server imported and run in-process (fastest)
      - remote: External MCP server connected via HTTP/SSE
      - stdio: Local MCP server run as a subprocess
    """

    def __init__(self) -> None:
        self._clients: dict[str, MCPServerInterface] = {}
        self._configs: dict[str, MCPServerConfig] = {}
        self._tools: dict[str, MCPTool] = {}  # qualified_name → MCPTool
        self._tool_to_server: dict[str, str] = {}  # qualified_name → server_name

    async def add_server(self, config: MCPServerConfig) -> dict[str, Any]:
        """Add and connect to an MCP server, discover its tools."""
        # Remove existing if reconfiguring
        if config.name in self._clients:
            await self.remove_server(config.name)

        client = self._create_client(config)
        self._configs[config.name] = config
        self._clients[config.name] = client

        # Initialize connection
        try:
            await client.initialize()
        except Exception as e:
            log.error("Failed to initialize MCP server '%s': %s", config.name, e)
            del self._clients[config.name]
            del self._configs[config.name]
            return {"server": config.name, "status": "error", "error": str(e)}

        # Discover tools
        tools = await self._discover_tools(config.name, client, config)

        return {
            "server": config.name,
            "mode": config.mode,
            "status": "connected",
            "tools_discovered": len(tools),
            "tools": [t.qualified_name for t in tools],
        }

    async def remove_server(self, name: str) -> bool:
        """Disconnect and remove an MCP server and its tools."""
        client = self._clients.pop(name, None)
        self._configs.pop(name, None)

        # Remove all tools from this server
        tools_to_remove = [qn for qn, sn in self._tool_to_server.items() if sn == name]
        for qn in tools_to_remove:
            del self._tools[qn]
            del self._tool_to_server[qn]

        if client is not None:
            await client.close()
            log.info("Removed MCP server '%s' (%d tools removed)", name, len(tools_to_remove))
            return True
        return False

    async def call_tool(self, qualified_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call an MCP tool by its qualified name (server.tool_name)."""
        server_name = self._tool_to_server.get(qualified_name)
        if server_name is None:
            return {"error": f"MCP tool '{qualified_name}' not found"}

        client = self._clients.get(server_name)
        if client is None:
            return {"error": f"MCP server '{server_name}' not connected"}

        tool = self._tools.get(qualified_name)
        if tool is None:
            return {"error": f"Tool '{qualified_name}' not registered"}

        # Call using the raw tool name (without prefix)
        try:
            result = await client.call_tool(tool.name, arguments)
            return result
        except Exception as e:
            log.error("MCP tool '%s' call failed: %s", qualified_name, e)
            return {"error": f"Tool call failed: {e}"}

    def get_tool(self, qualified_name: str) -> MCPTool | None:
        """Get tool metadata by qualified name."""
        return self._tools.get(qualified_name)

    def has_tool(self, qualified_name: str) -> bool:
        """Check if a qualified tool name is an MCP tool."""
        return qualified_name in self._tools

    def list_tools(self) -> list[dict[str, Any]]:
        """List all MCP tools across all connected servers."""
        return [
            {
                "name": t.qualified_name,
                "description": t.description,
                "server": t.server_name,
                "input_schema": t.input_schema,
            }
            for t in self._tools.values()
        ]

    def list_servers(self) -> list[dict[str, Any]]:
        """List all connected MCP servers."""
        servers = []
        for name, config in self._configs.items():
            tool_count = sum(1 for sn in self._tool_to_server.values() if sn == name)
            servers.append({
                "name": name,
                "mode": config.mode,
                "connected": name in self._clients,
                "tools_count": tool_count,
            })
        return servers

    async def refresh_tools(self, server_name: str) -> list[MCPTool]:
        """Re-discover tools from a server (e.g., after server update)."""
        client = self._clients.get(server_name)
        config = self._configs.get(server_name)
        if client is None or config is None:
            return []

        # Remove old tools from this server
        tools_to_remove = [qn for qn, sn in self._tool_to_server.items() if sn == server_name]
        for qn in tools_to_remove:
            del self._tools[qn]
            del self._tool_to_server[qn]

        return await self._discover_tools(server_name, client, config)

    async def shutdown(self) -> None:
        """Close all MCP connections."""
        for name in list(self._clients.keys()):
            await self.remove_server(name)

    def _create_client(self, config: MCPServerConfig) -> MCPServerInterface:
        """Create the appropriate client based on mode."""
        if (
            config.mode in {"embedded", "stdio"}
            and not _production_local_mcp_allowed()
        ):
            raise RuntimeError(
                f"Local MCP mode {config.mode!r} is disabled in production. "
                "Use an isolated remote MCP server, or explicitly set "
                "OSTIARI_ALLOW_LOCAL_MCP=true after accepting the code-"
                "execution risk."
            )
        if config.mode == "embedded":
            from ostiari_gateway.mcp.embedded import EmbeddedMCPClient
            return EmbeddedMCPClient(config)
        elif config.mode == "remote":
            from ostiari_gateway.mcp.remote import RemoteMCPClient
            return RemoteMCPClient(config)
        elif config.mode == "stdio":
            from ostiari_gateway.mcp.stdio_client import StdioMCPClient
            return StdioMCPClient(config)
        else:
            raise ValueError(f"Unknown MCP mode: {config.mode}. Use 'embedded', 'remote', or 'stdio'")

    async def _discover_tools(
        self, server_name: str, client: MCPServerInterface, config: MCPServerConfig
    ) -> list[MCPTool]:
        """Discover tools from a server and register them."""
        try:
            raw_tools = await client.list_tools()
        except Exception as e:
            log.warning("Failed to discover tools from '%s': %s", server_name, e)
            return []

        prefix = config.prefix or config.name
        discovered = []

        for raw in raw_tools:
            name = raw.get("name", "") if isinstance(raw, dict) else getattr(raw, "name", "")
            description = raw.get("description", "") if isinstance(raw, dict) else getattr(raw, "description", "")
            input_schema = raw.get("inputSchema", {}) if isinstance(raw, dict) else getattr(raw, "input_schema", {})

            # Apply tool filtering
            if config.allowed_tools is not None and name not in config.allowed_tools:
                continue
            if name in config.blocked_tools:
                continue

            qualified_name = f"{prefix}.{name}"
            tool = MCPTool(
                name=name,
                description=description,
                input_schema=input_schema,
                server_name=server_name,
                qualified_name=qualified_name,
            )
            self._tools[qualified_name] = tool
            self._tool_to_server[qualified_name] = server_name
            discovered.append(tool)

        log.info(
            "Discovered %d tools from MCP server '%s' (mode=%s)",
            len(discovered), server_name, config.mode,
        )
        return discovered
