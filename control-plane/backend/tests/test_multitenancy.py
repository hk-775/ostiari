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
