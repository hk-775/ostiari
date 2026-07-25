"""Provider-agnostic OIDC JWT validation for the gateway (JWKS-based).

Mirror of the control-plane validator, kept self-contained here because the
gateway is an independent service (it doesn't import the control-plane package).
Validates RS256 tokens from any OIDC issuer; nothing AWS-specific.

Enable via env (unset = gateway auth off, current header behavior preserved):
  OSTIARI_GATEWAY_AUTH=required     turn on token enforcement for tool calls
  OSTIARI_OIDC_ISSUER=<issuer url>
  OSTIARI_OIDC_JWKS_URL=<url>       defaults to <issuer>/.well-known/jwks.json
  OSTIARI_OIDC_AUDIENCE=<client id> optional aud/client_id check
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx
from jose import jwt
from jose.exceptions import JWTError


class OIDCError(Exception):
    """Raised when a token fails OIDC validation."""


class OIDCValidator:
    def __init__(self, issuer, jwks_url=None, audience=None, *, cache_ttl_seconds=3600, http_get=None):
        self.issuer = issuer.rstrip("/")
        self.jwks_url = jwks_url or f"{self.issuer}/.well-known/jwks.json"
        self.audience = audience
        self._cache_ttl = cache_ttl_seconds
        self._keys: dict[str, dict] = {}
        self._fetched_at = 0.0
        self._http_get = http_get or self._default_get

    @staticmethod
    def _default_get(url: str) -> dict:
        resp = httpx.get(url, timeout=5.0)
        resp.raise_for_status()
        return resp.json()

    def _refresh_keys(self) -> None:
        data = self._http_get(self.jwks_url)
        keys = {k["kid"]: k for k in data.get("keys", []) if "kid" in k}
        if keys:
            self._keys = keys
            self._fetched_at = time.time()

    def _key_for(self, kid: str) -> dict | None:
        if kid not in self._keys or (time.time() - self._fetched_at) > self._cache_ttl:
            self._refresh_keys()
        return self._keys.get(kid)

    def validate(self, token: str) -> dict[str, Any]:
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
        try:
            claims = jwt.decode(
                token, key, algorithms=["RS256"], issuer=self.issuer,
                audience=self.audience if self.audience else None,
                options={"verify_aud": bool(self.audience)},
            )
        except JWTError as exc:
            raise OIDCError(f"token validation failed: {exc}") from None
        return claims


def agent_id_from_claims(claims: dict[str, Any]) -> str:
    """The identity a service/agent token asserts — the value X-Agent-Id must match."""
    return str(
        claims.get("agent_id")
        or claims.get("custom:agent_id")
        or claims.get("client_id")
        or claims.get("sub")
        or ""
    )


def tenant_from_claims(claims: dict[str, Any]) -> str:
    """Tenant/org id from the token, defaulting to 'default' (multi-tenant seam)."""
    for key in ("tenant_id", "custom:tenant_id", "org_id", "custom:org"):
        val = claims.get(key)
        if isinstance(val, str) and val:
            return val
    return "default"


# ─── env-wired singletons (one validator per issuer) ─────────────────────────

_validators: dict[str, OIDCValidator] = {}


def auth_required() -> bool:
    return os.environ.get("OSTIARI_GATEWAY_AUTH", "off").lower() == "required"


def resolve_issuer(tenant_id: str = "default") -> str:
    """Resolve the trusted issuer for a tenant. Single-issuer today; becomes a
    tenant→pool lookup for multi-tenant SaaS later (one-function change)."""
    return os.environ.get("OSTIARI_OIDC_ISSUER", "")


def get_validator(tenant_id: str = "default") -> OIDCValidator | None:
    if not auth_required():
        return None
    issuer = resolve_issuer(tenant_id)
    if not issuer:
        return None
    if issuer not in _validators:
        audience = os.environ.get("OSTIARI_OIDC_AUDIENCE") or None
        if audience is None:
            # Without an audience, any token from the trusted issuer is accepted
            # — including one minted for a DIFFERENT app sharing the same pool
            # (e.g. a sibling Cognito client). Pin OSTIARI_OIDC_AUDIENCE in
            # shared-IdP deployments.
            logging.getLogger("ostiari.oidc").warning(
                "OIDC audience not configured (OSTIARI_OIDC_AUDIENCE unset) — "
                "any token from issuer '%s' is accepted regardless of its 'aud'. "
                "Set OSTIARI_OIDC_AUDIENCE to restrict to this application.",
                issuer,
            )
        _validators[issuer] = OIDCValidator(
            issuer=issuer,
            jwks_url=os.environ.get("OSTIARI_OIDC_JWKS_URL") or None,
            audience=audience,
        )
    return _validators[issuer]


def reset_validator() -> None:
    _validators.clear()
