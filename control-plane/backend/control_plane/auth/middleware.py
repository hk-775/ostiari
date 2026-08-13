"""Control-plane API authentication middleware.

The audit (assessment finding #1) found the control-plane API almost entirely
unauthenticated — 26 routers wired with no global auth dependency, so any
network caller could read all data and rewrite policies/quotas. This middleware
closes that by requiring a valid Bearer token on every ``/api/*`` route except a
small allowlist (login, health, SSO callback, trace ingest which has its own
shared secret).

Configurable, so the local demo still works:
  - OSTIARI_REQUIRE_AUTH unset/false (default): auth NOT enforced globally
    (demo/dev — preserves the seeded-admin, no-token flows).
  - OSTIARI_REQUIRE_AUTH=true: fail-closed — unauthenticated /api/* calls get
    401. This is the intended production posture.

Fine-grained role checks (admin vs operator vs viewer) still live on individual
routers via require_role; this middleware is the coarse "are you authenticated
at all" gate that was missing.
"""

from __future__ import annotations

import os
import re
import secrets

from jose import JWTError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from control_plane.auth import oidc
from control_plane.auth.roles import WRITE_ROLES, normalize_role
from control_plane.auth.schemas import AuthUser
from control_plane.auth.service import decode_token
from control_plane.env import tenant_is_allowed

# Paths that must remain reachable without a token. Prefix match on the URL path.
_PUBLIC_PREFIXES = (
    "/api/health",
    "/api/auth/login",
    "/api/auth/register",       # first-run bootstrap; tighten separately if desired
    "/api/auth/sso/",           # SSO login/callback/config (browser flow, pre-token)
    "/api/traces/ingest",       # machine ingest — guarded by its own X-Ingest-Key
    "/docs", "/openapi.json", "/redoc",
)


def _require_auth_enabled() -> bool:
    return os.environ.get("OSTIARI_REQUIRE_AUTH", "").lower() in ("1", "true", "yes", "on")


def _is_public(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in _PUBLIC_PREFIXES)


def _token_principal(token: str) -> AuthUser | None:
    """Validate a Bearer token and return its normalized principal."""
    validator = oidc.get_validator()
    if validator is not None:
        try:
            claims = validator.validate(token)
            principal = oidc.principal_from_claims(claims)
            role = normalize_role(principal.role)
            if role is None:
                return None
            principal.role = role
            return principal
        except oidc.OIDCError:
            return None
    try:
        payload = decode_token(token)
        role = normalize_role(payload.get("role"))
        if role is None:
            return None
        return AuthUser(
            id=int(payload["sub"]),
            email=payload["email"],
            role=role,
            subject=str(payload["sub"]),
            kind="user",
            tenant_id=payload.get("org", "default"),
        )
    except (JWTError, KeyError, TypeError, ValueError):
        return None


_MACHINE_ROUTES = (
    ("POST", re.compile(r"^/api/gateways/[^/]+/(register|heartbeat|spend)$")),
    ("GET", re.compile(r"^/api/gateways/[^/]+/spend$")),
    ("GET", re.compile(r"^/api/gateways/[^/]+/config-bundle$")),
    ("POST", re.compile(r"^/api/approvals$")),
    ("GET", re.compile(r"^/api/approvals/[^/]+$")),
    ("POST", re.compile(r"^/api/costs/record(?:/batch)?$")),
    ("POST", re.compile(r"^/api/payments/ingest$")),
    ("POST", re.compile(r"^/api/quotas/alerts$")),
)

_OPERATOR_VISIBLE_MACHINE_ROUTES = (
    ("GET", re.compile(r"^/api/gateways/[^/]+/(config-bundle|spend)$")),
    ("GET", re.compile(r"^/api/approvals/[^/]+$")),
)


def _is_machine_route(method: str, path: str) -> bool:
    return any(method == allowed and pattern.fullmatch(path) for allowed, pattern in _MACHINE_ROUTES)


def _is_operator_visible_machine_route(method: str, path: str) -> bool:
    return any(
        method == allowed and pattern.fullmatch(path)
        for allowed, pattern in _OPERATOR_VISIBLE_MACHINE_ROUTES
    )


def _valid_service_key(request: Request) -> bool:
    expected = os.environ.get("OSTIARI_SERVICE_TOKEN", "").strip()
    presented = request.headers.get("X-Ostiari-Service-Key", "")
    return bool(expected and presented and secrets.compare_digest(presented, expected))


class AuthMiddleware(BaseHTTPMiddleware):
    """Coarse authentication gate for the control-plane API.

    No-op unless OSTIARI_REQUIRE_AUTH is truthy. When enabled, every non-public
    ``/api/*`` request must carry a valid Bearer token or receives 401.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (
            path.startswith("/api/")
            and _is_machine_route(request.method, path)
            and _valid_service_key(request)
        ):
            request.state.machine_authenticated = True
            request.state.audit_actor = "gateway-service"
            return await call_next(request)

        if not _require_auth_enabled():
            return await call_next(request)

        # Only guard the API surface; static/non-/api paths pass through.
        if not path.startswith("/api/") or _is_public(path):
            return await call_next(request)

        if (
            _is_machine_route(request.method, path)
            and not _is_operator_visible_machine_route(request.method, path)
        ):
            return JSONResponse(
                status_code=401,
                content={"detail": "Gateway service authentication required"},
                headers={"WWW-Authenticate": "X-Ostiari-Service-Key"},
            )

        # Some lifecycle resources are also visible to signed-in operators.
        # A missing service key may therefore continue to normal Bearer auth;
        # an invalid request still fails below.

        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""
        principal = _token_principal(token) if token else None
        if principal is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not tenant_is_allowed(principal.tenant_id):
            return JSONResponse(
                status_code=403,
                content={"detail": "Token tenant is not permitted by this deployment"},
            )
        request.state.auth_principal = principal
        request.state.audit_actor = (
            principal.email or principal.subject or f"{principal.kind}:{principal.id}"
        )
        if (
            request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and principal.role not in WRITE_ROLES
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "This role is read-only"},
            )
        return await call_next(request)
