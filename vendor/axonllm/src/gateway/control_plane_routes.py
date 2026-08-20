"""Versioned ownership contract for control-plane HTTP routes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from starlette.routing import BaseRoute


ROUTE_CONTRACT_VERSION = 2


class RouteDisposition(StrEnum):
    """Target execution boundary for one control-plane route."""

    STATIC_ASSET = "static-asset"
    SYNCHRONOUS_API = "synchronous-api"
    ASYNC_EXPORT = "async-export"
    WORKER_TRIGGER = "worker-trigger"
    SPA_MIGRATION = "spa-migration"


@dataclass(frozen=True, order=True)
class ControlRoute:
    """One normalized route entry in the serverless migration inventory."""

    method: str
    path: str
    disposition: RouteDisposition


_STATIC_PATHS = frozenset(
    {
        "/",
        "/admin/dashboard",
        "/admin/static/{path:path}",
        "/{directory}/{path}",
        "/{path}",
    }
)
_SPA_MIGRATION_PATHS = frozenset(
    {
        "/admin/architecture",
        "/admin/catalog-drift",
        "/admin/pricing-drift",
        "/admin/production-checklist",
    }
)
_ASYNC_EXPORT_PATHS = frozenset(
    {
        "/admin/audit/export",
        "/admin/audit/exports/{job_id}",
        "/admin/audit/exports/{job_id}/download",
        "/admin/usage/export",
        "/admin/usage/exports/{job_id}",
        "/admin/usage/exports/{job_id}/download",
    }
)
_WORKER_TRIGGER_ROUTES = frozenset(
    {
        ("POST", "/admin/regions/failover"),
        ("POST", "/admin/regions/health/check"),
        ("POST", "/admin/webhooks/{name}/test"),
    }
)
_SYNCHRONOUS_PREFIXES = (
    "/admin/",
    "/auth/",
    "/saml/",
    "/scim/",
)
_SYNCHRONOUS_EXACT_PATHS = frozenset({"/health", "/ready"})


def classify_control_route(
    method: str,
    path: str,
) -> RouteDisposition:
    """Classify a control route or reject an unknown execution boundary."""

    normalized_method = method.upper()
    if path in _STATIC_PATHS:
        if normalized_method != "GET":
            raise ValueError(f"static route {path} must use GET, found {normalized_method}")
        return RouteDisposition.STATIC_ASSET
    if path in _SPA_MIGRATION_PATHS:
        if normalized_method != "GET":
            raise ValueError(f"SPA migration route {path} must use GET, found {normalized_method}")
        return RouteDisposition.SPA_MIGRATION
    if path in _ASYNC_EXPORT_PATHS:
        if normalized_method != "GET":
            raise ValueError(f"export route {path} must use GET, found {normalized_method}")
        return RouteDisposition.ASYNC_EXPORT
    if (normalized_method, path) in _WORKER_TRIGGER_ROUTES:
        return RouteDisposition.WORKER_TRIGGER
    if path in _SYNCHRONOUS_EXACT_PATHS or path.startswith(_SYNCHRONOUS_PREFIXES):
        return RouteDisposition.SYNCHRONOUS_API
    raise ValueError(
        f"control route {normalized_method} {path} is not classified by contract v{ROUTE_CONTRACT_VERSION}"
    )


def control_route_inventory(
    routes: Iterable[BaseRoute],
) -> tuple[ControlRoute, ...]:
    """Return a deterministic inventory and reject duplicate route methods."""

    inventory: list[ControlRoute] = []
    seen: set[tuple[str, str]] = set()
    for route in routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not isinstance(path, str) or not methods:
            raise ValueError("control-plane routes must be HTTP path routes")
        for method in sorted(methods):
            if method == "HEAD":
                continue
            key = (method, path)
            if key in seen:
                raise ValueError(f"duplicate control route {method} {path}")
            seen.add(key)
            inventory.append(
                ControlRoute(
                    method=method,
                    path=path,
                    disposition=classify_control_route(method, path),
                )
            )
    return tuple(sorted(inventory))


__all__ = [
    "ControlRoute",
    "ROUTE_CONTRACT_VERSION",
    "RouteDisposition",
    "classify_control_route",
    "control_route_inventory",
]
