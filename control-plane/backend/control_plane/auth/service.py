"""Authentication service — JWT and password utilities."""

import os
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from jwt.exceptions import PyJWTError as JWTError

from control_plane.auth.roles import require_valid_role
from control_plane.env import DEFAULT_DEV_JWT_SECRET, is_production


def _resolve_jwt_secret() -> str:
    """Return the JWT signing secret, refusing insecure config in production.

    In production (OSTIARI_ENV=production) a strong OSTIARI_JWT_SECRET is
    mandatory — starting with the well-known dev default would let anyone forge
    admin tokens. In dev/demo the default is allowed for convenience.
    """
    secret = os.environ.get("OSTIARI_JWT_SECRET", "").strip()
    if is_production():
        if not secret or secret == DEFAULT_DEV_JWT_SECRET:
            raise RuntimeError(
                "OSTIARI_JWT_SECRET must be set to a strong secret in production "
                "(OSTIARI_ENV=production) — the dev default is refused."
            )
        if len(secret) < 32:
            raise RuntimeError("OSTIARI_JWT_SECRET too short (need >= 32 chars) in production.")
        return secret
    return secret or DEFAULT_DEV_JWT_SECRET


JWT_SECRET = _resolve_jwt_secret()
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24
MIN_PRODUCTION_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    """Verify a password against its bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        # bcrypt rejects inputs longer than 72 bytes (and malformed hashes).
        # Invalid credentials should remain a normal authentication failure,
        # not escape as a 500 response.
        return False


def validate_local_password(password: str) -> None:
    """Reject weak local credentials in production."""
    if is_production() and len(password) < MIN_PRODUCTION_PASSWORD_LENGTH:
        raise ValueError(
            "Password must be at least "
            f"{MIN_PRODUCTION_PASSWORD_LENGTH} characters in production"
        )


def create_access_token(user_id: int, email: str, role: str, org: str = "default") -> str:
    """Create a JWT access token with 24h expiry.

    `org` is the tenant the principal belongs to; it's read back by
    get_current_user and drives per-org data scoping.
    """
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": require_valid_role(role, source="token role"),
        "org": org,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT token. Raises JWTError on failure."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError:
        raise
