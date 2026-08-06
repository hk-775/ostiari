"""Multi-tenancy isolation for the broker pilot and the gateway proxy.

The last two tables without an `org_id` were `token_pools` and
`reconciliation_records`, and none of the pilot's routes took `get_current_org`.
Follows the conventions in `test_multitenancy.py`: mint a token per org and
assert one tenant can neither see nor touch another's data.

`token_pools` is the one table where `org_id` is part of the *primary key*
rather than an indexed column, because pool identity is (org, provider) — so
these tests also pin that two orgs can each hold a pool for the same provider,
which is what a plain added column would not have allowed.
"""

from __future__ import annotations

import pytest
from control_plane.auth.service import create_access_token

pytestmark = pytest.mark.anyio


def _org_headers(org: str, role: str = "admin") -> dict[str, str]:
    tok = create_access_token(user_id=1, email=f"{org}@test.io", role=role, org=org)
    return {"Authorization": f"Bearer {tok}"}


ORG_A = _org_headers("org-a")
ORG_B = _org_headers("org-b")

PILOT = "/api/token-broker/pilot"


async def _fund(client, headers, provider, tokens, cost, low=0):
    return await client.post(f"{PILOT}/pools/fund", headers=headers, json={
        "provider": provider, "tokens": tokens, "cost_usd": cost,
        "low_threshold_tokens": low,
    })


@pytest.mark.usefixtures("app_and_db")
class TestPoolIsolation:
    async def test_orgs_cannot_see_each_others_pools(self, client):
        await _fund(client, ORG_A, "anthropic", 1_000, 1.0)
        await _fund(client, ORG_B, "openai", 500, 0.5)

        a = (await client.get(f"{PILOT}/pools", headers=ORG_A)).json()
        b = (await client.get(f"{PILOT}/pools", headers=ORG_B)).json()
        assert [p["provider"] for p in a] == ["anthropic"]
        assert [p["provider"] for p in b] == ["openai"]

    async def test_two_orgs_each_hold_a_pool_for_the_same_provider(self, client):
        """The reason org_id is in the primary key.

        With `provider` as the sole key the second fund would have collided with
        the first — or worse, silently topped it up.
        """
        await _fund(client, ORG_A, "anthropic", 1_000, 1.0)
        r = await _fund(client, ORG_B, "anthropic", 7_777, 7.0)
        assert r.status_code == 200, r.text

        a = (await client.get(f"{PILOT}/pools", headers=ORG_A)).json()
        b = (await client.get(f"{PILOT}/pools", headers=ORG_B)).json()
        assert len(a) == len(b) == 1
        assert a[0]["purchased_tokens"] == 1_000
        assert b[0]["purchased_tokens"] == 7_777

    async def test_funding_does_not_top_up_another_orgs_pool(self, client):
        await _fund(client, ORG_A, "anthropic", 1_000, 1.0)
        await _fund(client, ORG_B, "anthropic", 500, 0.5)
        await _fund(client, ORG_B, "anthropic", 500, 0.5)   # B accumulates on its own

        a = (await client.get(f"{PILOT}/pools", headers=ORG_A)).json()[0]
        b = (await client.get(f"{PILOT}/pools", headers=ORG_B)).json()[0]
        assert a["purchased_tokens"] == 1_000     # untouched by either B call
        assert b["purchased_tokens"] == 1_000     # 500 + 500, its own


@pytest.mark.usefixtures("app_and_db")
class TestDrawDownIsolation:
    async def test_drawdown_burns_only_the_owning_orgs_pool(self, client):
        """Traffic from one tenant must not consume another's purchased tokens.

        This is the money case: pools are prepaid inventory, so a mis-scoped
        draw-down spends a tenant's balance on someone else's requests.
        """
        await _fund(client, ORG_A, "anthropic", 10_000, 10.0)
        await _fund(client, ORG_B, "anthropic", 10_000, 10.0)

        from control_plane.database import async_session
        from control_plane.routers.broker_pilot import draw_down
        async with async_session() as db:
            await draw_down(db, model="claude-haiku", tokens=4_000,
                            our_cost_usd=4.0, org="org-b")
            await db.commit()

        a = (await client.get(f"{PILOT}/pools", headers=ORG_A)).json()[0]
        b = (await client.get(f"{PILOT}/pools", headers=ORG_B)).json()[0]
        assert a["consumed_tokens"] == 0
        assert b["consumed_tokens"] == 4_000

    async def test_drawdown_for_an_org_without_a_pool_is_a_noop(self, client):
        """Best-effort is per-org: A's pool must not absorb B's unprovisioned traffic."""
        await _fund(client, ORG_A, "anthropic", 10_000, 10.0)

        from control_plane.database import async_session
        from control_plane.routers.broker_pilot import draw_down
        async with async_session() as db:
            await draw_down(db, model="claude-haiku", tokens=9_999,
                            our_cost_usd=9.0, org="org-b")
            await db.commit()

        a = (await client.get(f"{PILOT}/pools", headers=ORG_A)).json()[0]
        assert a["consumed_tokens"] == 0
        assert (await client.get(f"{PILOT}/pools", headers=ORG_B)).json() == []

    async def test_usage_recording_draws_down_the_reporting_gateways_org(self, client):
        """End-to-end: the org comes from the gateway record, not the payload.

        `POST /api/costs/record` is called by gateways with no user token, so the
        org has to be derived from the gateway's row — the same rule the usage
        record itself follows.
        """
        await client.post("/api/gateways", headers=ORG_B,
                          json={"id": "gw-b", "name": "B", "endpoint": "http://b:8421",
                                "description": ""})
        await _fund(client, ORG_A, "anthropic", 10_000, 10.0)
        await _fund(client, ORG_B, "anthropic", 10_000, 10.0)

        r = await client.post("/api/costs/record", json={
            "gateway_id": "gw-b", "agent_id": "agent-1", "model": "claude-haiku",
            "input_tokens": 1_000, "output_tokens": 500, "total_tokens": 1_500,
            "cost_usd": 0.01, "action": "invoke",
        })
        assert r.status_code == 200, r.text

        a = (await client.get(f"{PILOT}/pools", headers=ORG_A)).json()[0]
        b = (await client.get(f"{PILOT}/pools", headers=ORG_B)).json()[0]
        assert a["consumed_tokens"] == 0
        assert b["consumed_tokens"] == 1_500


@pytest.mark.usefixtures("app_and_db")
class TestReconciliationIsolation:
    async def test_orgs_cannot_see_each_others_reconciliations(self, client):
        for headers in (ORG_A, ORG_B):
            r = await client.post(f"{PILOT}/reconcile", headers=headers, json={
                "provider": "anthropic", "period_days": 30, "invoiced_cost_usd": 5.0,
            })
            assert r.status_code == 200, r.text

        a = (await client.get(f"{PILOT}/reconciliations", headers=ORG_A)).json()
        b = (await client.get(f"{PILOT}/reconciliations", headers=ORG_B)).json()
        assert len(a) == 1 and len(b) == 1
        assert a[0]["id"] != b[0]["id"]

    async def test_computed_cost_counts_only_the_callers_usage(self, client):
        """Drift is the number this page exists to show.

        Computing it over every tenant's usage inflated one org's drift by the
        whole fleet's traffic, which reads as a billing discrepancy that isn't.
        """
        for org, headers in (("org-a", ORG_A), ("org-b", ORG_B)):
            await client.post("/api/gateways", headers=headers,
                              json={"id": f"gw-{org}", "name": org,
                                    "endpoint": "http://x:8421", "description": ""})
            await client.post("/api/costs/record", json={
                "gateway_id": f"gw-{org}", "agent_id": "a", "model": "claude-haiku",
                "input_tokens": 100, "output_tokens": 100, "total_tokens": 200,
                "cost_usd": 1.0, "action": "invoke",
            })

        r = await client.post(f"{PILOT}/reconcile", headers=ORG_A, json={
            "provider": "anthropic", "period_days": 30, "invoiced_cost_usd": 1.0,
        })
        # A's own $1.00, not the $2.00 both tenants spent together.
        assert r.json()["computed_cost_usd"] == pytest.approx(1.0)
        assert r.json()["consumed_tokens"] == 200


@pytest.mark.usefixtures("app_and_db")
class TestProxyIsolation:
    """The widest hole: the proxy reached another org's *runtime*, not its records.

    `POST /api/proxy/gateway/{id}/{path}` forwards an arbitrary body to the
    gateway's own endpoints, so an unscoped lookup let any caller who knew a
    gateway id call another tenant's /config and /tool routes through the
    control plane.
    """

    async def test_cross_org_proxy_is_404(self, client):
        await client.post("/api/gateways", headers=ORG_A,
                          json={"id": "a-gw", "name": "A", "endpoint": "http://a:8421",
                                "description": ""})
        r = await client.post("/api/proxy/gateway/a-gw/tools", headers=ORG_B, json={})
        assert r.status_code == 404

    async def test_cross_org_proxy_get_is_404(self, client):
        await client.post("/api/gateways", headers=ORG_A,
                          json={"id": "a-gw", "name": "A", "endpoint": "http://a:8421",
                                "description": ""})
        r = await client.get("/api/proxy/gateway/a-gw/tools", headers=ORG_B)
        assert r.status_code == 404

    async def test_a_cross_org_id_is_indistinguishable_from_a_missing_one(self, client):
        """Both 404. A different status would confirm the gateway exists."""
        await client.post("/api/gateways", headers=ORG_A,
                          json={"id": "a-gw", "name": "A", "endpoint": "http://a:8421",
                                "description": ""})
        real = await client.get("/api/proxy/gateway/a-gw/tools", headers=ORG_B)
        fake = await client.get("/api/proxy/gateway/no-such-gw/tools", headers=ORG_B)
        assert real.status_code == fake.status_code == 404


@pytest.mark.usefixtures("app_and_db")
class TestSingleOrgBackCompat:
    """Tokenless callers land in the default org — the demo/dev posture."""

    async def test_fund_and_list_without_a_token(self, client):
        r = await _fund(client, None, "anthropic", 1_000, 1.0)
        assert r.status_code == 200, r.text
        pools = (await client.get(f"{PILOT}/pools")).json()
        assert [p["provider"] for p in pools] == ["anthropic"]

    async def test_default_org_pool_is_invisible_to_a_real_tenant(self, client):
        await _fund(client, None, "anthropic", 1_000, 1.0)
        assert (await client.get(f"{PILOT}/pools", headers=ORG_A)).json() == []

    async def test_collector_stays_unscoped(self, client):
        """Deployment config, not tenant data — every org sees the same mode."""
        a = (await client.get(f"{PILOT}/collector", headers=ORG_A)).json()
        b = (await client.get(f"{PILOT}/collector", headers=ORG_B)).json()
        assert a == b == {"mode": "simulated"}
