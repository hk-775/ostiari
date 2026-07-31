"""Tests for the seeded broker token pools and reconciliation history.

The Token Broker page renders both as tables with no empty state, so the seed's
job is to fill them with figures that agree with the rest of the demo. Its risk
isn't crashing — it's a pool inventory that claims to be auditable while
asserting consumption no usage record supports, or two healthy rows that never
show the depletion halt the feature exists for.
"""

import pytest

pytestmark = pytest.mark.anyio


async def _seed_usage_and_pools(db):
    """Run the real metering seed, then the pool seed over it."""
    from control_plane.demo_seed import seed_demo_broker_pools, seed_demo_db

    await seed_demo_db(db)
    await seed_demo_broker_pools(db)


async def _pools(db):
    from control_plane.models.database import TokenPool
    from sqlalchemy import select

    return {p.provider: p for p in (await db.execute(select(TokenPool))).scalars().all()}


async def _recons(db):
    from control_plane.models.database import ReconciliationRecord
    from sqlalchemy import select

    return (await db.execute(select(ReconciliationRecord))).scalars().all()


class TestBrokerPoolDemoSeed:
    async def test_seeds_a_pool_per_configured_provider(self, app_and_db):
        from control_plane.database import async_session
        from control_plane.demo_seed import _BROKER_POOLS

        async with async_session() as db:
            await _seed_usage_and_pools(db)
            pools = await _pools(db)
        assert set(pools) == {p for p, _, _ in _BROKER_POOLS}

    async def test_seeding_twice_does_not_duplicate(self, app_and_db):
        from control_plane.database import async_session
        from control_plane.demo_seed import seed_demo_broker_pools

        async with async_session() as db:
            await _seed_usage_and_pools(db)
            first = len(await _pools(db))
            n_recon = len(await _recons(db))
            await seed_demo_broker_pools(db)
            assert len(await _pools(db)) == first
            assert len(await _recons(db)) == n_recon

    async def test_an_existing_pool_suppresses_the_seed(self, client):
        """A real funded pool must not be joined by demo rows."""
        from control_plane.database import async_session
        from control_plane.demo_seed import seed_demo_broker_pools, seed_demo_db

        r = await client.post("/api/token-broker/pilot/pools/fund", json={
            "provider": "anthropic", "tokens": 1000, "cost_usd": 1.0,
        })
        assert r.status_code == 200
        async with async_session() as db:
            await seed_demo_db(db)
            await seed_demo_broker_pools(db)
            assert len(await _pools(db)) == 1

    async def test_nothing_is_seeded_without_metered_usage(self, app_and_db):
        """No usage means no consumption to draw down, so a pool would be
        asserting a purchase and burn that nothing backs."""
        from control_plane.database import async_session
        from control_plane.demo_seed import seed_demo_broker_pools

        async with async_session() as db:
            await seed_demo_broker_pools(db)
            assert await _pools(db) == {}
            assert await _recons(db) == []

    async def test_consumed_tokens_are_summed_from_the_usage_records(self, app_and_db):
        """The burn figure must be the metered traffic, not an invented one."""
        from control_plane.broker_pilot import provider_for
        from control_plane.database import async_session
        from control_plane.models.database import UsageRecord
        from sqlalchemy import select

        async with async_session() as db:
            await _seed_usage_and_pools(db)
            rows = (await db.execute(
                select(UsageRecord.model, UsageRecord.total_tokens)
            )).all()
            pools = await _pools(db)

        truth: dict[str, int] = {}
        for model, tokens in rows:
            p = provider_for(model)
            truth[p] = truth.get(p, 0) + int(tokens or 0)
        for provider, pool in pools.items():
            assert pool.consumed_tokens == truth[provider], provider

    async def test_consumed_cost_is_the_discounted_retail_cost(self, app_and_db):
        """draw_down() charges the pool retail * (1 - bulk_discount). A pool
        holding the raw retail figure would overstate our cost by the whole
        broker margin — the number the page exists to report."""
        from control_plane.broker_pilot import provider_for
        from control_plane.database import async_session
        from control_plane.models.database import DEFAULT_ORG, UsageRecord
        from control_plane.routers.token_broker import _config as _tb
        from sqlalchemy import select

        async with async_session() as db:
            await _seed_usage_and_pools(db)
            rows = (await db.execute(
                select(UsageRecord.model, UsageRecord.cost_usd)
            )).all()
            pools = await _pools(db)

        discount = float(_tb[DEFAULT_ORG]["bulk_discount"])
        assert discount > 0, "a zero discount would make this test vacuous"
        truth: dict[str, float] = {}
        for model, retail in rows:
            p = provider_for(model)
            truth[p] = truth.get(p, 0.0) + float(retail or 0.0) * (1 - discount)
        for provider, pool in pools.items():
            assert pool.consumed_cost_usd == pytest.approx(truth[provider], abs=1e-4)

    async def test_consumption_never_exceeds_the_purchase(self, app_and_db):
        """Burning more than was bought would render a negative balance the
        draw-down path clamps at zero — a state it could not reach."""
        from control_plane.database import async_session

        async with async_session() as db:
            await _seed_usage_and_pools(db)
            for p in (await _pools(db)).values():
                assert p.consumed_tokens <= p.purchased_tokens, p.provider

    async def test_one_pool_is_depleted_and_one_is_healthy(self, app_and_db):
        """Depletion halts routing — the feature the pool table exists to show.
        Two healthy rows would demonstrate the columns, not the halt."""
        from control_plane.database import async_session

        async with async_session() as db:
            await _seed_usage_and_pools(db)
            statuses = {p.provider: p.status for p in (await _pools(db)).values()}
        assert set(statuses.values()) == {"active", "depleted"}, statuses

    async def test_status_matches_the_rule_draw_down_enforces(self, app_and_db):
        """The badge must be what enforcement would decide, not a label chosen
        independently: depleted iff remaining <= low_threshold."""
        from control_plane.database import async_session

        async with async_session() as db:
            await _seed_usage_and_pools(db)
            for p in (await _pools(db)).values():
                remaining = max(0, p.purchased_tokens - p.consumed_tokens)
                expected = "depleted" if remaining <= p.low_threshold_tokens else "active"
                assert p.status == expected, (p.provider, remaining, p.low_threshold_tokens)

    async def test_reconciliation_computed_cost_matches_the_route(self, app_and_db):
        """/reconcile recomputes from retail usage cost. If the seeded rows used
        a different basis, hitting Reconcile in the UI would replace them with
        visibly different numbers for the same period."""
        from control_plane.broker_pilot import provider_for
        from control_plane.database import async_session
        from control_plane.models.database import UsageRecord
        from sqlalchemy import select

        async with async_session() as db:
            await _seed_usage_and_pools(db)
            rows = (await db.execute(
                select(UsageRecord.model, UsageRecord.cost_usd)
            )).all()
            recons = await _recons(db)

        truth: dict[str, float] = {}
        for model, retail in rows:
            p = provider_for(model)
            truth[p] = truth.get(p, 0.0) + float(retail or 0.0)
        assert recons
        for r in recons:
            assert r.computed_cost_usd == pytest.approx(truth[r.provider], abs=1e-4)

    async def test_drift_spans_both_sides_of_the_red_threshold(self, app_and_db):
        """TokenBroker.tsx styles |drift_pct| > 5 red. All-benign rows would show
        the column exists but never that drift gets flagged."""
        from control_plane.database import async_session

        async with async_session() as db:
            await _seed_usage_and_pools(db)
            recons = await _recons(db)

        pcts = [
            (r.invoiced_cost_usd - r.computed_cost_usd) / r.computed_cost_usd * 100
            for r in recons if r.computed_cost_usd
        ]
        assert any(abs(p) > 5 for p in pcts), pcts
        assert any(abs(p) <= 5 for p in pcts), pcts

    async def test_pools_only_name_providers_the_demo_meters(self, app_and_db):
        """A pool for a provider no usage record draws from could never move."""
        from control_plane.broker_pilot import provider_for
        from control_plane.demo_seed import _BROKER_POOLS, _MODELS

        metered = {provider_for(m) for m in _MODELS}
        for provider, _, _ in _BROKER_POOLS:
            assert provider in metered, f"pool {provider} draws from no metered model"

    async def test_seeded_pools_are_visible_on_the_api(self, client):
        """The rows come back off the real route the page fetches."""
        from control_plane.database import async_session

        async with async_session() as db:
            await _seed_usage_and_pools(db)

        r = await client.get("/api/token-broker/pilot/pools")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 2
        # remaining_pct is derived by the serializer; a pool with no purchase
        # would divide by zero and report 0% on a nonzero balance.
        assert all(p["purchased_tokens"] > 0 and p["remaining_pct"] > 0 for p in body)

        rr = await client.get("/api/token-broker/pilot/reconciliations")
        assert rr.status_code == 200 and len(rr.json()) == 2

    async def test_a_seeded_pool_can_still_be_topped_up(self, client):
        """Fund must work on seeded rows — and a top-up over the low mark has to
        bring a depleted pool back to active, which is the reactivation path."""
        from control_plane.database import async_session

        async with async_session() as db:
            await _seed_usage_and_pools(db)
            depleted = [p.provider for p in (await _pools(db)).values()
                        if p.status == "depleted"]
        assert depleted
        r = await client.post("/api/token-broker/pilot/pools/fund", json={
            "provider": depleted[0], "tokens": 5_000_000, "cost_usd": 12.0,
        })
        assert r.status_code == 200
        assert r.json()["status"] == "active"
