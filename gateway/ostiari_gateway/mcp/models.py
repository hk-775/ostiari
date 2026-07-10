"""Data models for MCP server configuration."""

from pydantic import BaseModel, Field


class MCPServerConfig(BaseModel):
    """Configuration for an MCP server connection."""

    name: str
    mode: str = "embedded"  # "embedded" | "remote" | "stdio"
    # For embedded mode
    package: str = ""
    module: str = ""
    # For remote mode
    url: str = ""
    # For stdio mode
    command: list[str] = Field(default_factory=list)
    # Shared config passed to the MCP server
    config: dict = Field(default_factory=dict)
    # Tool filtering
    allowed_tools: list[str] | None = None
    blocked_tools: list[str] = Field(default_factory=list)
    # Tool name prefix (defaults to server name)
    prefix: str = ""


class MCPTool(BaseModel):
    """A tool discovered from an MCP server."""

    name: str
    description: str = ""
    input_schema: dict = Field(default_factory=dict)
    server_name: str = ""
    qualified_name: str = ""  # {prefix}.{name}
