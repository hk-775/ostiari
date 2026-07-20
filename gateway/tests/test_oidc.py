"""Tests for the gateway's OIDC auth: token validation + X-Agent-Id matching.

Uses a locally-generated RSA keypair and in-process JWKS — no AWS needed.
Verifies the gate stays OFF by default (header trust preserved for the demo)
and, when required, enforces a valid token whose agent identity matches
X-Agent-Id.
"""

import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt
from jose.utils import long_to_base64
from ostiari_gateway import oidc
from ostiari_gateway.models import PolicyConfig, SidecarConfig, ToolDefinition
from ostiari_gateway.server import create_app
from starlette.testclient import TestClient

ISSUER = "https://issuer.test/pool"


@pytest.fixture
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_numbers()
    jwk_dict = {"kty": "RSA", "kid": "gw-key-1", "use": "sig", "alg": "RS256",
                "n": long_to_base64(pub.n).decode(), "e": long_to_base64(pub.e).decode()}
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return {"pem": pem, "jwks": {"keys": [jwk_dict]}, "kid": "gw-key-1"}


def _token(keypair, *, agent_id="research-agent", issuer=ISSUER, exp_delta=3600):
    now = int(time.time())
    claims = {"sub": agent_id, "agent_id": agent_id, "token_use": "access",
              "client_id": agent_id, "iss": issuer, "iat": now, "exp": now + exp_delta}
    return jwt.encode(claims, keypair["pem"], algorithm="RS256", headers={"kid": keypair["kid"]})


class TestValidatorUnit:
    def test_valid(self, keypair):
        v = oidc.OIDCValidator(issuer=ISSUER, jwks_url="x", http_get=lambda u: keypair["jwks"])
        claims = v.validate(_token(keypair))
        assert claims["agent_id"] == "research-agent"

    def test_agent_id_from_claims(self):
        assert oidc.agent_id_from_claims({"agent_id": "a"}) == "a"
        assert oidc.agent_id_from_claims({"client_id": "svc"}) == "svc"
        assert oidc.agent_id_from_claims({"sub": "s"}) == "s"

    def test_tenant_default(self):
        assert oidc.tenant_from_claims({}) == "default"
        assert oidc.tenant_from_claims({"tenant_id": "acme"}) == "acme"


# ─── Enforcement through the proxy ───────────────────────────────────────────

def _app_with_tool(httpserver):
    httpserver.expect_request("/echo", method="POST").respond_with_json({"ok": True})
    config = SidecarConfig(
        sidecar_id="crm-agent",
        tools=[ToolDefinition(name="web_search", endpoint=httpserver.url_for("/echo"))],
        policy=PolicyConfig(allow=["web_search"]),
    )
    return create_app(initial_config=config)


class TestGatewayAuthOffByDefault:
    def test_no_token_needed_when_auth_off(self, httpserver, monkeypatch):
        monkeypatch.delenv("OSTIARI_GATEWAY_AUTH", raising=False)
        oidc.reset_validator()
        client = TestClient(_app_with_tool(httpserver))
        # No Authorization header — still works (header-trust preserved).
        r = client.post("/tool/web_search", json={"q": "x"}, headers={"X-Agent-Id": "research-agent"})
        assert r.status_code == 200


class TestGatewayAuthRequired:
    @pytest.fixture(autouse=True)
    def _enable_auth(self, keypair, monkeypatch):
        monkeypatch.setenv("OSTIARI_GATEWAY_AUTH", "required")
        v = oidc.OIDCValidator(issuer=ISSUER, jwks_url="x", http_get=lambda u: keypair["jwks"])
        monkeypatch.setattr(oidc, "get_validator", lambda *a, **k: v)
        yield
        oidc.reset_validator()

    def test_missing_token_401(self, httpserver):
        client = TestClient(_app_with_tool(httpserver))
        r = client.post("/tool/web_search", json={"q": "x"}, headers={"X-Agent-Id": "research-agent"})
        assert r.status_code == 401

    def test_valid_token_matching_agent_200(self, httpserver, keypair):
        client = TestClient(_app_with_tool(httpserver))
        tok = _token(keypair, agent_id="research-agent")
        r = client.post("/tool/web_search", json={"q": "x"},
                        headers={"X-Agent-Id": "research-agent", "Authorization": f"Bearer {tok}"})
        assert r.status_code == 200

    def test_token_agent_mismatch_403(self, httpserver, keypair):
        client = TestClient(_app_with_tool(httpserver))
        tok = _token(keypair, agent_id="other-agent")  # token says other, header says research
        r = client.post("/tool/web_search", json={"q": "x"},
                        headers={"X-Agent-Id": "research-agent", "Authorization": f"Bearer {tok}"})
        assert r.status_code == 403

    def test_expired_token_401(self, httpserver, keypair):
        client = TestClient(_app_with_tool(httpserver))
        tok = _token(keypair, exp_delta=-10)
        r = client.post("/tool/web_search", json={"q": "x"},
                        headers={"X-Agent-Id": "research-agent", "Authorization": f"Bearer {tok}"})
        assert r.status_code == 401
