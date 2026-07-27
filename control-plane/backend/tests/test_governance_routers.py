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


class TestOpenAICompatibleProviders:
    """xAI and Together are OpenAI-compatible and share one /test branch.

    Before wiring, both fell through to `else: Unknown provider type`, so a
    configured key could never verify — the reason register_demo_providers.py
    used to skip them. These tests stub the outbound call: the contract under
    test is the branch's request shape and status mapping, not the vendor's API.
    """

    @pytest.fixture
    def captured(self, monkeypatch):
        """Capture the provider module's outbound probe and control its response.

        Patches the AsyncClient the router itself constructs, NOT httpx globally —
        the ASGI test client is also an httpx.AsyncClient, so a global patch
        swallows the inbound request and `calls` records that instead.
        """
        import httpx as _httpx
        from control_plane.routers import providers as _mod

        calls: list[dict] = []
        state = {"status": 200}

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, headers=None, json=None, **kw):
                calls.append({"url": url, "headers": headers or {}, "json": json or {}})
                return _httpx.Response(state["status"], json={"ok": True})

        monkeypatch.setattr(_mod.httpx, "AsyncClient", _FakeClient)
        return calls, state

    @pytest.mark.parametrize(
        ("name", "host", "model"),
        [("xai", "https://api.x.ai", "grok-3-mini"),
         ("together", "https://api.together.xyz", "meta-llama/Llama-3.3-70B-Instruct-Turbo")],
    )
    async def test_probe_targets_the_right_endpoint(self, client, admin_headers, captured,
                                                   name, host, model):
        calls, _ = captured
        await client.post("/api/providers", json={"name": name, "api_key": "sk-live"},
                          headers=admin_headers)
        r = await client.post(f"/api/providers/{name}/test", headers=admin_headers)
        assert r.status_code == 200, r.text
        assert r.json()["success"] is True
        # Endpoint, auth scheme and probe model must match what AxonLLM routes
        # to — a divergence would pass a key the router can't actually use.
        assert calls[-1]["url"] == f"{host}/v1/chat/completions"
        assert calls[-1]["headers"]["Authorization"] == "Bearer sk-live"
        assert calls[-1]["json"]["model"] == model

    @pytest.mark.parametrize("name", ["xai", "together"])
    async def test_no_longer_unknown_provider_type(self, client, admin_headers, captured, name):
        await client.post("/api/providers", json={"name": name, "api_key": "k"},
                          headers=admin_headers)
        body = (await client.post(f"/api/providers/{name}/test", headers=admin_headers)).json()
        assert "Unknown provider type" not in str(body.get("error", ""))

    @pytest.mark.parametrize("code", [401, 403])
    async def test_rejected_key_reports_invalid(self, client, admin_headers, captured, code):
        """403 matters here: xAI returns it for a key with no credit, which a
        `401-only` check would have reported as a healthy provider."""
        _, state = captured
        state["status"] = code
        await client.post("/api/providers", json={"name": "xai", "api_key": "bad"},
                          headers=admin_headers)
        body = (await client.post("/api/providers/xai/test", headers=admin_headers)).json()
        assert body["success"] is False
        assert body["error"] == "Invalid API key"

    async def test_server_error_is_not_reported_as_connected(self, client, admin_headers, captured):
        _, state = captured
        state["status"] = 503
        await client.post("/api/providers", json={"name": "together", "api_key": "k"},
                          headers=admin_headers)
        body = (await client.post("/api/providers/together/test", headers=admin_headers)).json()
        assert body["success"] is False

    async def test_custom_base_url_overrides_the_default(self, client, admin_headers, captured):
        calls, _ = captured
        await client.post("/api/providers", json={"name": "xai", "api_key": "k",
                                                 "api_base_url": "http://proxy.internal"},
                          headers=admin_headers)
        await client.post("/api/providers/xai/test", headers=admin_headers)
        assert calls[-1]["url"] == "http://proxy.internal/v1/chat/completions"

    @pytest.mark.parametrize("name", ["xai", "together"])
    async def test_models_are_advertised(self, client, admin_headers, captured, name):
        await client.post("/api/providers", json={"name": name, "api_key": "k"},
                          headers=admin_headers)
        await client.post(f"/api/providers/{name}/test", headers=admin_headers)
        listed = (await client.get("/api/providers", headers=admin_headers)).json()
        rec = next(p for p in listed if p["name"] == name)
        assert rec["models_available"], "a connected provider must advertise its models"

    @pytest.mark.parametrize(
        ("model", "provider"),
        [("grok-3", "xai"), ("grok-3-mini", "xai"),
         ("llama-3.3-70b", "together"), ("deepseek-r1-together", "together")],
    )
    async def test_registry_has_routable_models(self, client, model, provider):
        """A provider with no models in the registry is display-only — the
        router needs an entry mapping a model name to that provider."""
        from control_plane.routers.model_config import seed_models
        seed_models()  # conftest clears _models between tests
        models = (await client.get("/api/models")).json()
        entry = next((m for m in models if m["name"] == model), None)
        assert entry is not None, f"{model} missing from the model registry"
        assert provider in [p["provider"] for p in entry["providers"]]


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


class TestShadowReport:
    async def test_empty_report(self, client):
        r = await client.get("/api/traces/shadow-report")
        assert r.status_code == 200
        body = r.json()
        assert body["total_shadow_calls"] == 0
        assert body["would_block_count"] == 0
        assert body["offending_actions"] == []

    async def test_aggregates_would_block(self, client):
        # Ingest a mix: 2 would-block on send_email, 1 would-block on delete_db,
        # 1 allowed shadow, 1 non-shadow (ignored).
        events = [
            {"action": "send_email", "shadow": True, "would_block": True, "score": 80, "blocked_reason": "PII"},
            {"action": "send_email", "shadow": True, "would_block": True, "score": 90, "blocked_reason": "PII"},
            {"action": "delete_db", "shadow": True, "would_block": True, "score": 100, "blocked_reason": "destructive"},
            {"action": "read_doc", "shadow": True, "would_block": False, "score": 5},
            {"action": "normal", "shadow": False, "would_block": False, "score": 0},
        ]
        for e in events:
            await client.post("/api/traces/ingest", json=e)

        r = await client.get("/api/traces/shadow-report")
        body = r.json()
        assert body["total_shadow_calls"] == 4        # excludes the non-shadow event
        assert body["would_block_count"] == 3
        assert body["would_allow_count"] == 1
        assert body["block_rate"] == 0.75
        # offenders sorted by count desc: send_email (2) first
        offenders = body["offending_actions"]
        assert offenders[0]["action"] == "send_email"
        assert offenders[0]["count"] == 2
        assert offenders[0]["max_score"] == 90
        assert offenders[0]["reasons"] == ["PII"]


class TestDelegationReport:
    async def test_empty(self, client):
        r = await client.get("/api/traces/delegation-report")
        assert r.status_code == 200
        b = r.json()
        assert b["blocked_delegation_count"] == 0 and b["edges"] == []

    async def test_aggregates_blocked_edges(self, client):
        events = [
            {"action": "a2a.payments", "limit_type": "cross_agent_delegation", "would_block": True,
             "shadow": False, "blocked_reason": "not permitted", "delegation_chain": ["research"]},
            {"action": "a2a.payments", "limit_type": "cross_agent_delegation", "would_block": True,
             "shadow": False, "blocked_reason": "not permitted", "delegation_chain": ["research"]},
            {"action": "a2a.db", "limit_type": "cross_agent_delegation", "would_block": True,
             "shadow": True, "blocked_reason": "low trust", "delegation_chain": ["ops", "coder"]},
            # noise: a non-delegation trace must be ignored
            {"action": "send_email", "limit_type": "", "would_block": True, "shadow": True},
        ]
        for e in events:
            await client.post("/api/traces/ingest", json=e)
        b = (await client.get("/api/traces/delegation-report")).json()
        assert b["blocked_delegation_count"] == 3
        assert b["distinct_edges"] == 2
        top = b["edges"][0]
        assert top["caller"] == "research" and top["callee"] == "payments"
        assert top["count"] == 2
        assert top["reasons"] == ["not permitted"]
        # the ops->coder edge came from a shadow trace
        db_edge = next(e for e in b["edges"] if e["callee"] == "db")
        assert db_edge["caller"] == "coder"  # chain[-1]
        assert db_edge["shadow"] is True
