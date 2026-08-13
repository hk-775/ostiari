"""Canonical control-plane roles and validation helpers."""

from __future__ import annotations

from typing import Literal, TypeAlias, cast

Role: TypeAlias = Literal["admin", "operator", "viewer"]

VALID_ROLES: frozenset[str] = frozenset({"admin", "operator", "viewer"})
WRITE_ROLES: frozenset[str] = frozenset({"admin", "operator"})


def normalize_role(value: object) -> Role | None:
    """Return a canonical role, or ``None`` for unknown/untrusted values."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized not in VALID_ROLES:
        return None
    return cast("Role", normalized)


def require_valid_role(value: object, *, source: str = "role") -> Role:
    """Return a canonical role or fail rather than granting accidental access."""
    normalized = normalize_role(value)
    if normalized is None:
        allowed = ", ".join(sorted(VALID_ROLES))
        raise ValueError(f"Invalid {source} {value!r}; expected one of: {allowed}")
    return normalized
