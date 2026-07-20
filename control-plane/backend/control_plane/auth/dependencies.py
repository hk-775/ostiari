"""FastAPI dependencies for authentication and authorization."""

from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status
from jose import JWTError

from control_plane.auth import oidc
from control_plane.auth.schemas import AuthUser
from control_plane.auth.service import decode_token


async def get_current_user(request: Request) -> AuthUser:
    """Extract and validate a Bearer token, returning the AuthUser principal.

    When OSTIARI_AUTH_MODE=oidc, tokens are validated as external OIDC JWTs
    (Cognito / any OIDC IdP) via JWKS and mapped to an AuthUser. Otherwise
    (default) the control plane's own locally-issued token is decoded — the
    behavior the demo and seeded admin login rely on.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = auth_header.removeprefix("Bearer ")

    validator = oidc.get_validator()
    if validator is not None:
        try:
            claims = validator.validate(token)
            return oidc.principal_from_claims(claims)
        except oidc.OIDCError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from None

    try:
        payload = decode_token(token)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return AuthUser(
        id=int(payload["sub"]),
        email=payload["email"],
        role=payload["role"],
        subject=str(payload["sub"]),
        kind="user",
    )


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
