"""Tests for behavior-derived trust scoring + API (shadow-first, opt-in enforce)."""

import pytest

from control_plane import trust

pytestmark = pytest.mark.anyio


class TestScoringEngine:
    def test_no_history_is_baseline(self):
        assert trust.derive_score([]) == trust.BASELINE

    def test_clean_agent_scores_high(self):
        assert trust.derive_score([{"tier": "allow", "score": 10}] * 10) >= 90

    def test_bad_agent_scores_low(self):
        assert trust.derive_score([{"tier": "block", "score": 95}] * 10) <= 20

    def test_thin_sample_stays_near_baseline(self):
        # a single block must not tank trust to 0 (confidence blend)
        s = trust.derive_score([{"tier": "block", "score": 100}])
        assert 35 <= s <= 55

    def test_score_agents_shows_delta(self):
        traces = ([{"agent_id": "good", "tier": "allow", "score": 10}] * 8 +
                  [{"agent_id": "risky", "tier": "block", "score": 95}] * 8)
        rows = {r["agent_id"]: r for r in trust.score_agents(traces, configured={"good": 90, "risky": 90})}
        assert rows["risky"]["derived_score"] <= 20
        assert rows["risky"]["delta"] < -50   # configured 90, derived low
        assert rows["good"]["derived_score"] >= 90


class TestTrustAPI:
    async def _seed(self, client, agent, tier, score, n):
        for _ in range(n):
            await client.post("/api/traces/ingest", json={
                "agent_id": agent, "tier": tier, "score": score, "action": "x",
            })

    async def test_scores_shadow_view(self, client):
        await self._seed(client, "riskybot", "block", 95, 8)
        await self._seed(client, "goodbot", "allow", 8, 8)
        r = await client.get("/api/trust/scores")
        assert r.status_code == 200
        body = r.json()
        assert body["enforced"] is False   # shadow by default
        agents = {a["agent_id"]: a for a in body["agents"]}
        assert agents["riskybot"]["derived_score"] < agents["goodbot"]["derived_score"]

    async def test_apply_requires_activity(self, client):
        # no gateway registered / no activity -> 400 or 404, never 500
        r = await client.post("/api/trust/apply?gateway_id=ghost")
        assert r.status_code in (400, 404)

    async def test_enforce_toggle_state(self, client):
        # disable is always safe and flips the flag off
        r = await client.post("/api/trust/disable?gateway_id=crm-agent")
        assert r.status_code == 200
        assert r.json()["enforced"] is False
