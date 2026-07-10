"""MCP protocol types and constants."""

from typing import Any, Protocol


class MCPServerInterface(Protocol):
    """Interface that both embedded and remote MCP clients implement."""

    async def initialize(self) -> dict[str, Any]:
        """Initialize the MCP connection and return server info."""
        ...

    async def list_tools(self) -> list[dict[str, Any]]:
        """List available tools from the MCP server."""
        ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool on the MCP server and return the result."""
        ...

    async def close(self) -> None:
        """Close the connection."""
        ...
