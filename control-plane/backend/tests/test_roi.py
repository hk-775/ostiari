"""Tests for ROI / savings: cost model, damage-prevented computation, router."""

import pytest
from control_plane import roi

pytestmark = pytest.mark.anyio


# ─── Logic module (unit) ─────────────────────────────────────────────────────

class TestComputeRoi:
    def test_empty(self):
        r = roi.compute_roi([])
        assert r.blocked_count == 0 and r.total_prevented_usd == 0.0

    def test_ignores_non_blocked(self):
        traces = [{"action": "db_delete", "tier": "allow", "score": 90}]
        assert roi.compute_roi(traces).blocked_count == 0

    def test_counts_block_tier(self):
        traces = [{"action": "db_delete", "tier": "block", "score": 100}]
        r = roi.compute_roi(traces, weight_by_score=False)
        assert r.blocked_count == 1
        assert r.actions[0].unit_cost == 500_000.0
        assert r.actions[0].prevented_usd == 500_000.0

    def test_counts_would_block_shadow(self):
        traces = [{"action": "db_delete", "tier": "allow", "would_block": True, "score": 100}]
        assert roi.compute_roi(traces, weight_by_score=False).blocked_count == 1

    def test_risk_weighting(self):
        traces = [{"action": "db_delete", "tier": "block", "score": 50}]
        r = roi.compute_roi(traces, weight_by_score=True)
        assert r.total_prevented_usd == pytest.approx(250_000.0)  # 500k * 0.5

    def test_fnmatch_first_match_wins(self):
        # db_delete matches its exact pattern (500k) before *delete* (120k)
        traces = [{"action": "db_delete", "tier": "block", "score": 100}]
        assert roi.compute_roi(traces, weight_by_score=False).actions[0].unit_cost == 500_000.0

    def test_fallback_cost(self):
        traces = [{"action": "some_unknown_tool", "tier": "block", "score": 100}]
        r = roi.compute_roi(traces, weight_by_score=False, fallback_cost=9_999.0)
        assert r.actions[0].unit_cost == 9_999.0

    def test_custom_cost_model(self):
        traces = [{"action": "send_email", "tier": "block", "score": 100}]
        r = roi.compute_roi(traces, cost_model=[("send_email", 42.0)], weight_by_score=False)
        assert r.actions[0].unit_cost == 42.0

    def test_grouping_and_sort(self):
        traces = [
            {"action": "send_email", "tier": "block", "score": 100},
            {"action": "send_email", "tier": "block", "score": 100},
            {"action": "db_delete", "tier": "block", "score": 100},
        ]
        r = roi.compute_roi(traces, weight_by_score=False)
        assert r.distinct_actions == 2
        # db_delete (500k) sorts above send_email (2x5k)
        assert r.actions[0].action == "db_delete"
        assert next(a for a in r.actions if a.action == "send_email").count == 2


# ─── Router (integration) ────────────────────────────────────────────────────

@pytest.fixture
def seeded(app_and_db):
    """Seed the trace buffer with blocks and reset the cost model.

    Depends on app_and_db so it runs *after* conftest's _reset_in_memory_state
    (which clears _recent_traces), not before.
    """
    from control_plane.routers import roi as roi_router
    from control_plane.routers.traces import _recent_traces
    roi_router._cost_model.update({"entries": None, "fallback": roi.DEFAULT_FALLBACK_COST})
    _recent_traces.clear()
    _recent_traces.extend([
        {"action": "db_delete", "tier": "block", "score": 95},
        {"action": "send_email", "tier": "block", "score": 60},
        {"action": "web_search", "tier": "allow", "score": 10},
    ])
    yield
    _recent_traces.clear()
    roi_router._cost_model.update({"entries": None, "fallback": roi.DEFAULT_FALLBACK_COST})


@pytest.mark.usefixtures("seeded")
class TestRoiRouter:
    async def test_report(self, client):
        r = await client.get("/api/roi/report")
        assert r.status_code == 200
        d = r.json()
        assert d["blocked_count"] == 2      # db_delete + send_email, not web_search
        assert d["distinct_actions"] == 2
        assert d["total_prevented_usd"] > 0

    async def test_report_unweighted_higher(self, client):
        weighted = (await client.get("/api/roi/report?weight_by_score=true")).json()
        flat = (await client.get("/api/roi/report?weight_by_score=false")).json()
        assert flat["total_prevented_usd"] > weighted["total_prevented_usd"]

    async def test_default_cost_model(self, client):
        d = (await client.get("/api/roi/cost-model")).json()
        assert d["customized"] is False
        assert any(e["pattern"] == "db_delete" for e in d["entries"])

    async def test_edit_cost_model_changes_report(self, client):
        before = (await client.get("/api/roi/report")).json()["total_prevented_usd"]
        await client.post("/api/roi/cost-model", json={
            "entries": [{"pattern": "db_delete", "cost": 1.0}, {"pattern": "send_email", "cost": 1.0}],
            "fallback": 1.0,
        })
        d = (await client.get("/api/roi/cost-model")).json()
        assert d["customized"] is True
        after = (await client.get("/api/roi/report")).json()["total_prevented_usd"]
        assert after < before  # slashed costs → much smaller estimate

    async def test_reset_cost_model(self, client):
        await client.post("/api/roi/cost-model", json={"entries": [{"pattern": "x", "cost": 1}], "fallback": 1})
        await client.post("/api/roi/cost-model/reset")
        assert (await client.get("/api/roi/cost-model")).json()["customized"] is False
