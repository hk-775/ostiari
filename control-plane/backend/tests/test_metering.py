"""Tests for the metering engine and API."""

import pytest
from control_plane import metering

pytestmark = pytest.mark.anyio


class _Rec:
    def __init__(self, agent_id="a", gateway_id="gw", action="tool", total_tokens=0):
        self.agent_id = agent_id
        self.gateway_id = gateway_id
        self.action = action
        self.total_tokens = total_tokens


class TestTiering:
    def test_tier_for(self):
        assert metering.tier_for(0) == "free"
        assert metering.tier_for(49_999) == "free"
        assert metering.tier_for(50_000) == "pro"
        assert metering.tier_for(500_000) == "enterprise"

    def test_next_tier(self):
        nt = metering.next_tier(10)
        assert nt["tier"] == "pro" and nt["calls_to_next"] == 49_990
        assert metering.next_tier(600_000) is None  # top tier


class TestSummarize:
    def test_counts_and_groups_by_agent(self):
        recs = [_Rec(agent_id="a", total_tokens=10), _Rec(agent_id="a", total_tokens=5),
                _Rec(agent_id="b", total_tokens=20)]
        s = metering.summarize(recs, group_by="agent")
        assert s["total_governed_calls"] == 3
        assert s["total_tokens"] == 35
        assert s["distinct_subjects"] == 2
        top = s["breakdown"][0]
        assert top["key"] == "a" and top["calls"] == 2 and top["tokens"] == 15

    def test_group_by_tool_and_gateway(self):
        recs = [_Rec(action="search"), _Rec(action="search"), _Rec(action="write")]
        s = metering.summarize(recs, group_by="tool")
        assert s["group_by"] == "tool"
        assert s["breakdown"][0]["key"] == "search" and s["breakdown"][0]["calls"] == 2

    def test_csv_export(self):
        s = metering.summarize([_Rec(agent_id="a", total_tokens=3)], group_by="agent")
        csv = metering.to_csv(s)
        assert csv.splitlines()[0] == "agent,governed_calls,tokens,tier"
        assert "a,1,3,free" in csv


class TestMeteringAPI:
    async def _seed(self, client, n, agent="bot", gw="gw1"):
        for _ in range(n):
            await client.post("/api/costs/record", json={
                "gateway_id": gw, "agent_id": agent, "model": "m",
                "total_tokens": 10, "cost_usd": 0.001, "action": "search",
            })

    async def test_summary_empty(self, client):
        r = await client.get("/api/metering/summary")
        assert r.status_code == 200
        assert r.json()["total_governed_calls"] == 0

    async def test_summary_counts_governed_calls(self, client):
        await self._seed(client, 3)
        r = await client.get("/api/metering/summary?group_by=agent")
        b = r.json()
        assert b["total_governed_calls"] == 3
        assert b["overall_tier"] == "free"
        assert b["breakdown"][0]["key"] == "bot" and b["breakdown"][0]["calls"] == 3
        assert b["tiers"][1]["tier"] == "pro"

    async def test_group_by_tool(self, client):
        await self._seed(client, 2)
        r = await client.get("/api/metering/summary?group_by=tool")
        assert r.json()["breakdown"][0]["key"] == "search"

    async def test_csv_export_endpoint(self, client):
        await self._seed(client, 2)
        r = await client.get("/api/metering/export?group_by=agent")
        assert r.status_code == 200
        assert "governed_calls" in r.text
        assert "bot,2,20,free" in r.text
