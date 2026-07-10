"""Role-based access control definitions."""

ROLES: dict[str, list[str]] = {
    "admin": [
        "users:read", "users:write", "users:delete",
        "gateways:read", "gateways:write", "gateways:delete",
        "tools:read", "tools:write", "tools:delete",
        "policies:read", "policies:write", "policies:delete",
        "quotas:read", "quotas:write", "quotas:delete",
        "agents:read", "agents:write", "agents:delete",
        "mcp:read", "mcp:write", "mcp:delete",
        "audit:read", "costs:read", "experiments:read", "experiments:write",
        "models:read", "models:write",
    ],
    "operator": [
        "gateways:read", "gateways:write", "gateways:delete",
        "tools:read", "tools:write", "tools:delete",
        "policies:read", "policies:write", "policies:delete",
        "quotas:read", "quotas:write", "quotas:delete",
        "agents:read", "agents:write", "agents:delete",
        "mcp:read", "mcp:write", "mcp:delete",
        "audit:read", "costs:read", "experiments:read", "experiments:write",
        "models:read", "models:write",
    ],
    "viewer": [
        "gateways:read", "tools:read", "policies:read", "quotas:read",
        "agents:read", "mcp:read", "audit:read", "costs:read",
        "experiments:read", "models:read", "users:read",
    ],
}


def check_permission(role: str, action: str) -> bool:
    """Check if a role has permission for a given action."""
    permissions = ROLES.get(role, [])
    return action in permissions
