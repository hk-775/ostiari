"""Tests for the payments router: wallets, funding, limits, ledger, summary,
pricing, and the payment config bundle builder."""

import pytest

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _clear_pricing():
    """The pricing policy is module-global; reset it around each test."""
    from control_plane.routers import payments
    payments._pricing.clear()
    yield
    payments._pricing.clear()


async def _wallet(client, agent_id="research-agent", balance=1.0, **kw):
    return await client.post("/api/payments/wallets", json={
        "agent_id": agent_id, "balance_usdc": balance, **kw,
    })


class TestWallets:
    async def test_create_and_list(self, client):
        assert (await client.get("/api/payments/wallets")).json() == []
        r = await _wallet(client, balance=2.5)
        assert r.status_code == 200, r.text
        assert r.json()["balance_usdc"] == 2.5
        rows = (await client.get("/api/payments/wallets")).json()
        assert len(rows) == 1 and rows[0]["agent_id"] == "research-agent"

    async def test_upsert_updates_existing(self, client):
        await _wallet(client, balance=1.0)
        await _wallet(client, balance=5.0, per_call_limit_usdc=0.01)
        rows = (await client.get("/api/payments/wallets")).json()
        assert len(rows) == 1
        assert rows[0]["balance_usdc"] == 5.0
        assert rows[0]["per_call_limit_usdc"] == 0.01

    async def test_fund_adds_balance(self, client):
        await _wallet(client, balance=1.0)
        r = await client.post("/api/payments/wallets/research-agent/fund",
                              json={"amount_usdc": 0.5})
        assert r.json()["balance_usdc"] == 1.5

    async def test_fund_missing_404(self, client):
        r = await client.post("/api/payments/wallets/ghost/fund", json={"amount_usdc": 1})
        assert r.status_code == 404

    async def test_fund_reactivates_paused(self, client):
        await _wallet(client, balance=0.0)
        await client.patch("/api/payments/wallets/research-agent", json={"status": "paused"})
        r = await client.post("/api/payments/wallets/research-agent/fund",
                              json={"amount_usdc": 1.0})
        assert r.json()["status"] == "active"

    async def test_patch_limits_and_pause(self, client):
        await _wallet(client)
        r = await client.patch("/api/payments/wallets/research-agent", json={
            "daily_limit_usdc": 5.0, "status": "paused",
        })
        assert r.json()["daily_limit_usdc"] == 5.0
        assert r.json()["status"] == "paused"

    async def test_patch_missing_404(self, client):
        assert (await client.patch("/api/payments/wallets/ghost", json={"status": "paused"})).status_code == 404


class TestPricing:
    async def test_default_off(self, client):
        r = await client.get("/api/payments/pricing?gateway_id=crm-agent")
        assert r.json()["mode"] == "off"

    async def test_set_and_get(self, client):
        await client.post("/api/payments/pricing?gateway_id=crm-agent", json={
            "mode": "metered", "default": 0.0, "overrides": {"web_search": 0.005},
        })
        r = await client.get("/api/payments/pricing?gateway_id=crm-agent")
        assert r.json()["mode"] == "metered"
        assert r.json()["overrides"]["web_search"] == 0.005


class TestLedgerAndSummary:
    async def test_empty_ledger_and_summary(self, client):
        assert (await client.get("/api/payments/ledger")).json() == []
        s = (await client.get("/api/payments/summary")).json()
        assert s["settled_count"] == 0
        assert s["total_settled_usdc"] == 0.0
        assert s["by_agent"] == []

    async def test_summary_aggregates(self, client, app_and_db):
        # Insert ledger rows directly via the app's session factory.
        from control_plane.database import async_session
        from control_plane.models.database import PaymentRecord
        async with async_session() as db:
            db.add_all([
                PaymentRecord(agent_id="a", action="x", amount_usdc=0.005, settled=True),
                PaymentRecord(agent_id="a", action="y", amount_usdc=0.005, settled=True),
                PaymentRecord(agent_id="b", action="z", amount_usdc=0.010, settled=True),
                PaymentRecord(agent_id="b", action="w", amount_usdc=0.005, settled=False),
            ])
            await db.commit()
        s = (await client.get("/api/payments/summary")).json()
        assert s["settled_count"] == 3
        assert s["blocked_count"] == 1
        assert s["total_settled_usdc"] == pytest.approx(0.02)
        assert s["fees_captured_usdc"] == pytest.approx(0.02 * 0.03)
        # sorted desc by spend: a=0.010, b=0.010 (order stable) — check membership
        agents = {r["agent_id"]: r for r in s["by_agent"]}
        assert agents["a"]["calls"] == 2
        assert agents["b"]["calls"] == 1

    async def test_ledger_filter_by_agent(self, client):
        from control_plane.database import async_session
        from control_plane.models.database import PaymentRecord
        async with async_session() as db:
            db.add_all([
                PaymentRecord(agent_id="a", action="x", amount_usdc=0.005, settled=True),
                PaymentRecord(agent_id="b", action="y", amount_usdc=0.005, settled=True),
            ])
            await db.commit()
        rows = (await client.get("/api/payments/ledger?agent_id=a")).json()
        assert len(rows) == 1 and rows[0]["agent_id"] == "a"


class TestBuildConfig:
    async def test_bundle_has_pricing_and_wallets(self, client, app_and_db):
        await _wallet(client, agent_id="a", balance=1.0)
        await client.post("/api/payments/pricing?gateway_id=crm-agent", json={
            "mode": "passthrough", "default": 0.0, "overrides": {},
        })
        from control_plane.database import async_session
        from control_plane.routers.payments import build_payment_config
        async with async_session() as db:
            bundle = await build_payment_config(db, "crm-agent")
        assert bundle["mode"] == "passthrough"
        assert len(bundle["wallets"]) == 1
        assert bundle["wallets"][0]["agent_id"] == "a"


class TestPush:
    async def test_push_missing_gateway_404(self, client):
        assert (await client.post("/api/payments/push?gateway_id=nope")).status_code == 404
