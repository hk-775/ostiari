"""SSO/OIDC router for Ostiari Control Plane.

Provides endpoints for initiating SSO login, handling the IdP callback,
and reporting SSO configuration status to the frontend.
"""

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.models import User
from control_plane.auth.service import create_access_token
from control_plane.auth.sso import (
    OIDCConfig,
    detect_provider,
    exchange_code,
    extract_roles_from_claims,
    get_authorization_url,
    get_oidc_config,
    get_userinfo,
    validate_id_token,
)
from control_plane.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth/sso", tags=["auth-sso"])

# In-memory state store for CSRF protection.
# In production with multiple instances, use Redis or a shared store.
_pending_states: dict[str, dict] = {}

# Default role for new SSO users (configurable via env var)
DEFAULT_ROLE = os.environ.get("OIDC_DEFAULT_ROLE", "viewer")

# Frontend URL to redirect to after successful SSO login
FRONTEND_URL = os.environ.get("OSTIARI_FRONTEND_URL", "http://localhost:9500")


class SSOConfigResponse(BaseModel):
    """SSO configuration status for the frontend."""

    enabled: bool
    provider: str | None = None
    login_url: str | None = None


@router.get("/config", response_model=SSOConfigResponse)
async def sso_config():
    """Return SSO configuration status.

    The frontend uses this to determine whether to show the SSO login button.
    """
    config = get_oidc_config()
    if not config:
        return SSOConfigResponse(enabled=False)

    provider = detect_provider(config.issuer)
    return SSOConfigResponse(
        enabled=True,
        provider=provider,
        login_url="/api/auth/sso/login",
    )


@router.get("/login")
async def sso_login():
    """Initiate SSO login by redirecting to the IdP authorization endpoint.

    The browser is redirected to the IdP's login page. After authentication,
    the IdP redirects back to /api/auth/sso/callback with an authorization code.
    """
    config = get_oidc_config()
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SSO is not configured",
        )

    try:
        authorization_url, state, nonce = await get_authorization_url(config)
    except Exception as e:
        logger.error(f"Failed to build authorization URL: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to contact identity provider",
        )

    # Store state and nonce for validation in callback
    _pending_states[state] = {"nonce": nonce}

    return RedirectResponse(url=authorization_url, status_code=status.HTTP_302_FOUND)


@router.get("/callback")
async def sso_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Handle the IdP callback after user authentication.

    Receives the authorization code, exchanges it for tokens, validates the
    ID token, creates or matches the local user, and issues an Ostiari JWT.

    The user is then redirected to the frontend with the token as a query parameter.
    """
    # Handle IdP errors
    if error:
        logger.warning(f"SSO callback error: {error} - {error_description}")
        return RedirectResponse(
            url=f"{FRONTEND_URL}/login?error=sso_failed&detail={error_description or error}",
            status_code=status.HTTP_302_FOUND,
        )

    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing code or state parameter",
        )

    # Validate state (CSRF protection)
    pending = _pending_states.pop(state, None)
    if not pending:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired state parameter",
        )

    nonce = pending["nonce"]

    config = get_oidc_config()
    if not config:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SSO configuration lost",
        )

    # Exchange authorization code for tokens
    try:
        token_response = await exchange_code(config, code)
    except Exception as e:
        logger.error(f"Token exchange failed: {e}")
        return RedirectResponse(
            url=f"{FRONTEND_URL}/login?error=token_exchange_failed",
            status_code=status.HTTP_302_FOUND,
        )

    access_token = token_response.get("access_token")
    id_token = token_response.get("id_token")

    if not id_token:
        logger.error("No id_token in token response")
        return RedirectResponse(
            url=f"{FRONTEND_URL}/login?error=no_id_token",
            status_code=status.HTTP_302_FOUND,
        )

    # Validate the ID token
    try:
        id_claims = await validate_id_token(config, id_token, nonce=nonce)
    except ValueError as e:
        logger.error(f"ID token validation failed: {e}")
        return RedirectResponse(
            url=f"{FRONTEND_URL}/login?error=invalid_token",
            status_code=status.HTTP_302_FOUND,
        )

    # Get user info (supplement ID token claims)
    userinfo = {}
    if access_token:
        try:
            userinfo = await get_userinfo(config, access_token)
        except Exception as e:
            logger.warning(f"Failed to fetch userinfo: {e}")

    # Merge claims — userinfo takes precedence for profile data
    claims = {**id_claims, **userinfo}

    # Extract user attributes
    email = claims.get("email")
    if not email:
        logger.error("No email claim in SSO response")
        return RedirectResponse(
            url=f"{FRONTEND_URL}/login?error=no_email",
            status_code=status.HTTP_302_FOUND,
        )

    name = claims.get("name") or claims.get("preferred_username") or email.split("@")[0]
    subject_id = claims.get("sub", "")
    provider = detect_provider(config.issuer)

    # Try to extract role from IdP claims
    idp_role = extract_roles_from_claims(claims, provider)

    # Find or create local user
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user:
        # Existing user — update SSO fields
        user.name = name
        user.sso_provider = provider
        user.sso_subject_id = subject_id
        user.last_login = datetime.now(timezone.utc)
        # Update role from IdP if available and user was SSO-provisioned
        if idp_role and user.sso_provider:
            user.role = idp_role
    else:
        # New user — create with default or IdP-assigned role
        role = idp_role or DEFAULT_ROLE
        user = User(
            email=email,
            name=name,
            hashed_password="",  # SSO users don't have passwords
            role=role,
            is_active=True,
            sso_provider=provider,
            sso_subject_id=subject_id,
            last_login=datetime.now(timezone.utc),
        )
        db.add(user)
        await db.flush()

    # Issue Ostiari JWT
    token = create_access_token(user.id, user.email, user.role)

    # Redirect to frontend with token
    return RedirectResponse(
        url=f"{FRONTEND_URL}/auth/sso-callback?token={token}",
        status_code=status.HTTP_302_FOUND,
    )
