"""Tests for governance routers: quotas, costs, experiments, providers,
traces, audit, proxy."""

import pytest

pytestmark = pytest.mark.anyio


# ─── Quotas (DB-backed) ──────────────────────────────────────────────────────

class TestQuotas:
    async def test_create_list_delete(self, client):
        r = await client.post("/api/quotas", json={
            "name": "q1", "scope": "gateway", "scope_id": "gw1",
            "rate_limit_rpm": 100, "budget_limit_usd": 50.0,
        })
        assert r.status_code == 200, r.text
        qid = r.json()["id"]
        assert any(q["name"] == "q1" for q in (await client.get("/api/quotas")).json())
        assert (await client.request("DELETE", f"/api/quotas/{qid}")).status_code == 200

    async def test_push_missing_404(self, client):
        assert (await client.post("/api/quotas/99999/push")).status_code == 404


# ─── Costs (DB-backed) ───────────────────────────────────────────────────────

class TestCosts:
    async def test_record_and_summary(self, client):
        r = await client.post("/api/costs/record", json={
            "gateway_id": "gw1", "agent_id": "a1", "model": "gpt-4o",
            "input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "cost_usd": 0.01,
        })
        assert r.status_code == 200, r.text
        # summary reflects the record
        r = await client.get("/api/costs/summary")
        assert r.status_code == 200
        assert r.json()["total_cost_usd"] >= 0.01 - 1e-9

    async def test_estimated_cost_when_zero(self, client):
        # cost_usd=0 with tokens>0 triggers estimation (should not be negative)
        r = await client.post("/api/costs/record", json={
            "gateway_id": "gw1", "model": "gpt-4o",
            "input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500, "cost_usd": 0.0,
        })
        assert r.status_code == 200
        assert r.json()["cost_usd"] >= 0.0

    async def test_records_list(self, client):
        await client.post("/api/costs/record", json={
            "gateway_id": "gw2", "model": "claude", "total_tokens": 10, "cost_usd": 0.005,
        })
        assert (await client.get("/api/costs/records")).status_code == 200


# ─── Experiments (DB-backed) ─────────────────────────────────────────────────

class TestExperiments:
    async def test_create_toggle_results_delete(self, client):
        r = await client.post("/api/experiments", json={
            "name": "exp1", "model_a": "gpt-4o", "model_b": "claude", "traffic_pct_b": 20, "gateway_id": "gw1",
        })
        assert r.status_code == 200, r.text
        assert any(e["name"] == "exp1" for e in (await client.get("/api/experiments")).json())
        # toggle
        assert (await client.patch("/api/experiments/exp1/toggle")).status_code == 200
        # results
        assert (await client.get("/api/experiments/exp1/results")).status_code == 200
        # delete
        assert (await client.request("DELETE", "/api/experiments/exp1")).status_code == 200

    async def test_toggle_missing_404(self, client):
        assert (await client.patch("/api/experiments/ghost/toggle")).status_code == 404

    async def test_traffic_pct_validation_422(self, client):
        r = await client.post("/api/experiments", json={
            "name": "bad", "model_a": "a", "model_b": "b", "traffic_pct_b": 150, "gateway_id": "gw1",
        })
        assert r.status_code == 422  # ge=1, le=99


# ─── Providers (in-memory, admin-gated) ──────────────────────────────────────

class TestProviders:
    async def test_add_requires_admin(self, client, viewer_headers):
        r = await client.post("/api/providers", json={"name": "openai", "api_key": "sk-test"},
                              headers=viewer_headers)
        assert r.status_code == 403

    async def test_crud_as_admin(self, client, admin_headers):
        r = await client.post("/api/providers", json={"name": "openai", "api_key": "sk-secret123"},
                              headers=admin_headers)
        assert r.status_code == 200, r.text
        # list masks key (never returns raw secret)
        listed = (await client.get("/api/providers", headers=admin_headers)).json()
        rec = next(p for p in listed if p["name"] == "openai")
        assert "sk-secret123" not in str(rec)
        # explicit key-reveal endpoint returns the real key
        r = await client.get("/api/providers/openai/key", headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["api_key"] == "sk-secret123"
        # update + delete
        assert (await client.put("/api/providers/openai", json={"region": "us-east-1"},
                                 headers=admin_headers)).status_code == 200
        assert (await client.request("DELETE", "/api/providers/openai",
                                     headers=admin_headers)).status_code == 200

    async def test_key_reveal_requires_admin(self, client, viewer_headers):
        assert (await client.get("/api/providers/openai/key", headers=viewer_headers)).status_code == 403

    async def test_key_missing_provider_404(self, client, admin_headers):
        assert (await client.get("/api/providers/ghost/key", headers=admin_headers)).status_code == 404


# ─── Traces (in-memory) ──────────────────────────────────────────────────────

class TestTraces:
    async def test_ingest_and_recent(self, client):
        await client.post("/api/traces/ingest", json={
            "gateway_id": "gw1", "action": "web_search", "tier": "allow",
        })
        r = await client.get("/api/traces/recent")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] >= 1
        assert body["traces"][-1]["action"] == "web_search"

    async def test_recent_empty(self, client):
        r = await client.get("/api/traces/recent")
        assert r.status_code == 200
        assert r.json()["total"] == 0


# ─── Audit (DB-backed) ───────────────────────────────────────────────────────

class TestAudit:
    async def test_list_audit_ok(self, client):
        r = await client.get("/api/audit")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


# ─── Proxy (catch-all; unknown gateway should not 500) ───────────────────────

class TestProxy:
    async def test_proxy_unknown_gateway(self, client):
        r = await client.get("/api/proxy/gateway/ghost/config/llm")
        # No such gateway registered — expect a clean error, never a 500 crash.
        assert r.status_code in (404, 502, 503), r.text
