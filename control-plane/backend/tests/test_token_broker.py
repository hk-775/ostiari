"""Tests for token broker economics: discount/markup math, invariants, router."""

from dataclasses import dataclass

import pytest
from control_plane import token_broker

pytestmark = pytest.mark.anyio


@dataclass
class _Rec:
    model: str
    cost_usd: float
    total_tokens: int = 0


# ─── Logic module (unit) ─────────────────────────────────────────────────────

class TestComputeBroker:
    def test_empty(self):
        r = token_broker.compute_broker([])
        assert r.total_retail_usd == 0.0
        assert r.customer_savings_usd == 0.0
        assert r.margin_usd == 0.0

    def test_basic_math(self):
        # retail $100, 25% discount → our_cost $75, 12% markup → charged $84
        r = token_broker.compute_broker(
            [_Rec("gpt-4o", 100.0, 1000)], bulk_discount=0.25, markup=0.12
        )
        assert r.total_retail_usd == pytest.approx(100.0)
        assert r.total_our_cost_usd == pytest.approx(75.0)
        assert r.total_charged_usd == pytest.approx(84.0)
        assert r.customer_savings_usd == pytest.approx(16.0)   # 100 - 84
        assert r.margin_usd == pytest.approx(9.0)              # 84 - 75

    def test_win_win_invariant(self):
        # With sane inputs, customer pays less than retail AND we profit.
        r = token_broker.compute_broker(
            [_Rec("gpt-4o", 50.0)], bulk_discount=0.25, markup=0.12
        )
        assert r.total_charged_usd < r.total_retail_usd    # customer saves
        assert r.total_charged_usd > r.total_our_cost_usd  # we profit

    def test_savings_pct(self):
        r = token_broker.compute_broker(
            [_Rec("gpt-4o", 100.0)], bulk_discount=0.25, markup=0.12
        )
        assert r.savings_pct == pytest.approx(16.0)

    def test_grouping_by_model(self):
        r = token_broker.compute_broker([
            _Rec("gpt-4o", 60.0, 100),
            _Rec("gpt-4o", 40.0, 100),
            _Rec("claude-haiku", 10.0, 50),
        ])
        assert len(r.models) == 2
        assert r.models[0].model == "gpt-4o"       # sorted by retail desc
        assert r.models[0].calls == 2
        assert r.models[0].tokens == 200

    def test_discount_clamped(self):
        # discount clamped to 0.95 so charged never goes negative/absurd
        r = token_broker.compute_broker([_Rec("x", 100.0)], bulk_discount=5.0, markup=0.0)
        assert r.bulk_discount == 0.95
        assert r.total_our_cost_usd == pytest.approx(5.0)

    def test_high_markup_can_erase_savings(self):
        # If markup eats the discount, charged can exceed retail — surfaced honestly.
        r = token_broker.compute_broker([_Rec("x", 100.0)], bulk_discount=0.10, markup=1.0)
        # our_cost 90, charged 180 > retail 100 → negative savings
        assert r.customer_savings_usd < 0


# ─── Router (integration) ────────────────────────────────────────────────────

@pytest.fixture
def seeded(app_and_db):
    """Seed usage records and reset broker config."""
    from control_plane.routers import token_broker as tb_router
    tb_router._config.update({
        "bulk_discount": token_broker.DEFAULT_BULK_DISCOUNT,
        "markup": token_broker.DEFAULT_MARKUP,
        "_customized": False,
    })
    yield
    tb_router._config.update({
        "bulk_discount": token_broker.DEFAULT_BULK_DISCOUNT,
        "markup": token_broker.DEFAULT_MARKUP,
        "_customized": False,
    })


@pytest.mark.usefixtures("seeded")
class TestBrokerRouter:
    async def _seed_usage(self):
        from control_plane.database import async_session
        from control_plane.models.database import UsageRecord
        async with async_session() as db:
            db.add_all([
                UsageRecord(gateway_id="crm-agent", agent_id="a", model="gpt-4o",
                            total_tokens=1000, cost_usd=1.00),
                UsageRecord(gateway_id="crm-agent", agent_id="b", model="claude-haiku",
                            total_tokens=500, cost_usd=0.20),
            ])
            await db.commit()

    async def test_report(self, client):
        await self._seed_usage()
        d = (await client.get("/api/token-broker/report")).json()
        assert d["total_retail_usd"] == pytest.approx(1.20)
        assert d["customer_savings_usd"] > 0
        assert d["margin_usd"] > 0
        assert len(d["models"]) == 2

    async def test_default_config(self, client):
        d = (await client.get("/api/token-broker/config")).json()
        assert d["customized"] is False
        assert d["bulk_discount"] == token_broker.DEFAULT_BULK_DISCOUNT

    async def test_edit_config_changes_report(self, client):
        await self._seed_usage()
        before = (await client.get("/api/token-broker/report")).json()["customer_savings_usd"]
        await client.post("/api/token-broker/config", json={"bulk_discount": 0.5, "markup": 0.1})
        assert (await client.get("/api/token-broker/config")).json()["customized"] is True
        after = (await client.get("/api/token-broker/report")).json()["customer_savings_usd"]
        assert after > before  # bigger discount → bigger customer savings

    async def test_reset_config(self, client):
        await client.post("/api/token-broker/config", json={"bulk_discount": 0.5, "markup": 0.5})
        await client.post("/api/token-broker/config/reset")
        assert (await client.get("/api/token-broker/config")).json()["customized"] is False
