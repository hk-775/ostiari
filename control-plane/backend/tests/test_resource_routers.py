"""Tests for core resource routers: gateways, agents, tools, policies,
model_config, mcp_servers."""

import pytest

pytestmark = pytest.mark.anyio


async def _make_gateway(client, gid="gw1"):
    return await client.post("/api/gateways", json={
        "id": gid, "name": f"GW {gid}", "endpoint": "http://localhost:9001", "description": "d",
    })


# ─── Gateways (DB-backed) ────────────────────────────────────────────────────

class TestGateways:
    async def test_crud_lifecycle(self, client):
        assert (await client.get("/api/gateways")).json() == []
        r = await _make_gateway(client)
        assert r.status_code == 200, r.text
        assert r.json()["id"] == "gw1"
        # get one
        r = await client.get("/api/gateways/gw1")
        assert r.status_code == 200
        # patch
        r = await client.patch("/api/gateways/gw1", json={"name": "Renamed"})
        assert r.status_code == 200
        assert r.json()["name"] == "Renamed"
        # delete
        assert (await client.request("DELETE", "/api/gateways/gw1")).status_code == 200
        assert (await client.get("/api/gateways")).json() == []

    async def test_get_missing_404(self, client):
        assert (await client.get("/api/gateways/nope")).status_code == 404

    async def test_patch_missing_404(self, client):
        assert (await client.patch("/api/gateways/nope", json={"name": "x"})).status_code == 404

    async def test_heartbeat_marks_healthy(self, client):
        await _make_gateway(client)
        r = await client.post("/api/gateways/gw1/heartbeat")
        assert r.status_code == 200
        r = await client.get("/api/gateways/gw1")
        assert r.json()["status"] == "healthy"

    async def test_heartbeat_unknown_404(self, client):
        assert (await client.post("/api/gateways/ghost/heartbeat")).status_code == 404

    async def test_health_endpoint(self, client):
        await _make_gateway(client)
        assert (await client.get("/api/gateways/gw1/health")).status_code == 200

    async def test_push_all_no_gateways_ok(self, client):
        assert (await client.post("/api/gateways/push-all")).status_code == 200


# ─── Agents (in-memory) ──────────────────────────────────────────────────────

class TestAgents:
    async def test_register_get_delete(self, client):
        r = await client.post("/api/agents", json={"name": "crm", "framework": "openai"})
        assert r.status_code == 200, r.text
        r = await client.get("/api/agents/crm")
        assert r.status_code == 200
        assert r.json()["framework"] == "openai"
        assert (await client.request("DELETE", "/api/agents/crm")).status_code == 200

    async def test_get_missing_404(self, client):
        assert (await client.get("/api/agents/ghost")).status_code == 404

    async def test_list_returns_registered(self, client):
        await client.post("/api/agents", json={"name": "ops", "framework": "strands"})
        names = [a["name"] for a in (await client.get("/api/agents")).json()]
        assert "ops" in names


# ─── Tools (DB-backed, needs a gateway) ──────────────────────────────────────

class TestTools:
    async def test_add_list_delete(self, client):
        await _make_gateway(client)
        r = await client.post("/api/tools/gw1", json={"name": "search", "endpoint": "http://svc/q"})
        assert r.status_code == 200, r.text
        tool_id = r.json()["id"]
        tools = (await client.get("/api/tools?gateway_id=gw1")).json()
        assert any(t["name"] == "search" for t in tools)
        assert (await client.request("DELETE", f"/api/tools/{tool_id}")).status_code == 200

    async def test_delete_missing_404(self, client):
        assert (await client.request("DELETE", "/api/tools/99999")).status_code == 404


# ─── Policies (DB-backed) ────────────────────────────────────────────────────

class TestPolicies:
    async def test_crud_lifecycle(self, client):
        r = await client.post("/api/policies", json={
            "name": "p1", "description": "d", "content": {"rules": []},
        })
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        assert (await client.get(f"/api/policies/{pid}")).status_code == 200
        r = await client.patch(f"/api/policies/{pid}", json={"description": "updated"})
        assert r.status_code == 200
        assert r.json()["description"] == "updated"
        assert (await client.request("DELETE", f"/api/policies/{pid}")).status_code == 200

    async def test_get_missing_404(self, client):
        assert (await client.get("/api/policies/99999")).status_code == 404

    async def test_push_missing_404(self, client):
        assert (await client.post("/api/policies/99999/push")).status_code == 404


# ─── Model config (in-memory) ────────────────────────────────────────────────

class TestModelConfig:
    async def test_add_update_delete(self, client):
        r = await client.post("/api/models", json={"name": "claude-x", "category": "reasoning"})
        assert r.status_code == 200, r.text
        r = await client.put("/api/models/claude-x", json={"name": "claude-x", "max_tokens": 8192})
        assert r.status_code == 200
        assert r.json()["max_tokens"] == 8192
        assert (await client.request("DELETE", "/api/models/claude-x")).status_code == 200

    async def test_get_missing_404(self, client):
        assert (await client.get("/api/models/ghost")).status_code == 404


# ─── MCP servers (DB-backed, needs a gateway) ────────────────────────────────

class TestMcpServers:
    async def test_add_get_delete(self, client):
        await _make_gateway(client)
        r = await client.post("/api/mcp-servers/gw1",
                              json={"name": "fs", "mode": "embedded", "module": "fs_server"})
        assert r.status_code == 200, r.text
        mcp_id = r.json()["id"]
        assert (await client.get(f"/api/mcp-servers/{mcp_id}")).status_code == 200
        assert (await client.request("DELETE", f"/api/mcp-servers/{mcp_id}")).status_code == 200

    async def test_embedded_requires_package_or_module_400(self, client):
        await _make_gateway(client)
        r = await client.post("/api/mcp-servers/gw1", json={"name": "fs", "mode": "embedded"})
        assert r.status_code == 400

    async def test_get_missing_404(self, client):
        assert (await client.get("/api/mcp-servers/99999")).status_code == 404


class TestGatewayMode:
    """Control-plane-configurable shadow/enforce mode per gateway."""

    async def test_default_mode_is_enforce(self, client):
        await _make_gateway(client, "gwm")
        r = await client.get("/api/gateways/gwm")
        assert r.json()["mode"] == "enforce"

    async def test_set_shadow_mode_persists(self, client):
        await _make_gateway(client, "gwm")
        # push will fail (no live gateway at the endpoint) but mode must persist
        r = await client.put("/api/gateways/gwm/mode", json={"mode": "shadow"})
        assert r.status_code == 200, r.text
        assert r.json()["mode"] == "shadow"
        # re-read confirms persistence
        assert (await client.get("/api/gateways/gwm")).json()["mode"] == "shadow"
        # and it shows in the list view
        listed = {g["id"]: g["mode"] for g in (await client.get("/api/gateways")).json()}
        assert listed["gwm"] == "shadow"

    async def test_invalid_mode_400(self, client):
        await _make_gateway(client, "gwm")
        r = await client.put("/api/gateways/gwm/mode", json={"mode": "bogus"})
        assert r.status_code == 400

    async def test_mode_unknown_gateway_404(self, client):
        r = await client.put("/api/gateways/ghost/mode", json={"mode": "shadow"})
        assert r.status_code == 404
