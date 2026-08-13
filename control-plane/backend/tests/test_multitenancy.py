"""Multi-tenancy isolation tests (foundational slice).

Scoping is driven by the org claim in the caller's token (get_current_org), so
these tests just mint tokens for different orgs and assert one org can't see or
touch another's data. No OSTIARI_REQUIRE_AUTH needed — the coarse auth gate is
separate from per-org data scoping.

Covers the six core tables scoped in this slice: gateways, tools, policies,
wallets, usage_records, audit_logs. Also asserts single-org back-compat.
"""

from __future__ import annotations

import pytest
from control_plane.auth.service import create_access_token

pytestmark = pytest.mark.anyio


def _org_headers(org: str, role: str = "admin") -> dict[str, str]:
    tok = create_access_token(user_id=1, email=f"{org}@test.io", role=role, org=org)
    return {"Authorization": f"Bearer {tok}"}


ORG_A = _org_headers("org-a")
ORG_B = _org_headers("org-b")


class TestBackCompat:
    async def test_no_token_uses_default_org_and_still_works(self, client):
        """No auth → everything lands in the default org, visible as before."""
        r = await client.post(
            "/api/gateways",
            json={"id": "gw-default", "name": "GW", "endpoint": "http://x:8421", "description": ""},
        )
        assert r.status_code == 200
        listed = (await client.get("/api/gateways")).json()
        assert any(g["id"] == "gw-default" for g in listed)


class TestGatewaysIsolation:
    async def test_orgs_cannot_see_each_others_gateways(self, client):
        await client.post("/api/gateways", headers=ORG_A,
                          json={"id": "a1", "name": "A1", "endpoint": "http://a:8421", "description": ""})
        await client.post("/api/gateways", headers=ORG_B,
                          json={"id": "b1", "name": "B1", "endpoint": "http://b:8421", "description": ""})

        a_ids = {g["id"] for g in (await client.get("/api/gateways", headers=ORG_A)).json()}
        b_ids = {g["id"] for g in (await client.get("/api/gateways", headers=ORG_B)).json()}
        assert a_ids == {"a1"}
        assert b_ids == {"b1"}

    async def test_cross_org_get_is_404(self, client):
        await client.post("/api/gateways", headers=ORG_A,
                          json={"id": "a-secret", "name": "A", "endpoint": "http://a:8421", "description": ""})
        assert (await client.get("/api/gateways/a-secret", headers=ORG_B)).status_code == 404
        assert (await client.get("/api/gateways/a-secret", headers=ORG_A)).status_code == 200

    async def test_cross_org_delete_is_404(self, client):
        await client.post("/api/gateways", headers=ORG_A,
                          json={"id": "a-del", "name": "A", "endpoint": "http://a:8421", "description": ""})
        assert (await client.delete("/api/gateways/a-del", headers=ORG_B)).status_code == 404
        # Still there for the owner.
        assert (await client.get("/api/gateways/a-del", headers=ORG_A)).status_code == 200


class TestToolsIsolation:
    async def test_tool_added_to_one_org_invisible_to_other(self, client):
        await client.post("/api/gateways", headers=ORG_A,
                          json={"id": "a-gw", "name": "A", "endpoint": "http://a:8421", "description": ""})
        r = await client.post("/api/tools/a-gw", headers=ORG_A,
                              json={"name": "send_email", "endpoint": "http://x/e", "method": "POST"})
        assert r.status_code in (200, 201)
        # Org B can't even see org A's gateway, so listing its tools is empty/404-free.
        b_tools = (await client.get("/api/tools", headers=ORG_B)).json()
        assert all(t.get("gateway_id") != "a-gw" for t in b_tools)

    async def test_cannot_add_tool_to_other_orgs_gateway(self, client):
        await client.post("/api/gateways", headers=ORG_A,
                          json={"id": "a-gw2", "name": "A", "endpoint": "http://a:8421", "description": ""})
        # Org B tries to attach a tool to org A's gateway → 404 (gateway not in B's org).
        r = await client.post("/api/tools/a-gw2", headers=ORG_B,
                              json={"name": "evil", "endpoint": "http://x/e", "method": "POST"})
        assert r.status_code == 404


class TestPoliciesIsolation:
    async def test_policies_isolated(self, client):
        # Distinct names (Policy.name is globally unique this slice).
        await client.post("/api/policies", headers=ORG_A,
                          json={"name": "pol-a", "content": {"block": ["*.delete"]}})
        await client.post("/api/policies", headers=ORG_B,
                          json={"name": "pol-b", "content": {"block": ["*.drop"]}})
        a_names = {p["name"] for p in (await client.get("/api/policies", headers=ORG_A)).json()}
        b_names = {p["name"] for p in (await client.get("/api/policies", headers=ORG_B)).json()}
        assert "pol-a" in a_names and "pol-b" not in a_names
        assert "pol-b" in b_names and "pol-a" not in b_names

    async def test_policy_cannot_target_another_orgs_gateway(self, client):
        await client.post(
            "/api/gateways",
            headers=ORG_A,
            json={
                "id": "a-policy-gw",
                "name": "A",
                "endpoint": "http://a:8421",
                "description": "",
            },
        )
        response = await client.post(
            "/api/policies",
            headers=ORG_B,
            json={
                "name": "cross-org-policy",
                "gateway_id": "a-policy-gw",
                "content": {"block": ["*"]},
            },
        )

        assert response.status_code == 404


class TestWalletsIsolation:
    async def test_wallets_isolated(self, client):
        await client.post("/api/payments/wallets", headers=ORG_A,
                          json={"agent_id": "agent-a", "balance_usdc": 5.0, "address": ""})
        await client.post("/api/payments/wallets", headers=ORG_B,
                          json={"agent_id": "agent-b", "balance_usdc": 3.0, "address": ""})
        a_ids = {w["agent_id"] for w in (await client.get("/api/payments/wallets", headers=ORG_A)).json()}
        b_ids = {w["agent_id"] for w in (await client.get("/api/payments/wallets", headers=ORG_B)).json()}
        assert a_ids == {"agent-a"}
        assert b_ids == {"agent-b"}

    async def test_cross_org_wallet_patch_is_404(self, client):
        await client.post("/api/payments/wallets", headers=ORG_A,
                          json={"agent_id": "wallet-a", "balance_usdc": 5.0, "address": ""})
        r = await client.patch("/api/payments/wallets/wallet-a", headers=ORG_B,
                               json={"status": "paused"})
        assert r.status_code == 404


class TestAuditIsolation:
    async def test_audit_rows_scoped_but_chain_global(self, client):
        # A config change under each org writes an org-stamped audit row.
        await client.post("/api/gateways", headers=ORG_A,
                          json={"id": "aud-a", "name": "A", "endpoint": "http://a:8421", "description": ""})
        await client.post("/api/gateways", headers=ORG_B,
                          json={"id": "aud-b", "name": "B", "endpoint": "http://b:8421", "description": ""})
        a_rows = (await client.get("/api/audit", headers=ORG_A)).json()
        b_rows = (await client.get("/api/audit", headers=ORG_B)).json()
        a_resources = {r["resource_id"] for r in a_rows}
        b_resources = {r["resource_id"] for r in b_rows}
        assert "aud-a" in a_resources and "aud-a" not in b_resources
        assert "aud-b" in b_resources and "aud-b" not in a_resources
        # The tamper-evident chain spans all orgs and stays valid.
        v = (await client.get("/api/audit/verify", headers=ORG_A)).json()
        assert v["valid"] is True


class TestMcpServersIsolation:
    async def _make_gateway(self, client, headers, gid):
        await client.post("/api/gateways", headers=headers,
                          json={"id": gid, "name": gid, "endpoint": "http://x:8421", "description": ""})

    async def test_mcp_servers_scoped(self, client):
        await self._make_gateway(client, ORG_A, "mcp-gw-a")
        r = await client.post("/api/mcp-servers/mcp-gw-a", headers=ORG_A,
                              json={"name": "fs", "mode": "remote", "url": "http://m:3000"})
        assert r.status_code in (200, 201)
        # Org B can't see org A's MCP servers.
        b_list = (await client.get("/api/mcp-servers", headers=ORG_B)).json()
        assert all(m["gateway_id"] != "mcp-gw-a" for m in b_list)
        # Org A does.
        a_list = (await client.get("/api/mcp-servers", headers=ORG_A)).json()
        assert any(m["gateway_id"] == "mcp-gw-a" for m in a_list)

    async def test_cannot_add_mcp_to_other_orgs_gateway(self, client):
        await self._make_gateway(client, ORG_A, "mcp-gw-a2")
        r = await client.post("/api/mcp-servers/mcp-gw-a2", headers=ORG_B,
                              json={"name": "evil", "mode": "remote", "url": "http://m:3000"})
        assert r.status_code == 404


class TestPaymentLedgerIsolation:
    """The payment ledger/summary previously aggregated across ALL orgs."""

    async def test_ledger_and_summary_scoped(self, client):
        # Ingest a settled charge tagged (via gateway) to each org.
        await client.post("/api/gateways", headers=ORG_A,
                          json={"id": "pay-gw-a", "name": "A", "endpoint": "http://a:8421", "description": ""})
        await client.post("/api/gateways", headers=ORG_B,
                          json={"id": "pay-gw-b", "name": "B", "endpoint": "http://b:8421", "description": ""})
        await client.post("/api/payments/ingest", json={
            "agent_id": "x", "gateway_id": "pay-gw-a", "action": "premium",
            "amount_usdc": 0.10, "settled": True, "mode": "simulated", "source": "tool_402",
        })
        await client.post("/api/payments/ingest", json={
            "agent_id": "y", "gateway_id": "pay-gw-b", "action": "premium",
            "amount_usdc": 0.99, "settled": True, "mode": "simulated", "source": "tool_402",
        })
        a_ledger = (await client.get("/api/payments/ledger", headers=ORG_A)).json()
        b_ledger = (await client.get("/api/payments/ledger", headers=ORG_B)).json()
        assert {r["gateway_id"] for r in a_ledger} == {"pay-gw-a"}
        assert {r["gateway_id"] for r in b_ledger} == {"pay-gw-b"}
        # Summaries don't leak each other's totals.
        a_sum = (await client.get("/api/payments/summary", headers=ORG_A)).json()
        b_sum = (await client.get("/api/payments/summary", headers=ORG_B)).json()
        assert a_sum["total_settled_usdc"] == pytest.approx(0.10)
        assert b_sum["total_settled_usdc"] == pytest.approx(0.99)


class TestUsageRecordIsolation:
    """Spend was the worst leak: every gateway's usage landed in the "default"
    org (the column default) and every reader aggregated across all orgs, so any
    caller's /api/costs/summary listed every tenant's gateway names, agent names
    and dollar totals — and each real tenant's own ledger read empty.

    Ingest is tokenless (a gateway reporting its own usage), so the org has to be
    derived from the gateway row, not from the caller or the payload.
    """

    async def _seed(self, client):
        for hdr, gid in ((ORG_A, "cost-gw-a"), (ORG_B, "cost-gw-b")):
            await client.post("/api/gateways", headers=hdr,
                              json={"id": gid, "name": gid, "endpoint": "http://x:8421", "description": ""})
        # No auth header on /record — this is how the gateway's cost reporter posts.
        await client.post("/api/costs/record", json={
            "gateway_id": "cost-gw-a", "agent_id": "agent-a", "model": "claude-sonnet-4-6",
            "input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100,
            "cost_usd": 0.10, "action": "chat",
        })
        await client.post("/api/costs/record", json={
            "gateway_id": "cost-gw-b", "agent_id": "agent-b", "model": "gpt-4o",
            "input_tokens": 2000, "output_tokens": 200, "total_tokens": 2200,
            "cost_usd": 0.99, "action": "chat",
        })

    async def test_ingest_derives_org_from_the_gateway(self, client):
        await self._seed(client)
        a = (await client.get("/api/costs/records", headers=ORG_A)).json()
        b = (await client.get("/api/costs/records", headers=ORG_B)).json()
        assert {r["gateway_id"] for r in a} == {"cost-gw-a"}
        assert {r["gateway_id"] for r in b} == {"cost-gw-b"}
        # Nothing fell through to the default org.
        assert (await client.get("/api/costs/records")).json() == []

    async def test_summary_does_not_leak_spend_or_names(self, client):
        await self._seed(client)
        a = (await client.get("/api/costs/summary", headers=ORG_A)).json()
        b = (await client.get("/api/costs/summary", headers=ORG_B)).json()
        assert a["total_cost_usd"] == pytest.approx(0.10)
        assert b["total_cost_usd"] == pytest.approx(0.99)
        # The breakdowns are the leak surface — they name the other tenant's
        # gateways, agents and models, not just inflate a total.
        assert {g["gateway_id"] for g in a["by_gateway"]} == {"cost-gw-a"}
        assert {g["agent_id"] for g in a["by_agent"]} == {"agent-a"}
        assert {m["model"] for m in b["by_model"]} == {"gpt-4o"}
        assert a["total_tokens"] == 1100 and b["total_tokens"] == 2200

    async def test_batch_ingest_is_scoped_per_gateway(self, client):
        await self._seed(client)
        r = await client.post("/api/costs/record/batch", json=[
            {"gateway_id": "cost-gw-a", "agent_id": "agent-a", "model": "gpt-4o-mini",
             "input_tokens": 10, "output_tokens": 1, "total_tokens": 11, "cost_usd": 0.01, "action": "chat"},
            {"gateway_id": "cost-gw-b", "agent_id": "agent-b", "model": "gpt-4o-mini",
             "input_tokens": 10, "output_tokens": 1, "total_tokens": 11, "cost_usd": 0.02, "action": "chat"},
        ])
        assert r.json()["recorded"] == 2
        # One batch, two gateways, two orgs — the per-batch org cache must not
        # smear the first gateway's org across the rest of the batch.
        a = (await client.get("/api/costs/summary", headers=ORG_A)).json()
        b = (await client.get("/api/costs/summary", headers=ORG_B)).json()
        assert a["total_cost_usd"] == pytest.approx(0.11)
        assert b["total_cost_usd"] == pytest.approx(1.01)

    async def test_unregistered_gateway_still_recorded_under_default(self, client):
        """Demo posture: usage from a gateway we've never seen is still kept
        (filed under the default org) rather than silently dropped."""
        await client.post("/api/costs/record", json={
            "gateway_id": "never-registered", "agent_id": "z", "model": "gpt-4o",
            "input_tokens": 1, "output_tokens": 1, "total_tokens": 2, "cost_usd": 0.05, "action": "chat",
        })
        default_records = (await client.get("/api/costs/records")).json()
        assert {r["gateway_id"] for r in default_records} == {"never-registered"}
        assert (await client.get("/api/costs/records", headers=ORG_A)).json() == []

    async def test_token_broker_report_scoped(self, client):
        """The broker report reads the CALLER's margin config; aggregating every
        org's usage against it reported one tenant's economics over another's spend."""
        await self._seed(client)
        a = (await client.get("/api/token-broker/report", headers=ORG_A)).json()
        b = (await client.get("/api/token-broker/report", headers=ORG_B)).json()
        assert a["total_tokens"] == 1100
        assert b["total_tokens"] == 2200
        assert {m["model"] for m in a["models"]} == {"claude-sonnet-4-6"}


class TestTraceIngestIsolation:
    """Traces arrive with no user token, so the owning org must come from the
    reporting gateway's row — never from the event body. The recent-trace buffer
    is read back by /recent, the WebSocket fan-out, compliance, ROI, trust
    scoring and discovery, so a misfiled trace leaks into all of them at once."""

    async def _seed_gateways(self, client):
        await client.post("/api/gateways", headers=ORG_A,
                          json={"id": "trace-gw-a", "name": "A", "endpoint": "http://a:8421",
                                "description": ""})
        await client.post("/api/gateways", headers=ORG_B,
                          json={"id": "trace-gw-b", "name": "B", "endpoint": "http://b:8421",
                                "description": ""})

    @staticmethod
    def _event(**kw):
        base = {"action": "db_query", "tier": "allow", "score": 0, "duration_ms": 1.0,
                "agent_id": "bot", "framework": "curl", "timestamp": 1753459200.0}
        base.update(kw)
        return base

    async def test_ingest_derives_org_from_the_reporting_gateway(self, client):
        await self._seed_gateways(client)
        await client.post("/api/traces/ingest",
                          json=self._event(sidecar_id="trace-gw-a", trace_id="t-a"))
        await client.post("/api/traces/ingest",
                          json=self._event(sidecar_id="trace-gw-b", trace_id="t-b"))

        a = {t["trace_id"] for t in (await client.get("/api/traces/recent", headers=ORG_A)).json()["traces"]}
        b = {t["trace_id"] for t in (await client.get("/api/traces/recent", headers=ORG_B)).json()["traces"]}
        assert a == {"t-a"}
        assert b == {"t-b"}
        # Neither fell through to the default org.
        assert (await client.get("/api/traces/recent")).json()["traces"] == []

    async def test_payload_cannot_choose_its_own_org(self, client):
        """The core defect: trusting the body's org_id let any ingest caller
        plant a trace in an arbitrary tenant's buffer. The gateway's row wins."""
        await self._seed_gateways(client)
        await client.post("/api/traces/ingest", json=self._event(
            sidecar_id="trace-gw-a", trace_id="forged", org_id="org-b",
            action="exfiltrate_all", params={"note": "planted"}))

        b = (await client.get("/api/traces/recent", headers=ORG_B)).json()["traces"]
        assert b == [], "a forged org_id reached another tenant's trace buffer"
        a = {t["trace_id"] for t in (await client.get("/api/traces/recent", headers=ORG_A)).json()["traces"]}
        assert a == {"forged"}

    async def test_forged_org_id_is_not_stored_on_the_event(self, client):
        """Even filed correctly, a surviving org_id in the body would mislead any
        consumer that reads the field instead of the buffer it came from."""
        await self._seed_gateways(client)
        await client.post("/api/traces/ingest", json=self._event(
            sidecar_id="trace-gw-a", trace_id="stamped", org_id="org-b"))
        stored = (await client.get("/api/traces/recent", headers=ORG_A)).json()["traces"][0]
        assert stored["org_id"] == "org-a"

    async def test_gateway_id_is_populated_for_consumers(self, client):
        """The reporter sends sidecar_id; the trace viewer's Gateway column and
        the delegation report read gateway_id. Without the alias it was blank."""
        await self._seed_gateways(client)
        await client.post("/api/traces/ingest",
                          json=self._event(sidecar_id="trace-gw-a", trace_id="t-gw"))
        stored = (await client.get("/api/traces/recent", headers=ORG_A)).json()["traces"][0]
        assert stored["gateway_id"] == "trace-gw-a"

    async def test_explicit_gateway_id_is_not_overwritten(self, client):
        await self._seed_gateways(client)
        await client.post("/api/traces/ingest", json=self._event(
            sidecar_id="trace-gw-a", gateway_id="trace-gw-a", trace_id="t-both"))
        stored = (await client.get("/api/traces/recent", headers=ORG_A)).json()["traces"][0]
        assert stored["gateway_id"] == "trace-gw-a"

    async def test_unregistered_gateway_files_under_default(self, client):
        """Demo posture: a trace from a gateway we've never seen is still kept
        (default org) rather than dropped — matching costs/payments ingest."""
        await client.post("/api/traces/ingest",
                          json=self._event(sidecar_id="never-registered", trace_id="t-unknown"))
        assert {t["trace_id"] for t in (await client.get("/api/traces/recent")).json()["traces"]} == {"t-unknown"}
        assert (await client.get("/api/traces/recent", headers=ORG_A)).json()["traces"] == []

    async def test_downstream_reports_do_not_leak_across_orgs(self, client):
        """Trust/compliance/ROI all read the same buffer, so a scoping miss shows
        up as one tenant's agents appearing in another's governance reports."""
        await self._seed_gateways(client)
        await client.post("/api/traces/ingest", json=self._event(
            sidecar_id="trace-gw-a", trace_id="t-a", agent_id="agent-a"))
        await client.post("/api/traces/ingest", json=self._event(
            sidecar_id="trace-gw-b", trace_id="t-b", agent_id="agent-b"))

        a_trust = (
            await client.get(
                "/api/trust/scores?gateway_id=crm-agent",
                headers=ORG_A,
            )
        ).json()
        ids = {row.get("agent_id") for row in (a_trust if isinstance(a_trust, list)
                                              else a_trust.get("agents", []))}
        assert "agent-b" not in ids
