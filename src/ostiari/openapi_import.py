"""Parse an OpenAPI (Swagger) spec into tool specifications.

Lives in the shared ``ostiari`` package so both the gateway and the control
plane can generate tools from a spec without either service depending on the
other. The output is plain dicts (not gateway models) — each caller wraps them
in whatever representation it needs (``ToolDefinition`` in the gateway, DB rows
in the control plane).

One tool per OpenAPI operation (path × method). The only wrinkle over a normal
tool is *parameter placement*: REST operations split inputs across the URL path
(``/orders/{id}``), the query string (``?status=open``), and the request body.
Each generated spec records ``path_params`` / ``query_params`` so a proxy can
rebuild the real HTTP call from a single flat argument dict.

Pure parsing — no network required for an inline spec. ``fetch_spec_text``
fetches a URL when a caller wants that.
"""

from __future__ import annotations

import json
import re
from typing import Any

_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_.]+")


class OpenAPIError(ValueError):
    """Raised when a spec can't be parsed into tools."""


def _load(spec: str | dict[str, Any]) -> dict[str, Any]:
    """Accept a dict, a JSON string, or a YAML string; return the spec dict."""
    if isinstance(spec, dict):
        return spec
    text = (spec or "").strip()
    if not text:
        raise OpenAPIError("Empty spec")
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        import yaml

        loaded = yaml.safe_load(text)
    except Exception as e:  # noqa: BLE001
        raise OpenAPIError(f"Spec is neither valid JSON nor YAML: {e}") from e
    if not isinstance(loaded, dict):
        raise OpenAPIError("Spec did not parse to an object")
    return loaded


def _resolve_ref(spec: dict[str, Any], ref: str) -> dict[str, Any]:
    """Resolve a local ``#/components/...`` JSON reference. Non-local refs -> {}."""
    if not ref.startswith("#/"):
        return {}
    node: Any = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")  # JSON-pointer unescape
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node if isinstance(node, dict) else {}


def _deref(spec: dict[str, Any], node: Any, _depth: int = 0) -> Any:
    """Recursively inline local ``$ref``s in a schema fragment (bounded depth)."""
    if _depth > 25 or not isinstance(node, dict):
        return node
    if "$ref" in node:
        resolved = _resolve_ref(spec, node["$ref"])
        merged = {**resolved, **{k: v for k, v in node.items() if k != "$ref"}}
        return _deref(spec, merged, _depth + 1)
    out: dict[str, Any] = {}
    for k, v in node.items():
        if isinstance(v, dict):
            out[k] = _deref(spec, v, _depth + 1)
        elif isinstance(v, list):
            out[k] = [_deref(spec, i, _depth + 1) for i in v]
        else:
            out[k] = v
    return out


def _base_url(spec: dict[str, Any], server_override: str | None) -> str:
    if server_override:
        return server_override.rstrip("/")
    servers = spec.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], dict):
        return str(servers[0].get("url", "")).rstrip("/")
    return ""


def _tool_name(operation_id: str, method: str, path: str, taken: set[str]) -> str:
    """Derive a stable, unique tool name from operationId (or method_path)."""
    if operation_id:
        name = _SAFE_NAME.sub("_", operation_id).strip("_")
    else:
        slug = _SAFE_NAME.sub("_", path.replace("{", "").replace("}", "")).strip("_")
        name = f"{method.lower()}_{slug}".strip("_")
    name = name or "op"
    base, i = name, 2
    while name in taken:
        name = f"{base}_{i}"
        i += 1
    taken.add(name)
    return name


def _build_schema(
    params: list[dict[str, Any]], body_schema: dict[str, Any] | None
) -> tuple[dict[str, Any], dict[str, str]]:
    """Merge path/query params + request body into one JSON-schema object.

    Returns (schema, param_locations) mapping each property to path|query|body.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    locations: dict[str, str] = {}

    for p in params:
        loc = p.get("in")
        name = p.get("name")
        if loc not in ("path", "query") or not name:
            continue
        properties[name] = p.get("schema", {"type": "string"}) or {"type": "string"}
        if p.get("description"):
            properties[name] = {**properties[name], "description": p["description"]}
        locations[name] = loc
        if p.get("required") or loc == "path":  # path params are always required
            required.append(name)

    if body_schema and body_schema.get("type") == "object":
        for name, sub in (body_schema.get("properties") or {}).items():
            properties[name] = sub
            locations[name] = "body"
        for name in body_schema.get("required", []) or []:
            if name not in required:
                required.append(name)
    elif body_schema:
        properties["body"] = body_schema
        locations["body"] = "body"
        required.append("body")

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema, locations


def _request_body_schema(spec: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any] | None:
    rb = operation.get("requestBody")
    if not isinstance(rb, dict):
        return None
    content = rb.get("content", {})
    for media in ("application/json", "application/*+json"):
        if media in content:
            return _deref(spec, content[media].get("schema", {}))
    for mt in content.values():
        if isinstance(mt, dict) and "schema" in mt:
            return _deref(spec, mt["schema"])
    return None


def parse_spec(
    spec: str | dict[str, Any],
    *,
    server_url: str | None = None,
    default_timeout: float = 30.0,
    name_prefix: str = "",
) -> list[dict[str, Any]]:
    """Parse an OpenAPI 3.x spec into a list of tool-spec dicts.

    Each dict has: name, endpoint, method, description, timeout_seconds, schema,
    path_params, query_params, plus metadata (path, summary, operation_id,
    param_locations).

    Raises OpenAPIError if the spec is unparseable or has no operations.
    """
    doc = _load(spec)
    paths = doc.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise OpenAPIError("Spec has no 'paths'")

    base = _base_url(doc, server_url)
    taken: set[str] = set()
    tools: list[dict[str, Any]] = []

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        shared_params = [_deref(doc, p) for p in path_item.get("parameters", []) if isinstance(p, dict)]

        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue

            op_params = [_deref(doc, p) for p in operation.get("parameters", []) if isinstance(p, dict)]
            merged: dict[tuple[str, str], dict[str, Any]] = {}
            for p in [*shared_params, *op_params]:
                merged[(p.get("name", ""), p.get("in", ""))] = p
            all_params = list(merged.values())

            body_schema = _request_body_schema(doc, operation)
            schema, locations = _build_schema(all_params, body_schema)

            op_id = operation.get("operationId", "")
            raw_name = _tool_name(op_id, method, path, taken)
            name = f"{name_prefix}{raw_name}" if name_prefix else raw_name

            description = (operation.get("summary") or operation.get("description") or "").strip()

            tools.append({
                "name": name,
                "endpoint": f"{base}{path}",  # path template; {vars} filled at call time
                "method": method.upper(),
                "description": description,
                "timeout_seconds": default_timeout,
                "schema": schema,
                "path_params": [n for n, loc in locations.items() if loc == "path"],
                "query_params": [n for n, loc in locations.items() if loc == "query"],
                # metadata (callers may ignore)
                "path": path,
                "summary": description,
                "operation_id": op_id or raw_name,
                "param_locations": locations,
            })

    if not tools:
        raise OpenAPIError("Spec produced no callable operations")
    return tools


def fetch_spec_text(url: str, *, timeout: float = 15.0) -> str:
    """Fetch an OpenAPI spec from a URL (returns the raw text)."""
    import httpx

    resp = httpx.get(url.strip(), timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def is_url(source: str) -> bool:
    return bool(re.match(r"^https?://", (source or "").strip()))
