"""Tests for provider-agnostic OIDC JWT validation + claims mapping.

Uses a locally-generated RSA keypair and an in-process JWKS — no AWS/Cognito
needed. Proves the full validate path: good token, wrong issuer, wrong key,
expired, plus claims → role/tenant/principal mapping.
"""

import base64
import time

import jwt
import pytest
from control_plane.auth import oidc
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://issuer.test/pool"
AUD = "test-client"


def _base64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.fixture
def keypair():
    """Generate an RSA keypair and its JWKS."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_numbers()
    jwk_dict = {
        "kty": "RSA", "kid": "test-key-1", "use": "sig", "alg": "RS256",
        "n": _base64url_uint(pub.n),
        "e": _base64url_uint(pub.e),
    }
    # PyJWT can sign with a PEM private key directly.
    from cryptography.hazmat.primitives import serialization
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return {"pem": pem, "jwks": {"keys": [jwk_dict]}, "kid": "test-key-1", "_jwk": jwk_dict}


def _make_validator(keypair, issuer=ISSUER, audience=None):
    return oidc.OIDCValidator(
        issuer=issuer, jwks_url="https://issuer.test/jwks", audience=audience,
        http_get=lambda url: keypair["jwks"],
    )


def _token(keypair, *, issuer=ISSUER, aud=AUD, exp_delta=3600, extra=None):
    now = int(time.time())
    claims = {"sub": "user-123", "iss": issuer, "aud": aud,
              "iat": now, "exp": now + exp_delta}
    if extra:
        claims.update(extra)
    return jwt.encode(claims, keypair["pem"], algorithm="RS256",
                      headers={"kid": keypair["kid"]})


class TestValidate:
    def test_valid_token(self, keypair):
        v = _make_validator(keypair)
        claims = v.validate(_token(keypair))
        assert claims["sub"] == "user-123"

    def test_wrong_issuer_rejected(self, keypair):
        v = _make_validator(keypair, issuer="https://other.test")
        with pytest.raises(oidc.OIDCError):
            v.validate(_token(keypair))

    def test_expired_token_rejected(self, keypair):
        v = _make_validator(keypair)
        with pytest.raises(oidc.OIDCError):
            v.validate(_token(keypair, exp_delta=-10))

    def test_unknown_kid_rejected(self, keypair):
        v = _make_validator(keypair)
        bad = jwt.encode({"sub": "x", "iss": ISSUER, "exp": int(time.time()) + 60},
                         keypair["pem"], algorithm="RS256", headers={"kid": "nope"})
        with pytest.raises(oidc.OIDCError):
            v.validate(bad)

    def test_audience_enforced_when_set(self, keypair):
        v = _make_validator(keypair, audience="expected-aud")
        with pytest.raises(oidc.OIDCError):
            v.validate(_token(keypair, aud="wrong-aud"))

    def test_malformed_token(self, keypair):
        v = _make_validator(keypair)
        with pytest.raises(oidc.OIDCError):
            v.validate("not.a.jwt")

    def test_jwks_cached_after_first_fetch(self, keypair):
        calls = {"n": 0}
        def counting_get(url):
            calls["n"] += 1
            return keypair["jwks"]
        v = oidc.OIDCValidator(issuer=ISSUER, jwks_url="x", http_get=counting_get)
        v.validate(_token(keypair))
        v.validate(_token(keypair))
        assert calls["n"] == 1  # fetched once, then cached


class TestRoleMapping:
    def test_explicit_role_claim(self):
        assert oidc._role_from_claims({"role": "admin"}) == "admin"
        assert oidc._role_from_claims({"custom:role": "operator"}) == "operator"

    def test_cognito_groups(self):
        assert oidc._role_from_claims({"cognito:groups": ["ostiari-admin"]}) == "admin"
        assert oidc._role_from_claims({"cognito:groups": ["viewers"]}) == "viewer"

    def test_scope_based(self):
        assert oidc._role_from_claims({"scope": "ostiari/admin"}) == "admin"
        assert oidc._role_from_claims({"scope": "write:tools"}) == "operator"

    def test_default_least_privilege(self):
        assert oidc._role_from_claims({}) == "viewer"

    def test_admin_wins_over_viewer(self):
        assert oidc._role_from_claims({"groups": ["viewer", "admin"]}) == "admin"


class TestTenantMapping:
    def test_default_when_absent(self):
        assert oidc.tenant_from_claims({}) == "default"

    def test_reads_tenant_claim(self):
        assert oidc.tenant_from_claims({"tenant_id": "acme"}) == "acme"
        assert oidc.tenant_from_claims({"custom:org": "globex"}) == "globex"


class TestPrincipalMapping:
    def test_user_principal(self):
        p = oidc.principal_from_claims({"sub": "u1", "email": "a@b.com",
                                        "cognito:groups": ["operator"]})
        assert p.kind == "user" and p.role == "operator"
        assert p.email == "a@b.com" and p.subject == "u1"
        assert p.tenant_id == "default"

    def test_service_principal(self):
        # client-credentials token: access token, no email
        p = oidc.principal_from_claims({"sub": "svc-1", "token_use": "access",
                                        "client_id": "svc-1", "scope": "ostiari/invoke"})
        assert p.kind == "service"
        assert p.subject == "svc-1"


class TestIssuerResolver:
    def test_single_issuer_default(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_OIDC_ISSUER", "https://x/pool")
        assert oidc.resolve_issuer() == "https://x/pool"
        assert oidc.resolve_issuer("any-tenant") == "https://x/pool"  # tenant-agnostic today


class TestEnvGating:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("OSTIARI_AUTH_MODE", raising=False)
        oidc.reset_validator()
        assert oidc.is_oidc_enabled() is False
        assert oidc.get_validator() is None

    def test_enabled_with_issuer(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_AUTH_MODE", "oidc")
        monkeypatch.setenv("OSTIARI_OIDC_ISSUER", "https://x/pool")
        oidc.reset_validator()
        assert oidc.is_oidc_enabled() is True
        assert oidc.get_validator() is not None
        oidc.reset_validator()


@pytest.mark.anyio
class TestGetCurrentUserOIDC:
    """End-to-end: a protected endpoint accepts a Cognito-style OIDC token when
    AUTH_MODE=oidc, and still uses local tokens by default."""

    async def test_oidc_token_accepted_on_protected_route(self, client, keypair, monkeypatch):
        # Force OIDC mode and point get_validator() at our in-test validator.
        monkeypatch.setenv("OSTIARI_AUTH_MODE", "oidc")
        v = _make_validator(keypair)
        monkeypatch.setattr(oidc, "get_validator", lambda *a, **k: v)

        token = _token(keypair, extra={"email": "op@corp.com", "cognito:groups": ["admin"]})
        r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        # 'me' echoes the principal — admin group mapped through.
        assert r.json().get("role") == "admin"

    async def test_oidc_invalid_token_rejected(self, client, keypair, monkeypatch):
        monkeypatch.setenv("OSTIARI_AUTH_MODE", "oidc")
        v = _make_validator(keypair, issuer="https://someone-else")  # our token won't match
        monkeypatch.setattr(oidc, "get_validator", lambda *a, **k: v)
        token = _token(keypair)
        r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401
