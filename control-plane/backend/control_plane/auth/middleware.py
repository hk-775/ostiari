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
from control_plane.auth.service import decode_token

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


def _token_role(token: str) -> str | None:
    """Validate a Bearer token and return its effective role."""
    validator = oidc.get_validator()
    if validator is not None:
        try:
            claims = validator.validate(token)
            return oidc.principal_from_claims(claims).role
        except oidc.OIDCError:
            return None
    try:
        return decode_token(token).get("role")
    except JWTError:
        return None


_MACHINE_ROUTES = (
    ("POST", re.compile(r"^/api/gateways/[^/]+/(register|heartbeat|spend)$")),
    ("GET", re.compile(r"^/api/gateways/[^/]+/spend$")),
    ("GET", re.compile(r"^/api/gateways/[^/]+/config-bundle$")),
    ("POST", re.compile(r"^/api/approvals$")),
    ("GET", re.compile(r"^/api/approvals/[^/]+$")),
    ("POST", re.compile(r"^/api/payments/ingest$")),
    ("POST", re.compile(r"^/api/quotas/alerts$")),
)


def _is_machine_route(method: str, path: str) -> bool:
    return any(method == allowed and pattern.fullmatch(path) for allowed, pattern in _MACHINE_ROUTES)


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
        if not _require_auth_enabled():
            return await call_next(request)

        path = request.url.path
        # Only guard the API surface; static/non-/api paths pass through.
        if not path.startswith("/api/") or _is_public(path):
            return await call_next(request)

        if _is_machine_route(request.method, path):
            if _valid_service_key(request):
                request.state.machine_authenticated = True
                return await call_next(request)
            # Some lifecycle resources are also visible to signed-in operators.
            # A missing service key may therefore continue to normal Bearer auth;
            # an invalid request still fails below.

        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""
        role = _token_role(token) if token else None
        if role is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if role == "viewer":
                return JSONResponse(status_code=403, content={"detail": "Viewer role is read-only"})
        return await call_next(request)
