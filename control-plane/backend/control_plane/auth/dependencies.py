"""FastAPI dependencies for authentication and authorization."""

from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status
from jwt.exceptions import PyJWTError as JWTError

from control_plane.auth import oidc
from control_plane.auth.schemas import AuthUser
from control_plane.auth.service import decode_token
from control_plane.env import configured_org_id, tenant_is_allowed


def principal_from_token(token: str) -> AuthUser:
    """Validate a local or OIDC token and return its normalized principal."""
    validator = oidc.get_validator()
    if validator is not None:
        claims = validator.validate(token)
        return oidc.principal_from_claims(claims)

    payload = decode_token(token)
    return AuthUser(
        id=int(payload["sub"]),
        email=payload["email"],
        role=payload["role"],
        subject=str(payload["sub"]),
        kind="user",
        tenant_id=payload.get("org", "default"),
    )


async def get_current_user(request: Request) -> AuthUser:
    """Extract and validate a Bearer token, returning the AuthUser principal.

    When OSTIARI_AUTH_MODE=oidc, tokens are validated as external OIDC JWTs
    (Cognito / any OIDC IdP) via JWKS and mapped to an AuthUser. Otherwise
    (default) the control plane's own locally-issued token is decoded — the
    behavior the demo and seeded admin login rely on.
    """
    cached = getattr(request.state, "auth_principal", None)
    if isinstance(cached, AuthUser):
        if not tenant_is_allowed(cached.tenant_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token tenant is not permitted by this deployment",
            )
        return cached

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = auth_header.removeprefix("Bearer ")

    try:
        principal = principal_from_token(token)
    except (oidc.OIDCError, JWTError, KeyError, TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    if not tenant_is_allowed(principal.tenant_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token tenant is not permitted by this deployment",
        )
    return principal


async def get_current_org(request: Request) -> str:
    """Resolve the caller's org (tenant) for per-org data scoping.

    Falls back to "default" when there's no bearer token — preserving the
    single-org demo/dev experience where routes are unauthenticated. When
    OSTIARI_REQUIRE_AUTH is on, AuthMiddleware has already 401'd tokenless
    requests before they reach a route, so this fallback is demo-only.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return configured_org_id()
    try:
        user = await get_current_user(request)
    except HTTPException:
        return configured_org_id()
    return user.tenant_id or configured_org_id()


def require_role(*roles: str) -> Callable:
    """Return a dependency that enforces the user has one of the allowed roles."""

    async def _check(user: AuthUser = Depends(get_current_user)) -> AuthUser:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{user.role}' not permitted. Required: {', '.join(roles)}",
            )
        return user

    return _check
