"""Provider-agnostic OIDC JWT validation (JWKS-based).

Validates RS256 JWTs issued by any OIDC provider — AWS Cognito, Okta, Auth0,
Keycloak, or a self-hosted issuer. The only inputs are the issuer URL and the
JWKS URL; nothing here is AWS-specific, so Ostiari stays portable off-AWS.

How it works (see docs/control-plane-guide.md and auth/README.md):
  - public signing keys are fetched from the JWKS URL ONCE and cached in-process
    (refetched only when a token references an unknown key id),
  - every token is then verified LOCALLY — signature (RS256), issuer, audience,
    expiry — with no per-request call to the identity provider.

Enable via env (all optional; unset = OIDC disabled, caller falls back):
  OSTIARI_OIDC_ISSUER      e.g. https://cognito-idp.us-east-1.amazonaws.com/<pool-id>
  OSTIARI_OIDC_JWKS_URL    defaults to <issuer>/.well-known/jwks.json
  OSTIARI_OIDC_AUDIENCE    optional; if set, the token's aud/client_id must match
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
from jose import jwt
from jose.exceptions import JWTError

from control_plane.auth.roles import VALID_ROLES
from control_plane.env import configured_org_id, tenancy_mode


class OIDCError(Exception):
    """Raised when a token fails OIDC validation."""


class OIDCValidator:
    """Fetches/caches JWKS and validates RS256 tokens against it."""

    def __init__(
        self,
        issuer: str,
        jwks_url: str | None = None,
        audience: str | None = None,
        *,
        cache_ttl_seconds: int = 3600,
        http_get=None,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.jwks_url = jwks_url or f"{self.issuer}/.well-known/jwks.json"
        self.audience = audience
        self._cache_ttl = cache_ttl_seconds
        self._keys: dict[str, dict] = {}       # kid -> JWK
        self._fetched_at: float = 0.0
        # Injectable fetcher for tests (default: real HTTP GET returning JSON).
        self._http_get = http_get or self._default_get

    @staticmethod
    def _default_get(url: str) -> dict:
        resp = httpx.get(url, timeout=5.0)
        resp.raise_for_status()
        return resp.json()

    # ─── JWKS cache ──────────────────────────────────────────────────────────

    def _refresh_keys(self) -> None:
        data = self._http_get(self.jwks_url)
        keys = {k["kid"]: k for k in data.get("keys", []) if "kid" in k}
        if keys:
            self._keys = keys
            self._fetched_at = _now()

    def _key_for(self, kid: str) -> dict | None:
        # Refresh if we've never fetched, the kid is unknown, or the cache is stale.
        if kid not in self._keys or (_now() - self._fetched_at) > self._cache_ttl:
            self._refresh_keys()
        return self._keys.get(kid)

    # ─── Validation ──────────────────────────────────────────────────────────

    def validate(self, token: str) -> dict[str, Any]:
        """Verify a JWT and return its claims. Raises OIDCError on any failure."""
        try:
            header = jwt.get_unverified_header(token)
        except JWTError as exc:
            raise OIDCError(f"malformed token header: {exc}") from None

        kid = header.get("kid")
        if not kid:
            raise OIDCError("token has no 'kid' header")

        key = self._key_for(kid)
        if key is None:
            raise OIDCError(f"no signing key for kid '{kid}'")

        # Cognito access tokens don't carry an 'aud' claim (they use client_id),
        # so only enforce audience when configured, and check client_id as a
        # fallback below.
        options = {"verify_aud": bool(self.audience)}
        try:
            claims = jwt.decode(
                token, key, algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience if self.audience else None,
                options=options,
            )
        except JWTError as exc:
            raise OIDCError(f"token validation failed: {exc}") from None

        if self.audience:
            aud = claims.get("aud") or claims.get("client_id")
            if aud != self.audience and self.audience not in (claims.get("aud") or []):
                raise OIDCError("audience mismatch")

        return claims


def _now() -> float:
    return time.time()


# ─── Module-level singletons wired from env (one validator per issuer) ───────

_validators: dict[str, OIDCValidator] = {}


def is_oidc_enabled() -> bool:
    return os.environ.get("OSTIARI_AUTH_MODE", "local").lower() == "oidc"


def resolve_issuer(tenant_id: str = "default") -> str:
    """Resolve the trusted issuer for a tenant.

    Single-tenant seam: today this returns the one configured issuer regardless
    of tenant. For multi-tenant SaaS later (pool-per-tenant), this becomes a
    lookup of tenant_id → that tenant's pool issuer — a one-function change, no
    call-site refactor. See docs/internal/security-faq-jwt-vs-ostiari.md.
    """
    return os.environ.get("OSTIARI_OIDC_ISSUER", "")


def get_validator(tenant_id: str = "default") -> OIDCValidator | None:
    """Return the configured validator for a tenant, or None if OIDC is off.

    Validators are cached per resolved issuer, so a future multi-issuer setup
    reuses one validator (and its JWKS cache) per pool.
    """
    if not is_oidc_enabled():
        return None
    issuer = resolve_issuer(tenant_id)
    if not issuer:
        return None
    if issuer not in _validators:
        _validators[issuer] = OIDCValidator(
            issuer=issuer,
            jwks_url=os.environ.get("OSTIARI_OIDC_JWKS_URL") or None,
            audience=os.environ.get("OSTIARI_OIDC_AUDIENCE") or None,
        )
    return _validators[issuer]


def reset_validator() -> None:
    """Clear cached validators (tests / config reload)."""
    _validators.clear()


# ─── Claims → Ostiari identity/role mapping ──────────────────────────────────

_VALID_ROLES = ("admin", "operator", "viewer")
_GROUP_ROLE_ALIASES = {
    "admin": frozenset({"admin", "admins", "administrator", "admin_group", "ostiari-admin"}),
    "operator": frozenset(
        {"operator", "operators", "operator_group", "editor", "ostiari-operator"}
    ),
    "viewer": frozenset(
        {"viewer", "viewers", "viewer_group", "reader", "readonly", "ostiari-viewer"}
    ),
}


def _role_from_claims(claims: dict[str, Any]) -> str:
    """Map IdP claims to an Ostiari role (admin | operator | viewer).

    Precedence: explicit custom 'role'/'ostiari_role' claim → Cognito groups →
    OAuth scopes → default 'viewer' (least privilege). Group and scope values
    are matched as complete, case-insensitive tokens; substrings never grant a
    role.
    """
    # 1. explicit role claim
    for key in ("ostiari_role", "custom:role", "role"):
        val = claims.get(key)
        if isinstance(val, str) and val.strip().lower() in VALID_ROLES:
            return val.strip().lower()

    # 2. groups (Cognito 'cognito:groups' or generic 'groups')
    groups = claims.get("cognito:groups") or claims.get("groups") or []
    if isinstance(groups, str):
        groups = [groups]
    lowered = {str(g).strip().lower() for g in groups}
    for role in _VALID_ROLES:  # admin wins over operator wins over viewer
        if lowered.intersection(_GROUP_ROLE_ALIASES[role]):
            return role

    # 3. scopes (space-delimited string or list)
    scope = claims.get("scope", "")
    scopes = scope.split() if isinstance(scope, str) else list(scope)
    normalized_scopes = {str(value).strip().lower() for value in scopes}
    if normalized_scopes.intersection({"admin", "ostiari.admin", "ostiari/admin"}):
        return "admin"
    if normalized_scopes.intersection(
        {
            "write",
            "operator",
            "ostiari.write",
            "ostiari.operator",
            "ostiari/operator",
            "write:tools",
        }
    ):
        return "operator"

    return "viewer"


def principal_from_claims(claims: dict[str, Any]):
    """Build an AuthUser from validated OIDC claims (user OR machine principal)."""
    from control_plane.auth.schemas import AuthUser

    subject = str(claims.get("sub", ""))
    # A machine (client-credentials) token has token_use=access with no user
    # identity — Cognito sets 'token_use' and often 'client_id' but no 'email'.
    is_machine = claims.get("token_use") == "access" and not claims.get("email")
    kind = "service" if is_machine else "user"
    email = claims.get("email") or claims.get("username") or claims.get("client_id") or subject

    return AuthUser(
        id=0,                       # external principals aren't local DB rows
        email=email,
        role=_role_from_claims(claims),
        subject=subject or str(claims.get("client_id", "")),
        kind=kind,
        tenant_id=tenant_from_claims(claims),
    )


def tenant_from_claims(claims: dict[str, Any]) -> str:
    """Extract the tenant/org id from a token, defaulting to 'default'.

    Single-tenant today (every token maps to 'default'); multi-tenant-ready
    because the seam already reads the claim. See
    docs/internal/security-faq-jwt-vs-ostiari.md.
    """
    for key in ("tenant_id", "custom:tenant_id", "org_id", "custom:org"):
        val = claims.get(key)
        if isinstance(val, str) and val:
            return val
    return configured_org_id() if tenancy_mode() == "single" else "default"
