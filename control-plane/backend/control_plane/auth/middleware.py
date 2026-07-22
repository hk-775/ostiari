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


def _valid_token(token: str) -> bool:
    """Validate a Bearer token (OIDC JWT when configured, else local token)."""
    validator = oidc.get_validator()
    if validator is not None:
        try:
            validator.validate(token)
            return True
        except oidc.OIDCError:
            return False
    try:
        decode_token(token)
        return True
    except JWTError:
        return False


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

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not _valid_token(auth.removeprefix("Bearer ")):
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)
