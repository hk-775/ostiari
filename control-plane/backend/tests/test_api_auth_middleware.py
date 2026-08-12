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

    async def test_service_key_only_allows_machine_routes(self, client, monkeypatch):
        monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
        monkeypatch.setenv("OSTIARI_SERVICE_TOKEN", "machine-secret")
        headers = {"X-Ostiari-Service-Key": "machine-secret"}

        registered = await client.post(
            "/api/gateways/service-auth-test/register", json={}, headers=headers
        )
        assert registered.status_code == 200
        assert (await client.get("/api/gateways", headers=headers)).status_code == 401

    async def test_operator_token_cannot_call_machine_only_lifecycle_route(
        self,
        client,
        monkeypatch,
        admin_headers,
    ):
        monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
        monkeypatch.setenv("OSTIARI_SERVICE_TOKEN", "machine-secret")

        registered = await client.post(
            "/api/gateways/operator-register/register",
            json={},
            headers=admin_headers,
        )

        assert registered.status_code == 401
        assert registered.json()["detail"] == (
            "Gateway service authentication required"
        )

    async def test_service_key_allows_cost_ingest(self, client, monkeypatch, admin_headers):
        monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
        monkeypatch.setenv("OSTIARI_SERVICE_TOKEN", "machine-secret")
        service_headers = {"X-Ostiari-Service-Key": "machine-secret"}
        await client.post(
            "/api/gateways",
            headers=admin_headers,
            json={"id": "cost-gateway", "name": "Cost Gateway", "endpoint": "http://gateway"},
        )
        record = {
            "gateway_id": "cost-gateway",
            "agent_id": "billing-test",
            "model": "gpt-4o-mini",
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        }

        single = await client.post("/api/costs/record", json=record, headers=service_headers)
        batch = await client.post("/api/costs/record/batch", json=[record], headers=service_headers)

        assert single.status_code == 200
        assert batch.status_code == 200
        assert batch.json() == {"recorded": 1}

    async def test_cost_ingest_rejects_missing_service_key(self, client, monkeypatch):
        monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
        monkeypatch.setenv("OSTIARI_SERVICE_TOKEN", "machine-secret")

        r = await client.post("/api/costs/record/batch", json=[])

        assert r.status_code == 401

    async def test_machine_route_rejects_wrong_service_key(self, client, monkeypatch):
        monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
        monkeypatch.setenv("OSTIARI_SERVICE_TOKEN", "machine-secret")
        r = await client.post(
            "/api/gateways/service-auth-test/register",
            json={},
            headers={"X-Ostiari-Service-Key": "wrong"},
        )
        assert r.status_code == 401

    async def test_viewer_is_read_only(self, client, monkeypatch, viewer_headers):
        monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
        assert (await client.get("/api/gateways", headers=viewer_headers)).status_code == 200
        r = await client.post(
            "/api/gateways",
            json={"id": "viewer-write", "name": "No", "endpoint": "http://gateway"},
            headers=viewer_headers,
        )
        assert r.status_code == 403
