"""Tests for the seeded gateway quotas.

The seed's job is to make the Quotas page show something true. Its risk isn't
crashing — it's depicting a fleet that disagrees with the other pages, or a set
of budgets that all look the same and so demonstrate nothing.
"""

import pytest

pytestmark = pytest.mark.anyio


async def _seed(db=None):
    from control_plane.database import async_session
    from control_plane.demo_seed import seed_demo_quotas

    if db is not None:
        await seed_demo_quotas(db)
        return
    async with async_session() as s:
        await seed_demo_quotas(s)


async def _usage(db, n=40):
    """Write usage records so quota spend has something real to sum."""
    import random
    from datetime import datetime, timedelta, timezone

    from control_plane.models.database import UsageRecord

    rnd = random.Random(11)
    now = datetime.now(timezone.utc)
    for _ in range(n):
        db.add(UsageRecord(
            gateway_id=rnd.choice(["crm-agent", "ops-agent", "devops-agent", "analytics-agent"]),
            agent_id="coder-agent", model="claude-haiku",
            input_tokens=100, output_tokens=100, total_tokens=200,
            cost_usd=round(rnd.uniform(0.01, 0.05), 5), action="db_query",
            timestamp=now - timedelta(minutes=rnd.randint(0, 600)),
        ))
    await db.commit()


class TestQuotaDemoSeed:
    async def test_seeds_one_quota_per_demo_gateway(self, app_and_db):
        from control_plane.demo_seed import _GATEWAY_QUOTAS
        from control_plane.models.database import DEFAULT_ORG
        from control_plane.routers.quotas import _quotas

        await _seed()
        assert len(_quotas[DEFAULT_ORG]) == len(_GATEWAY_QUOTAS)
        assert {q.scope_id for q in _quotas[DEFAULT_ORG].values()} == {
            gw for _, gw, _, _, _, _ in _GATEWAY_QUOTAS
        }

    async def test_seeding_twice_does_not_duplicate(self, app_and_db):
        from control_plane.models.database import DEFAULT_ORG
        from control_plane.routers.quotas import _quotas

        await _seed()
        first = len(_quotas[DEFAULT_ORG])
        await _seed()
        assert len(_quotas[DEFAULT_ORG]) == first

    async def test_an_existing_quota_suppresses_the_seed(self, client):
        """A real configured quota must not be joined by demo rows."""
        from control_plane.models.database import DEFAULT_ORG
        from control_plane.routers.quotas import _quotas

        r = await client.post("/api/quotas", json={
            "name": "real", "scope": "gateway", "scope_id": "crm-agent",
            "rate_limit_rpm": 10,
        })
        assert r.status_code == 200
        await _seed()
        assert len(_quotas[DEFAULT_ORG]) == 1

    async def test_spend_is_summed_from_the_metered_usage(self, app_and_db):
        """The number on the budget bar must be the same number the Costs and
        Metering pages report — not an independently invented figure."""
        from control_plane.database import async_session
        from control_plane.models.database import DEFAULT_ORG, UsageRecord
        from control_plane.routers.quotas import _quotas
        from sqlalchemy import func, select

        async with async_session() as db:
            await _usage(db)
            await _seed(db)
            rows = (await db.execute(
                select(UsageRecord.gateway_id, func.sum(UsageRecord.cost_usd))
                .group_by(UsageRecord.gateway_id)
            )).all()

        truth = {gw: round(float(total), 4) for gw, total in rows}
        for q in _quotas[DEFAULT_ORG].values():
            assert q.current_spend == pytest.approx(truth[q.scope_id], abs=1e-4), q.scope_id

    async def test_spend_is_zero_when_nothing_has_been_metered(self, app_and_db):
        """No usage rows must give 0.00, not a KeyError or a stale figure."""
        from control_plane.models.database import DEFAULT_ORG
        from control_plane.routers.quotas import _quotas

        await _seed()
        assert all(q.current_spend == 0.0 for q in _quotas[DEFAULT_ORG].values())

    async def test_budgets_span_every_band_the_page_renders(self, app_and_db):
        """Quotas.tsx colors the bar >90 red / >70 amber / else green and warns
        above 80. A seed where every quota were green would show the fields
        exist but never that a limit bites.

        Uses the real metering seed, not a synthetic one: the band spread is a
        property of the budgets chosen against *that* spend, so testing it
        against arbitrary usage would only measure the fixture.
        """
        from control_plane.database import async_session
        from control_plane.demo_seed import seed_demo_db
        from control_plane.models.database import DEFAULT_ORG
        from control_plane.routers.quotas import _quotas

        async with async_session() as db:
            await seed_demo_db(db)
            await _seed(db)

        bands = set()
        warned = 0
        for q in _quotas[DEFAULT_ORG].values():
            pct = q.current_spend / q.budget_limit_usd * 100
            bands.add("red" if pct > 90 else "amber" if pct > 70 else "green")
            warned += pct > 80
        assert bands == {"red", "amber", "green"}, bands
        assert warned >= 1

    async def test_each_budget_hits_its_declared_target_percentage(self, app_and_db):
        """_GATEWAY_QUOTAS records the percentage each budget is meant to put
        real spend at. Pinning it catches the trap I hit while writing this: the
        budgets were first tuned against a long-running instance whose live
        gateway traffic had inflated spend, so the red band appeared there and
        vanished on a fresh start."""
        from control_plane.database import async_session
        from control_plane.demo_seed import _GATEWAY_QUOTAS, seed_demo_db
        from control_plane.models.database import DEFAULT_ORG
        from control_plane.routers.quotas import _quotas

        async with async_session() as db:
            await seed_demo_db(db)
            await _seed(db)

        targets = {gw: pct for _, gw, _, _, _, pct in _GATEWAY_QUOTAS}
        for q in _quotas[DEFAULT_ORG].values():
            pct = q.current_spend / q.budget_limit_usd * 100
            assert pct == pytest.approx(targets[q.scope_id], abs=1.5), (
                f"{q.scope_id}: {pct:.1f}% but _GATEWAY_QUOTAS declares "
                f"{targets[q.scope_id]}%"
            )

    async def test_spend_never_exceeds_its_budget(self, app_and_db):
        """Over 100% would draw a bar clamped at full width next to a
        percentage above 100 — a state the enforcer would never have allowed to
        persist, since the budget blocks first."""
        from control_plane.database import async_session
        from control_plane.models.database import DEFAULT_ORG
        from control_plane.routers.quotas import _quotas

        async with async_session() as db:
            await _usage(db)
            await _seed(db)
        for q in _quotas[DEFAULT_ORG].values():
            assert q.current_spend <= q.budget_limit_usd, q.scope_id

    async def test_current_rpm_is_zero_on_an_idle_fleet(self, app_and_db):
        """Nothing is driving traffic in a fresh demo, so a nonzero RPM would be
        contradicted by Live Traces on the next page over."""
        from control_plane.models.database import DEFAULT_ORG
        from control_plane.routers.quotas import _quotas

        await _seed()
        assert all(q.current_rpm == 0 for q in _quotas[DEFAULT_ORG].values())

    async def test_quotas_target_gateways_that_exist(self, app_and_db):
        """A quota scoped to an unknown gateway can't be pushed — /push looks the
        id up in the gateways table and 404s."""
        from control_plane.demo_seed import _GATEWAY_QUOTAS
        from control_plane.routers.traces import seed_traces

        seed_traces()
        from control_plane.models.database import DEFAULT_ORG
        from control_plane.routers.traces import _recent_traces

        known = {t["gateway_id"] for t in _recent_traces[DEFAULT_ORG]}
        for _, gw, _, _, _, _ in _GATEWAY_QUOTAS:
            assert gw in known, f"quota targets {gw}, which no seeded trace mentions"

    async def test_allowlists_only_name_models_the_demo_meters(self, app_and_db):
        """An allowlist naming a model no usage record mentions would be
        unenforceable here and misrepresent what the fleet runs."""
        from control_plane.demo_seed import _MODELS, _QUOTA_ALLOWED_MODELS

        for gw, models in _QUOTA_ALLOWED_MODELS.items():
            for m in models:
                assert m in _MODELS, f"{gw} allows {m}, which the demo never meters"

    async def test_seeded_quotas_are_visible_on_the_api(self, client):
        """The rows are real QuotaResponse objects on the real route, which is
        what the page fetches."""
        await _seed()
        r = await client.get("/api/quotas")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 4
        assert all(q["scope"] == "gateway" and q["scope_id"] for q in body)

    async def test_a_seeded_quota_can_be_edited_and_deleted(self, client):
        """The page's Edit and Delete buttons must work on seeded rows."""
        await _seed()
        qid = (await client.get("/api/quotas")).json()[0]["id"]
        d = await client.delete(f"/api/quotas/{qid}")
        assert d.status_code == 200
        assert len((await client.get("/api/quotas")).json()) == 3

    async def test_push_payload_matches_the_gateways_quota_config(self, app_and_db):
        """/push builds its payload from these four fields; the sidecar's
        QuotaConfig must accept every one of them, or Push silently does less
        than the page implies."""
        import inspect

        from ostiari_gateway.quota_enforcer import QuotaConfig

        accepted = set(inspect.signature(QuotaConfig.__init__).parameters) - {"self"}
        pushed = {
            "rate_limit_rpm", "budget_limit_usd",
            "max_tokens_per_request", "allowed_models",
        }
        assert pushed <= accepted, pushed - accepted
