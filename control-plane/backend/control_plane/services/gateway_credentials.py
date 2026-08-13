"""Credentials and safe headers for control-plane calls to gateways."""

from __future__ import annotations

import os
from collections.abc import Mapping

_FORWARDED_HEADERS = frozenset(
    {
        "accept",
        "content-type",
        "traceparent",
        "tracestate",
        "baggage",
        "x-session-id",
        "x-plan",
        "x-step",
    }
)


def _bearer(value: str) -> str:
    token = value.removeprefix("Bearer ").strip()
    return f"Bearer {token}" if token else ""


def gateway_agent_credential(
    *,
    token_env: str | None = None,
    agent_env: str | None = None,
    default_agent_id: str = "control-plane-proxy",
) -> tuple[str, str | None]:
    """Return a dedicated gateway identity, with a generic fleet fallback."""
    token = ""
    if token_env:
        token = os.environ.get(token_env, "").strip()
    token = token or os.environ.get("OSTIARI_GATEWAY_AGENT_TOKEN", "").strip()

    agent_id = ""
    if agent_env:
        agent_id = os.environ.get(agent_env, "").strip()
    agent_id = (
        agent_id
        or os.environ.get("OSTIARI_GATEWAY_AGENT_ID", "").strip()
        or default_agent_id
    )
    authorization = _bearer(token)
    return agent_id, authorization or None


def proxy_headers(path: str, incoming: Mapping[str, str]) -> dict[str, str]:
    """Build downstream headers without forwarding browser credentials."""
    headers = {
        name: value
        for name, value in incoming.items()
        if name.lower() in _FORWARDED_HEADERS
    }
    normalized_path = path.lstrip("/")
    if normalized_path == "config" or normalized_path.startswith("config/"):
        config_key = os.environ.get("OSTIARI_CONFIG_ADMIN_KEY", "").strip()
        if config_key:
            headers["X-Config-Admin-Key"] = config_key
        return headers

    agent_id, authorization = gateway_agent_credential()
    headers["X-Agent-Id"] = agent_id
    headers["X-Framework"] = "control-plane"
    if authorization:
        headers["Authorization"] = authorization
    return headers
