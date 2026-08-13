"""SSO/OIDC router for Ostiari Control Plane.

Provides endpoints for initiating SSO login, handling the IdP callback,
and reporting SSO configuration status to the frontend.
"""

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.models import User
from control_plane.auth.roles import require_valid_role
from control_plane.auth.service import create_access_token
from control_plane.auth.sso import (
    detect_provider,
    exchange_code,
    extract_roles_from_claims,
    get_authorization_url,
    get_oidc_config,
    get_userinfo,
    validate_id_token,
)
from control_plane.database import get_db
from control_plane.env import configured_org_id, is_production, tenant_is_allowed
from control_plane.models.database import Organization, SSOLoginState

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth/sso", tags=["auth-sso"])

# Dev-only compatibility mirror. Durable state is stored by digest in SQL.
_pending_states: dict[str, dict] = {}
_STATE_TTL_SECONDS = 600

# Default role for new SSO users (configurable via env var)
DEFAULT_ROLE = require_valid_role(
    os.environ.get("OIDC_DEFAULT_ROLE", "viewer"),
    source="OIDC_DEFAULT_ROLE",
)

def _frontend_url() -> str:
    """Browser-reachable dashboard origin used after the IdP callback."""
    return os.environ.get("OSTIARI_FRONTEND_URL", "http://localhost:9000").rstrip("/")


def _state_digest(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


async def _store_state(db: AsyncSession, state: str, nonce: str) -> None:
    now = datetime.now(timezone.utc)
    await db.execute(delete(SSOLoginState).where(SSOLoginState.expires_at <= now))
    db.add(
        SSOLoginState(
            state_digest=_state_digest(state),
            nonce=nonce,
            expires_at=now + timedelta(seconds=_STATE_TTL_SECONDS),
        )
    )
    await db.commit()
    if not is_production():
        _pending_states[state] = {"nonce": nonce}


async def _consume_state(db: AsyncSession, state: str) -> str | None:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        delete(SSOLoginState)
        .where(
            SSOLoginState.state_digest == _state_digest(state),
            SSOLoginState.expires_at > now,
        )
        .returning(SSOLoginState.nonce)
    )
    nonce = result.scalar_one_or_none()
    await db.commit()
    if nonce is not None:
        _pending_states.pop(state, None)
        return nonce
    if is_production():
        return None
    pending = _pending_states.pop(state, None)
    return pending.get("nonce") if pending else None


def _frontend_redirect(
    path: str,
    *,
    query: dict[str, str] | None = None,
    fragment: dict[str, str] | None = None,
) -> RedirectResponse:
    url = f"{_frontend_url()}/{path.lstrip('/')}"
    if query:
        url = f"{url}?{urlencode(query)}"
    if fragment:
        # Fragments are not sent to the frontend web server or in Referer
        # headers, keeping the local JWT out of access logs.
        url = f"{url}#{urlencode(fragment)}"
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


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
async def sso_login(db: AsyncSession = Depends(get_db)):
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
        return _frontend_redirect(
            "/login",
            query={"error": "provider_unavailable"},
        )

    await _store_state(db, state, nonce)

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
        return _frontend_redirect(
            "/login",
            query={
                "error": "sso_failed",
                "detail": error_description or error,
            },
        )

    if not code or not state:
        return _frontend_redirect(
            "/login",
            query={"error": "invalid_callback"},
        )

    # Validate state (CSRF protection)
    nonce = await _consume_state(db, state)
    if nonce is None:
        return _frontend_redirect(
            "/login",
            query={"error": "invalid_state"},
        )

    config = get_oidc_config()
    if not config:
        return _frontend_redirect(
            "/login",
            query={"error": "sso_unavailable"},
        )

    # Exchange authorization code for tokens
    try:
        token_response = await exchange_code(config, code)
    except Exception as e:
        logger.error(f"Token exchange failed: {e}")
        return _frontend_redirect(
            "/login",
            query={"error": "token_exchange_failed"},
        )

    access_token = token_response.get("access_token")
    id_token = token_response.get("id_token")

    if not id_token:
        logger.error("No id_token in token response")
        return _frontend_redirect(
            "/login",
            query={"error": "no_id_token"},
        )

    # Validate the ID token
    try:
        id_claims = await validate_id_token(config, id_token, nonce=nonce)
    except ValueError as e:
        logger.error(f"ID token validation failed: {e}")
        return _frontend_redirect(
            "/login",
            query={"error": "invalid_token"},
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
        return _frontend_redirect(
            "/login",
            query={"error": "no_email"},
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
        if not tenant_is_allowed(user.org_id):
            logger.warning("User from disallowed tenant attempted SSO login: %s", email)
            return _frontend_redirect(
                "/login",
                query={"error": "tenant_not_allowed"},
            )
        if not user.is_active:
            logger.warning("Disabled user attempted SSO login: %s", email)
            return _frontend_redirect(
                "/login",
                query={"error": "account_disabled"},
            )
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
        org_id = configured_org_id()
        if await db.get(Organization, org_id) is None:
            db.add(Organization(id=org_id, name=org_id))
            await db.flush()
        user = User(
            email=email,
            name=name,
            hashed_password="",  # SSO users don't have passwords
            role=role,
            is_active=True,
            org_id=org_id,
            sso_provider=provider,
            sso_subject_id=subject_id,
            last_login=datetime.now(timezone.utc),
        )
        db.add(user)
        await db.flush()

    # Issue Ostiari JWT
    token = create_access_token(
        user.id,
        user.email,
        user.role,
        org=user.org_id or configured_org_id(),
    )

    # The frontend consumes and removes the fragment before validating /auth/me.
    return _frontend_redirect(
        "/auth/sso-callback",
        fragment={"token": token},
    )
