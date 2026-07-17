"""Tests for the token broker pilot: pool draw-down, depletion, reconciliation."""

import pytest
from control_plane import broker_pilot

pytestmark = pytest.mark.anyio


# ─── Provider mapping + collector (unit) ─────────────────────────────────────

class TestProviderMapping:
    def test_known(self):
        assert broker_pilot.provider_for("claude-sonnet-4-6") == "anthropic"
        assert broker_pilot.provider_for("gpt-4o") == "openai"
        assert broker_pilot.provider_for("gemini-2.5-flash") == "google"
        assert broker_pilot.provider_for("nova-lite") == "bedrock"

    def test_unknown_falls_back(self):
        assert broker_pilot.provider_for("mystery-model") == "other"
        assert broker_pilot.provider_for("") == "other"


class TestSimulatedCollector:
    async def test_collect_records_intent(self):
        c = broker_pilot.SimulatedCollector()
        r = await c.collect(customer="acme", amount_usd=1.0, model="gpt-4o")
        assert r["collected"] is True
        assert r["ref"].startswith("sim-bill-acme")
        assert r["mode"] == "simulated"


# ─── Pool inventory + draw-down (integration) ────────────────────────────────

@pytest.mark.usefixtures("app_and_db")
class TestPools:
    async def test_fund_and_list(self, client):
        r = await client.post("/api/token-broker/pilot/pools/fund", json={
            "provider": "anthropic", "tokens": 1_000_000, "cost_usd": 2.25,
            "low_threshold_tokens": 100_000,
        })
        assert r.status_code == 200, r.text
        p = r.json()
        assert p["remaining_tokens"] == 1_000_000
        assert p["remaining_pct"] == 100.0
        assert p["status"] == "active"
        pools = (await client.get("/api/token-broker/pilot/pools")).json()
        assert len(pools) == 1

    async def test_fund_accumulates(self, client):
        await client.post("/api/token-broker/pilot/pools/fund", json={"provider": "openai", "tokens": 500, "cost_usd": 1.0})
        r = await client.post("/api/token-broker/pilot/pools/fund", json={"provider": "openai", "tokens": 500, "cost_usd": 1.0})
        assert r.json()["purchased_tokens"] == 1000

    async def test_drawdown_decrements_and_depletes(self, client, app_and_db):
        await client.post("/api/token-broker/pilot/pools/fund", json={
            "provider": "anthropic", "tokens": 1000, "cost_usd": 0.01, "low_threshold_tokens": 100,
        })
        # Record usage that draws the pool down past the threshold.
        from control_plane.database import async_session
        from control_plane.routers.broker_pilot import draw_down
        async with async_session() as db:
            await draw_down(db, model="claude-haiku", tokens=950, our_cost_usd=0.009)
            await db.commit()
        pools = (await client.get("/api/token-broker/pilot/pools")).json()
        p = next(x for x in pools if x["provider"] == "anthropic")
        assert p["consumed_tokens"] == 950
        assert p["remaining_tokens"] == 50
        assert p["status"] == "depleted"      # 50 <= threshold 100

    async def test_fund_reactivates_depleted(self, client):
        await client.post("/api/token-broker/pilot/pools/fund", json={
            "provider": "openai", "tokens": 100, "cost_usd": 0.01, "low_threshold_tokens": 50,
        })
        from control_plane.database import async_session
        from control_plane.routers.broker_pilot import draw_down
        async with async_session() as db:
            await draw_down(db, model="gpt-4o", tokens=80, our_cost_usd=0.008)
            await db.commit()
        assert next(x for x in (await client.get("/api/token-broker/pilot/pools")).json()
                    if x["provider"] == "openai")["status"] == "depleted"
        # Top up above threshold → reactivates.
        r = await client.post("/api/token-broker/pilot/pools/fund", json={"provider": "openai", "tokens": 1000, "cost_usd": 0.1})
        assert r.json()["status"] == "active"

    async def test_drawdown_noop_without_pool(self, app_and_db):
        from control_plane.database import async_session
        from control_plane.routers.broker_pilot import draw_down
        async with async_session() as db:
            await draw_down(db, model="claude-haiku", tokens=100, our_cost_usd=0.1)  # no pool → no error
            await db.commit()


# ─── Reconciliation (integration) ────────────────────────────────────────────

@pytest.mark.usefixtures("app_and_db")
class TestReconciliation:
    async def _seed_usage(self):
        from control_plane.database import async_session
        from control_plane.models.database import UsageRecord
        async with async_session() as db:
            db.add_all([
                UsageRecord(gateway_id="g", agent_id="a", model="claude-haiku", total_tokens=100, cost_usd=1.00),
                UsageRecord(gateway_id="g", agent_id="a", model="gpt-4o", total_tokens=100, cost_usd=2.00),
            ])
            await db.commit()

    async def test_reconcile_computes_drift(self, client):
        await self._seed_usage()
        # Provider invoiced $1.20 for anthropic; we computed $1.00 → drift +$0.20.
        r = await client.post("/api/token-broker/pilot/reconcile", json={
            "provider": "anthropic", "period_days": 30, "invoiced_cost_usd": 1.20,
        })
        d = r.json()
        assert d["computed_cost_usd"] == pytest.approx(1.00)   # only the claude record
        assert d["invoiced_cost_usd"] == pytest.approx(1.20)
        assert d["drift_usd"] == pytest.approx(0.20)
        assert d["drift_pct"] == pytest.approx(20.0)

    async def test_reconciliation_history(self, client):
        await self._seed_usage()
        await client.post("/api/token-broker/pilot/reconcile", json={
            "provider": "openai", "period_days": 30, "invoiced_cost_usd": 2.0,
        })
        rows = (await client.get("/api/token-broker/pilot/reconciliations")).json()
        assert len(rows) == 1
        assert rows[0]["provider"] == "openai"


class TestCollectorEndpoint:
    @pytest.mark.usefixtures("app_and_db")
    async def test_collector_mode(self, client):
        assert (await client.get("/api/token-broker/pilot/collector")).json()["mode"] == "simulated"
