"""SSO/OIDC implementation for Ostiari Control Plane.

Supports any OIDC-compliant Identity Provider including:
- Okta: issuer = https://dev-XXXXX.okta.com
- AWS Cognito: issuer = https://cognito-idp.{region}.amazonaws.com/{user_pool_id}
- Azure AD: issuer = https://login.microsoftonline.com/{tenant_id}/v2.0
- Generic OIDC: any provider implementing OpenID Connect Discovery
"""

import hashlib
import os
import secrets
from dataclasses import dataclass, field
from typing import Any

import httpx
from jose import jwt
from jose.exceptions import JWTError


@dataclass
class OIDCConfig:
    """OIDC provider configuration loaded from environment variables."""

    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: list[str] = field(default_factory=lambda: ["openid", "email", "profile"])

    @property
    def discovery_url(self) -> str:
        """OpenID Connect Discovery endpoint."""
        issuer = self.issuer.rstrip("/")
        return f"{issuer}/.well-known/openid-configuration"


# Cache for OIDC discovery metadata and JWKS
_discovery_cache: dict[str, Any] = {}
_jwks_cache: dict[str, Any] = {}


def get_oidc_config() -> OIDCConfig | None:
    """Read OIDC configuration from environment variables.

    Returns None if SSO is not configured (OIDC_ISSUER not set).
    """
    issuer = os.environ.get("OIDC_ISSUER")
    if not issuer:
        return None

    client_id = os.environ.get("OIDC_CLIENT_ID", "")
    client_secret = os.environ.get("OIDC_CLIENT_SECRET", "")
    redirect_uri = os.environ.get(
        "OIDC_REDIRECT_URI", "http://localhost:9500/api/auth/sso/callback"
    )
    scopes_str = os.environ.get("OIDC_SCOPES", "openid email profile")
    scopes = scopes_str.split()

    if not client_id or not client_secret:
        return None

    return OIDCConfig(
        issuer=issuer,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scopes=scopes,
    )


async def _fetch_discovery(config: OIDCConfig) -> dict[str, Any]:
    """Fetch and cache the OIDC discovery document."""
    if config.issuer in _discovery_cache:
        return _discovery_cache[config.issuer]

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(config.discovery_url)
        resp.raise_for_status()
        metadata = resp.json()

    _discovery_cache[config.issuer] = metadata
    return metadata


async def _fetch_jwks(jwks_uri: str) -> dict[str, Any]:
    """Fetch and cache the JWKS from the IdP."""
    if jwks_uri in _jwks_cache:
        return _jwks_cache[jwks_uri]

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(jwks_uri)
        resp.raise_for_status()
        jwks = resp.json()

    _jwks_cache[jwks_uri] = jwks
    return jwks


def generate_state() -> str:
    """Generate a cryptographically secure state parameter."""
    return secrets.token_urlsafe(32)


def generate_nonce() -> str:
    """Generate a cryptographically secure nonce for ID token validation."""
    return secrets.token_urlsafe(32)


async def get_authorization_url(config: OIDCConfig) -> tuple[str, str, str]:
    """Build the IdP authorization URL.

    Returns:
        Tuple of (authorization_url, state, nonce)
    """
    metadata = await _fetch_discovery(config)
    authorization_endpoint = metadata["authorization_endpoint"]

    state = generate_state()
    nonce = generate_nonce()

    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "scope": " ".join(config.scopes),
        "state": state,
        "nonce": nonce,
    }

    query_string = "&".join(f"{k}={httpx.QueryParams({k: v})}" for k, v in params.items())
    # Use httpx URL building for proper encoding
    url = httpx.URL(authorization_endpoint, params=params)

    return str(url), state, nonce


async def exchange_code(config: OIDCConfig, code: str) -> dict[str, Any]:
    """Exchange an authorization code for tokens at the IdP's token endpoint.

    Args:
        config: OIDC configuration
        code: Authorization code received from IdP callback

    Returns:
        Token response containing access_token, id_token, etc.

    Raises:
        httpx.HTTPStatusError: If the token exchange fails
    """
    metadata = await _fetch_discovery(config)
    token_endpoint = metadata["token_endpoint"]

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.redirect_uri,
        "client_id": config.client_id,
        "client_secret": config.client_secret,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            token_endpoint,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return resp.json()


async def get_userinfo(config: OIDCConfig, access_token: str) -> dict[str, Any]:
    """Fetch user info from the IdP's userinfo endpoint.

    Args:
        config: OIDC configuration
        access_token: OAuth2 access token from IdP

    Returns:
        User info claims (sub, email, name, etc.)

    Note:
        - Okta: Returns standard OIDC claims + custom attributes
        - Cognito: Returns claims configured in the user pool
        - Azure AD: Returns claims based on token configuration
    """
    metadata = await _fetch_discovery(config)
    userinfo_endpoint = metadata.get("userinfo_endpoint")

    if not userinfo_endpoint:
        # Some providers (rare) don't expose userinfo; fall back to ID token claims
        return {}

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            userinfo_endpoint,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


async def validate_id_token(
    config: OIDCConfig, id_token: str, nonce: str | None = None
) -> dict[str, Any]:
    """Validate an ID token JWT from the IdP.

    Verifies the signature using the IdP's JWKS, validates standard claims
    (issuer, audience, expiration), and optionally checks the nonce.

    Args:
        config: OIDC configuration
        id_token: The raw JWT ID token string
        nonce: Expected nonce value (if one was sent in the auth request)

    Returns:
        Decoded and validated ID token claims

    Raises:
        ValueError: If token validation fails

    Provider notes:
        - Okta: Uses RS256 signing, standard JWKS endpoint
        - Cognito: Uses RS256, kid header to select key from JWKS
        - Azure AD: Uses RS256, may include v1 or v2 issuer depending on config
    """
    metadata = await _fetch_discovery(config)
    jwks_uri = metadata["jwks_uri"]
    jwks = await _fetch_jwks(jwks_uri)

    # Extract the key ID from the token header to find the right key
    try:
        unverified_header = jwt.get_unverified_header(id_token)
    except JWTError as e:
        raise ValueError(f"Invalid ID token header: {e}")

    kid = unverified_header.get("kid")
    if not kid:
        raise ValueError("ID token missing 'kid' header")

    # Find the matching key in JWKS
    rsa_key = None
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            rsa_key = key
            break

    if not rsa_key:
        # Key not found — clear cache and retry once (key rotation)
        _jwks_cache.pop(jwks_uri, None)
        jwks = await _fetch_jwks(jwks_uri)
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                rsa_key = key
                break

    if not rsa_key:
        raise ValueError(f"Unable to find matching key for kid: {kid}")

    # Validate the token
    issuer = config.issuer.rstrip("/")
    try:
        claims = jwt.decode(
            id_token,
            rsa_key,
            algorithms=["RS256"],
            audience=config.client_id,
            issuer=issuer,
            options={"verify_at_hash": False},
        )
    except JWTError as e:
        raise ValueError(f"ID token validation failed: {e}")

    # Verify nonce if provided
    if nonce and claims.get("nonce") != nonce:
        raise ValueError("ID token nonce mismatch")

    return claims


def detect_provider(issuer: str) -> str:
    """Detect the SSO provider type from the issuer URL.

    Returns one of: 'okta', 'cognito', 'azure_ad', 'oidc'
    """
    issuer_lower = issuer.lower()
    if "okta.com" in issuer_lower:
        return "okta"
    elif "cognito-idp" in issuer_lower and "amazonaws.com" in issuer_lower:
        return "cognito"
    elif "login.microsoftonline.com" in issuer_lower:
        return "azure_ad"
    return "oidc"


def extract_roles_from_claims(claims: dict[str, Any], provider: str) -> str | None:
    """Attempt to extract a role from IdP claims.

    Different providers send group/role information in different claims:
    - Okta: 'groups' claim (array) or custom 'role' claim
    - Cognito: 'cognito:groups' claim (array)
    - Azure AD: 'roles' claim (array) or 'groups' (array of GUIDs)
    - Generic OIDC: 'roles' or 'groups' claim

    Returns the first matching Ostiari role (admin, operator, viewer) or None.
    """
    valid_roles = {"admin", "operator", "viewer"}

    # Check provider-specific claims
    role_claims: list[str] = []

    if provider == "okta":
        role_claims = claims.get("groups", []) + [claims.get("role", "")]
    elif provider == "cognito":
        role_claims = claims.get("cognito:groups", [])
    elif provider == "azure_ad":
        role_claims = claims.get("roles", []) + claims.get("groups", [])
    else:
        role_claims = claims.get("roles", []) + claims.get("groups", [])

    # Also check a generic 'role' claim
    if claims.get("role"):
        role_claims.append(claims["role"])

    # Map to Ostiari roles (case-insensitive)
    for claim_value in role_claims:
        if isinstance(claim_value, str):
            normalized = claim_value.lower().strip()
            if normalized in valid_roles:
                return normalized
            # Common mappings
            if normalized in ("administrator", "admins", "admin_group"):
                return "admin"
            if normalized in ("operators", "operator_group", "editor"):
                return "operator"
            if normalized in ("viewers", "viewer_group", "reader", "readonly"):
                return "viewer"

    return None


def clear_caches() -> None:
    """Clear discovery and JWKS caches (useful for testing or key rotation)."""
    _discovery_cache.clear()
    _jwks_cache.clear()
