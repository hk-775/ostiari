"""Tests for the control-plane agent-routing policy endpoint."""

import pytest

pytestmark = pytest.mark.anyio


async def _make_gateway(client, gid="gw1"):
    return await client.post("/api/gateways", json={
        "id": gid, "name": f"GW {gid}", "endpoint": "http://localhost:9001", "description": "d",
    })


class TestAgentRouting:
    async def test_set_policy_saves_even_if_push_unreachable(self, client):
        await _make_gateway(client)
        r = await client.post("/api/agent-routing", json={
            "agent_id": "claude-code", "gateway_id": "gw1",
            "strategy": "round_robin", "models": ["claude-sonnet-4-6", "gpt-4o"], "scope": "session",
        })
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["status"] == "saved"
        # gateway endpoint is unreachable in tests -> push fails gracefully, save persists
        assert d["pushed"] is False
        assert d["policy"]["models"] == ["claude-sonnet-4-6", "gpt-4o"]

    async def test_list_and_filter_by_gateway(self, client):
        await _make_gateway(client, "gw1")
        await _make_gateway(client, "gw2")
        await client.post("/api/agent-routing", json={
            "agent_id": "a1", "gateway_id": "gw1", "models": ["x"]})
        await client.post("/api/agent-routing", json={
            "agent_id": "a2", "gateway_id": "gw2", "models": ["y"]})
        all_p = (await client.get("/api/agent-routing")).json()
        assert len(all_p) >= 2
        gw1 = (await client.get("/api/agent-routing/gw1")).json()
        assert {p["agent_id"] for p in gw1} == {"a1"}

    async def test_unknown_gateway_404(self, client):
        r = await client.post("/api/agent-routing", json={
            "agent_id": "a", "gateway_id": "nope", "models": ["x"]})
        assert r.status_code == 404

    async def test_empty_models_400(self, client):
        await _make_gateway(client)
        r = await client.post("/api/agent-routing", json={
            "agent_id": "a", "gateway_id": "gw1", "strategy": "round_robin", "models": []})
        assert r.status_code == 400

    async def test_delete_policy(self, client):
        await _make_gateway(client)
        await client.post("/api/agent-routing", json={
            "agent_id": "a1", "gateway_id": "gw1", "models": ["x"]})
        r = await client.request("DELETE", "/api/agent-routing/gw1/a1")
        assert r.status_code == 200
        assert (await client.get("/api/agent-routing/gw1")).json() == []

    async def test_delete_missing_404(self, client):
        assert (await client.request("DELETE", "/api/agent-routing/gw1/ghost")).status_code == 404
