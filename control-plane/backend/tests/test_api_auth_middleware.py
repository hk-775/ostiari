"""Tests for the control-plane API auth middleware (assessment finding #1).

Default (OSTIARI_REQUIRE_AUTH unset): open, preserving the demo. When set,
unauthenticated /api/* calls (except the public allowlist) get 401.
"""

import pytest

pytestmark = pytest.mark.anyio


class TestAuthMiddleware:
    async def test_open_by_default(self, client, monkeypatch):
        monkeypatch.delenv("OSTIARI_REQUIRE_AUTH", raising=False)
        # unauthenticated read works in demo mode
        assert (await client.get("/api/gateways")).status_code == 200

    async def test_enforced_when_enabled_blocks_unauthed(self, client, monkeypatch):
        monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
        r = await client.get("/api/gateways")
        assert r.status_code == 401
        assert "WWW-Authenticate" in r.headers

    async def test_health_public_when_enforced(self, client, monkeypatch):
        monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
        assert (await client.get("/api/health")).status_code == 200

    async def test_login_public_when_enforced(self, client, monkeypatch):
        monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
        # login must stay reachable to obtain a token; 401 here would be a lockout,
        # a 200/400/422 (bad creds/validation) all prove the path is NOT auth-gated
        r = await client.post("/api/auth/login", json={"email": "x@y.z", "password": "wrong"})
        assert r.status_code != 401 or r.headers.get("WWW-Authenticate") is None

    async def test_trace_ingest_public_when_enforced(self, client, monkeypatch):
        # machine ingest has its own shared-secret guard; middleware must not 401 it
        monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
        monkeypatch.delenv("OSTIARI_INGEST_KEY", raising=False)
        r = await client.post("/api/traces/ingest", json={"action": "x", "tier": "allow"})
        assert r.status_code == 200

    async def test_valid_token_allowed_when_enforced(self, client, monkeypatch, admin_headers):
        monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
        r = await client.get("/api/gateways", headers=admin_headers)
        assert r.status_code == 200
