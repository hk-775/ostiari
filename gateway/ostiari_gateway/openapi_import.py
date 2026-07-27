"""Generate gateway ToolDefinitions from an OpenAPI spec.

Thin wrapper over the shared ``ostiari.openapi_import`` parser (which lives in
the root package so both planes can use it without depending on each other).
This module just wraps the parsed tool-spec dicts in ``ToolDefinition``s.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ostiari.openapi_import import OpenAPIError, fetch_spec_text, is_url, parse_spec
from ostiari_gateway.models import ToolDefinition

__all__ = ["OpenAPIError", "GeneratedTool", "generate_tools", "import_openapi"]


@dataclass
class GeneratedTool:
    """A ToolDefinition plus the metadata a caller may want to preview."""

    tool: ToolDefinition
    operation_id: str
    method: str
    path: str
    summary: str = ""
    param_locations: dict[str, str] = field(default_factory=dict)


def _to_tool(spec: dict[str, Any]) -> ToolDefinition:
    return ToolDefinition(
        name=spec["name"],
        endpoint=spec["endpoint"],
        method=spec["method"],
        description=spec["description"],
        timeout_seconds=spec["timeout_seconds"],
        schema=spec["schema"],
        path_params=spec["path_params"],
        query_params=spec["query_params"],
    )


def generate_tools(
    spec: str | dict[str, Any],
    *,
    server_url: str | None = None,
    default_timeout: float = 30.0,
    name_prefix: str = "",
) -> list[GeneratedTool]:
    """Parse an OpenAPI 3.x spec into GeneratedTool objects.

    Raises:
        OpenAPIError: if the spec is unparseable or has no operations.
    """
    parsed = parse_spec(spec, server_url=server_url,
                        default_timeout=default_timeout, name_prefix=name_prefix)
    return [
        GeneratedTool(
            tool=_to_tool(p), operation_id=p["operation_id"], method=p["method"],
            path=p["path"], summary=p["summary"], param_locations=p["param_locations"],
        )
        for p in parsed
    ]


def import_openapi(
    source: str | dict[str, Any],
    *,
    server_url: str | None = None,
    name_prefix: str = "",
    timeout: float = 15.0,
) -> list[ToolDefinition]:
    """Convenience: fetch (if a URL) and return just the ToolDefinitions."""
    spec: str | dict[str, Any] = source
    if isinstance(source, str) and is_url(source):
        spec = fetch_spec_text(source, timeout=timeout)
    return [gt.tool for gt in generate_tools(spec, server_url=server_url, name_prefix=name_prefix)]
