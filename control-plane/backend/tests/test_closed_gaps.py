"""Tests for the previously-documented gaps between control plane and gateway.

Each class here covers something the docs described as "configurable in the UI
but never reaching the gateway", or a mutation that changed governance without
leaving an audit trail.
"""

import pytest

pytestmark = pytest.mark.anyio


async def _make_gateway(client, gid="gw1", endpoint="http://localhost:9001"):
    return await client.post("/api/gateways", json={
        "id": gid, "name": f"GW {gid}", "endpoint": endpoint, "description": "d",
    })


async def _audit_entries(client, resource_type=None, headers=None):
    """Audit entries in the order they were written.

    /api/audit lists newest-first (the useful order for a reader); these tests
    assert on sequences of actions, so reverse to chronological.
    """
    r = await client.get("/api/audit", headers=headers)
    assert r.status_code == 200, r.text
    rows = r.json()
    rows = rows if isinstance(rows, list) else rows.get("entries", rows.get("items", []))
    rows = list(reversed(rows))
    if resource_type:
        rows = [e for e in rows if e["resource_type"] == resource_type]
    return rows


# ─── Quota push now carries pricing ──────────────────────────────────────────

class TestQuotaPricing:
    async def test_pricing_table_shape_matches_gateway(self):
        """The registry is per-1k, same unit the gateway's enforcer takes."""
        from control_plane.routers.model_config import ModelConfig, _models, pricing_table

        _models["default"]["m1"] = ModelConfig(
            name="m1", input_cost_per_1k=0.003, output_cost_per_1k=0.015)
        table = pricing_table("default")
        assert table["m1"] == {"input": 0.003, "output": 0.015}

    async def test_zero_priced_models_omitted(self):
        """A missing model means "fall back to DEFAULT_PRICING" in the gateway,
        which beats asserting a real model is free (that disables the budget)."""
        from control_plane.routers.model_config import ModelConfig, _models, pricing_table

        _models["default"].clear()
        _models["default"]["free"] = ModelConfig(name="free")
        _models["default"]["paid"] = ModelConfig(name="paid", input_cost_per_1k=0.001)
        table = pricing_table("default")
        assert "free" not in table and "paid" in table

    async def test_push_includes_pricing(self, client, monkeypatch):
        from control_plane.routers.model_config import ModelConfig, _models

        await _make_gateway(client)
        _models["default"]["m1"] = ModelConfig(
            name="m1", input_cost_per_1k=0.002, output_cost_per_1k=0.008)
        q = await client.post("/api/quotas", json={
            "name": "q", "scope": "gateway", "scope_id": "gw1", "budget_limit_usd": 10.0})
        qid = q.json()["id"]

        sent = {}

        class _Resp:
            status_code = 200
            def json(self): return {"status": "ok"}

        class _Client:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, json=None):
                sent["url"], sent["json"] = url, json
                return _Resp()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        r = await client.post(f"/api/quotas/{qid}/push")
        assert r.status_code == 200, r.text
        assert sent["json"]["pricing"]["m1"] == {"input": 0.002, "output": 0.008}


# ─── Budget alerts now have somewhere to land ────────────────────────────────

class TestBudgetAlerts:
    async def test_ingest_and_list(self, client):
        await _make_gateway(client)
        r = await client.post("/api/quotas/alerts", json={
            "gateway_id": "gw1", "threshold": "80%",
            "spend_usd": 8.0, "budget_usd": 10.0})
        assert r.status_code == 200 and r.json() == {"recorded": True}

        alerts = (await client.get("/api/quotas/alerts")).json()
        assert len(alerts) == 1
        assert alerts[0]["threshold"] == "80%" and alerts[0]["gateway_id"] == "gw1"
        # Timestamp is stamped server-side when the gateway omits it.
        assert alerts[0]["timestamp"] > 0

    async def test_newest_first_and_clear(self, client):
        await _make_gateway(client)
        for t in ("80%", "90%", "100%"):
            await client.post("/api/quotas/alerts", json={"gateway_id": "gw1", "threshold": t})
        alerts = (await client.get("/api/quotas/alerts")).json()
        assert [a["threshold"] for a in alerts] == ["100%", "90%", "80%"]

        assert (await client.request("DELETE", "/api/quotas/alerts")).json() == {"cleared": 3}
        assert (await client.get("/api/quotas/alerts")).json() == []

    async def test_retries_are_idempotent_and_conflicts_fail_closed(self, client):
        await _make_gateway(client)
        event = {
            "event_id": "budget-event-1",
            "gateway_id": "gw1",
            "threshold": "90%",
            "spend_usd": 9.0,
            "budget_usd": 10.0,
            "timestamp": 1_787_000_000.0,
        }
        first = await client.post("/api/quotas/alerts", json=event)
        duplicate = await client.post("/api/quotas/alerts", json=event)

        assert first.json() == {"recorded": True}
        assert duplicate.json() == {
            "recorded": True,
            "duplicate": True,
            "event_id": "budget-event-1",
        }
        assert len((await client.get("/api/quotas/alerts")).json()) == 1

        conflict = await client.post(
            "/api/quotas/alerts",
            json={**event, "threshold": "100%"},
        )
        assert conflict.status_code == 409

    async def test_alerts_route_not_shadowed_by_quota_id(self, client):
        """/alerts must be declared before /{quota_id}, or DELETE /alerts would
        try to parse "alerts" as an int quota id and 422."""
        assert (await client.request("DELETE", "/api/quotas/alerts")).status_code == 200

    async def test_adding_put_did_not_disturb_the_alert_routes(self, client):
        """PUT /{quota_id} is the only PUT on this router, so it cannot shadow the
        /alerts routes — but assert that directly rather than trusting it.

        PUT /api/quotas/alerts itself 422s (the path-param route matches and can't
        parse "alerts" as an int) rather than 405. That's inherent to a path-param
        PUT and harmless: there is no PUT /alerts to reach.
        """
        await _make_gateway(client)
        await client.post("/api/quotas/alerts", json={"gateway_id": "gw1", "threshold": "80%"})
        assert len((await client.get("/api/quotas/alerts")).json()) == 1
        assert (await client.request("DELETE", "/api/quotas/alerts")).json() == {"cleared": 1}
        assert (await client.put("/api/quotas/alerts", json={})).status_code == 422

    async def test_alerts_survive_a_restart(self, client):
        """The SQL-backed store restores the bounded hot cache after a restart."""
        from collections import deque

        from control_plane.database import async_session
        from control_plane.persistence import load_runtime_caches
        from control_plane.routers.quotas import ALERT_HISTORY, _alerts

        await _make_gateway(client)
        for t in ("80%", "100%"):
            await client.post("/api/quotas/alerts", json={
                "gateway_id": "gw1", "threshold": t, "spend_usd": 9.0, "budget_usd": 10.0})

        _alerts.clear()
        assert (await client.get("/api/quotas/alerts")).json() == []
        async with async_session() as db:
            await load_runtime_caches(db)

        alerts = (await client.get("/api/quotas/alerts")).json()
        assert [a["threshold"] for a in alerts] == ["100%", "80%"]   # newest first
        assert alerts[0]["spend_usd"] == 9.0 and alerts[0]["gateway_id"] == "gw1"

        # Restored as a bounded deque, not a plain list: reloading must not quietly
        # remove the cap that keeps a chatty fleet from growing this forever.
        assert isinstance(_alerts["default"], deque)
        assert _alerts["default"].maxlen == ALERT_HISTORY


# ─── Editing a quota actually saves ──────────────────────────────────────────

class TestQuotaUpdate:
    """PUT /api/quotas/{id} had no handler, but the Quotas page's Edit → Save
    button called it. It answered 405; the panel closed and the list refetched
    unchanged, which is indistinguishable from a successful save.
    """

    async def _quota(self, client, **kw):
        body = {"name": "q1", "scope": "gateway", "scope_id": "gw1",
                "rate_limit_rpm": 60, "budget_limit_usd": 10.0, **kw}
        return (await client.post("/api/quotas", json=body)).json()

    async def test_update_persists(self, client):
        q = await self._quota(client)
        r = await client.put(f"/api/quotas/{q['id']}", json={"budget_limit_usd": 99.5})
        assert r.status_code == 200, r.text
        assert r.json()["budget_limit_usd"] == 99.5
        listed = (await client.get("/api/quotas")).json()
        assert listed[0]["budget_limit_usd"] == 99.5

    async def test_omitted_fields_are_untouched(self, client):
        """A partial body must not blank the limits it doesn't mention — the edit
        panel sends only the fields the operator filled in."""
        q = await self._quota(client)
        r = await client.put(f"/api/quotas/{q['id']}", json={"budget_limit_usd": 1.0})
        assert r.json()["rate_limit_rpm"] == 60
        assert r.json()["name"] == "q1"

    async def test_explicit_null_clears_a_limit(self, client):
        """Distinct from omission: this is how an operator removes a budget cap."""
        q = await self._quota(client)
        r = await client.put(f"/api/quotas/{q['id']}", json={"budget_limit_usd": None})
        assert r.json()["budget_limit_usd"] is None
        assert r.json()["rate_limit_rpm"] == 60      # still untouched

    async def test_empty_body_is_a_noop(self, client):
        q = await self._quota(client)
        r = await client.put(f"/api/quotas/{q['id']}", json={})
        assert r.status_code == 200
        assert r.json()["rate_limit_rpm"] == 60 and r.json()["budget_limit_usd"] == 10.0

    async def test_missing_quota_404s(self, client):
        assert (await client.put("/api/quotas/9999", json={"budget_limit_usd": 1})).status_code == 404

    async def test_update_is_audited(self, client):
        """Spend and rate controls: "who raised this budget" is an audit question,
        so the update leaves a trail like create and delete do."""
        q = await self._quota(client)
        await client.put(f"/api/quotas/{q['id']}", json={"budget_limit_usd": 500.0})
        rows = await _audit_entries(client, "quota")
        assert [e["action"] for e in rows] == ["create", "update"]
        assert rows[-1]["details"]["budget_limit_usd"] == 500.0

    @pytest.mark.usefixtures("multi_tenant_mode")
    async def test_another_org_cannot_edit(self, client):
        """The store is per-org, so a quota id from one tenant must not resolve in
        another — same 404 as a nonexistent id."""
        from control_plane.auth.service import create_access_token

        q = await self._quota(client)
        tok = create_access_token(user_id=1, email="b@t.io", role="admin", org="org-b")
        r = await client.put(f"/api/quotas/{q['id']}", json={"budget_limit_usd": 1},
                             headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 404


# ─── A/B experiments now reach the gateway ───────────────────────────────────

class TestExperimentPush:
    async def test_for_gateway_shape(self, client):
        from control_plane.routers.experiments import _for_gateway

        await _make_gateway(client)
        await client.post("/api/experiments", json={
            "name": "e1", "model_a": "a", "model_b": "b",
            "traffic_pct_b": 25, "gateway_id": "gw1"})
        out = _for_gateway("default", "gw1")
        assert out == [{"name": "e1", "enabled": True, "model_a": "a", "model_b": "b",
                        "traffic_pct_b": 25, "agents": []}]

    async def test_other_gateways_experiments_excluded(self, client):
        from control_plane.routers.experiments import _for_gateway

        await _make_gateway(client, "gw1")
        await _make_gateway(client, "gw2")
        await client.post("/api/experiments", json={
            "name": "e1", "model_a": "a", "model_b": "b", "gateway_id": "gw1"})
        await client.post("/api/experiments", json={
            "name": "e2", "model_a": "a", "model_b": "b", "gateway_id": "gw2"})
        assert [e["name"] for e in _for_gateway("default", "gw1")] == ["e1"]

    async def test_create_reports_push_outcome(self, client):
        """The gateway isn't running in tests, so pushed=False — but the failure
        is reported rather than swallowed, and the experiment is still stored."""
        await _make_gateway(client)
        r = await client.post("/api/experiments", json={
            "name": "e1", "model_a": "a", "model_b": "b", "gateway_id": "gw1"})
        assert r.status_code == 200
        assert r.json()["pushed"] is False and r.json()["push_error"]
        assert [e["name"] for e in (await client.get("/api/experiments")).json()] == ["e1"]

    async def test_delete_pushes_remaining_set(self, client, monkeypatch):
        """A delete only takes effect because the push is a full replace."""
        pushes = []

        class _Resp:
            status_code = 200
            text = "ok"

        class _Client:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, json=None):
                pushes.append(json)
                return _Resp()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        await _make_gateway(client)
        await client.post("/api/experiments", json={
            "name": "e1", "model_a": "a", "model_b": "b", "gateway_id": "gw1"})
        await client.post("/api/experiments", json={
            "name": "e2", "model_a": "a", "model_b": "b", "gateway_id": "gw1"})
        await client.request("DELETE", "/api/experiments/e1")

        assert pushes[-1]["ab_experiments"] and len(pushes[-1]["ab_experiments"]) == 1
        assert pushes[-1]["ab_experiments"][0]["name"] == "e2"

    async def test_bundle_carries_experiments(self, client):
        """So a gateway restart doesn't silently end a running experiment."""
        await _make_gateway(client)
        await client.post("/api/experiments", json={
            "name": "e1", "model_a": "a", "model_b": "b", "gateway_id": "gw1"})
        bundle = (await client.get("/api/gateways/gw1/config-bundle")).json()
        assert [e["name"] for e in bundle["ab_experiments"]] == ["e1"]

    async def test_bundle_omits_key_when_no_experiments(self, client):
        await _make_gateway(client)
        bundle = (await client.get("/api/gateways/gw1/config-bundle")).json()
        assert "ab_experiments" not in bundle


# ─── Audit coverage beyond gateways and policies ─────────────────────────────

class TestAuditCoverage:
    async def test_tool_create_and_delete_audited(self, client):
        await _make_gateway(client)
        r = await client.post("/api/tools/gw1", json={
            "name": "db_query", "endpoint": "http://x/q", "method": "POST"})
        tool_id = r.json()["id"]
        await client.request("DELETE", f"/api/tools/{tool_id}")

        actions = [e["action"] for e in await _audit_entries(client, "tool")]
        assert actions == ["create", "delete"]

    async def test_tool_delete_records_name_before_row_is_gone(self, client):
        await _make_gateway(client)
        r = await client.post("/api/tools/gw1", json={
            "name": "send_email", "endpoint": "http://x/e", "method": "POST"})
        await client.request("DELETE", f"/api/tools/{r.json()['id']}")
        entry = [e for e in await _audit_entries(client, "tool") if e["action"] == "delete"][0]
        assert entry["details"]["name"] == "send_email"

    async def test_mcp_server_audited(self, client, admin_headers):
        await _make_gateway(client)
        r = await client.post(
            "/api/mcp-servers/gw1",
            headers=admin_headers,
            json={
                "name": "github",
                "mode": "remote",
                "url": "http://mcp:9000",
            },
        )
        await client.request(
            "DELETE",
            f"/api/mcp-servers/{r.json()['id']}",
            headers=admin_headers,
        )
        actions = [e["action"] for e in await _audit_entries(client, "mcp_server")]
        assert actions == ["create", "delete"]

    async def test_quota_create_and_delete_audited(self, client):
        r = await client.post("/api/quotas", json={
            "name": "q", "scope": "gateway", "scope_id": "gw1", "rate_limit_rpm": 60})
        await client.request("DELETE", f"/api/quotas/{r.json()['id']}")
        entries = await _audit_entries(client, "quota")
        assert [e["action"] for e in entries] == ["create", "delete"]
        assert entries[0]["details"]["rate_limit_rpm"] == 60

    async def test_model_update_records_only_the_change(self, client):
        base = {"name": "m1", "input_cost_per_1k": 0.001, "output_cost_per_1k": 0.002}
        await client.post("/api/models", json=base)
        await client.put("/api/models/m1", json={**base, "input_cost_per_1k": 0.009})
        entry = [e for e in await _audit_entries(client, "model") if e["action"] == "update"][0]
        assert entry["details"] == {"input_cost_per_1k": {"from": 0.001, "to": 0.009}}

    async def test_model_delete_audited(self, client):
        await client.post("/api/models", json={"name": "m1"})
        await client.request("DELETE", "/api/models/m1")
        assert "delete" in [e["action"] for e in await _audit_entries(client, "model")]

    async def test_agent_register_then_update(self, client):
        """Re-registering an existing agent is an update — a tool-list change is
        a privilege change, and the trail should distinguish the two."""
        body = {"name": "a1", "framework": "openai", "tools": ["db_query"]}
        await client.post("/api/agents", json=body)
        await client.post("/api/agents", json={**body, "tools": ["db_query", "db_delete"]})
        actions = [e["action"] for e in await _audit_entries(client, "agent")]
        assert actions == ["register", "update"]

    async def test_experiment_lifecycle_audited(self, client):
        await _make_gateway(client)
        await client.post("/api/experiments", json={
            "name": "e1", "model_a": "a", "model_b": "b", "gateway_id": "gw1"})
        await client.patch("/api/experiments/e1/toggle")
        await client.request("DELETE", "/api/experiments/e1")
        actions = [e["action"] for e in await _audit_entries(client, "experiment")]
        assert actions == ["create", "toggle", "delete"]

    async def test_actor_header_is_ignored(self, client):
        await client.post("/api/models", json={"name": "m1"},
                          headers={"X-Actor": "alice@example.com"})
        entry = (await _audit_entries(client, "model"))[0]
        assert entry["actor"] == "system"

    async def test_actor_comes_from_authenticated_principal(
        self, client, monkeypatch, admin_headers
    ):
        monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
        await client.post(
            "/api/models",
            json={"name": "m1"},
            headers={**admin_headers, "X-Actor": "spoofed@example.com"},
        )
        entry = (await _audit_entries(
            client,
            "model",
            headers=admin_headers,
        ))[0]
        assert entry["actor"] == "admin@test.io"

    async def test_chain_stays_valid_across_new_call_sites(self, client):
        """The new entries are hash-chained like the old ones — an audit entry
        written outside a committed transaction would break verification."""
        await _make_gateway(client)
        r = await client.post("/api/tools/gw1", json={
            "name": "t", "endpoint": "http://x", "method": "POST"})
        await client.post("/api/models", json={"name": "m1"})
        await client.post("/api/agents", json={"name": "a1", "framework": "openai"})
        await client.post("/api/quotas", json={"name": "q", "scope_id": "gw1"})
        await client.request("DELETE", f"/api/tools/{r.json()['id']}")

        verify = (await client.get("/api/audit/verify")).json()
        assert verify["valid"] is True, verify
        # gateway create + 5 above = 6 entries minimum.
        assert verify["checked"] >= 6
