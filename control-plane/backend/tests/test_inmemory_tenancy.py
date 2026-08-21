"""Org isolation for the in-memory router stores.

These stores were process-global dicts keyed by bare id, so two orgs' data
collided. Now they're nested per org. Scoping is driven by the token's org
claim (get_current_org, defaults to "default"). The providers case is a
security test — those records hold encrypted API keys.
"""

from __future__ import annotations

import pytest
from control_plane.auth.service import create_access_token

pytestmark = pytest.mark.anyio


def _admin(org: str) -> dict[str, str]:
    tok = create_access_token(user_id=1, email=f"{org}@t.io", role="admin", org=org)
    return {"Authorization": f"Bearer {tok}"}


A = _admin("org-a")
B = _admin("org-b")


@pytest.mark.usefixtures("multi_tenant_mode")
class TestProvidersSecretIsolation:
    """The important one: an org must never read or overwrite another org's
    provider credentials by reusing a provider name."""

    async def test_provider_names_do_not_collide_across_orgs(self, client):
        await client.post("/api/providers", headers=A,
                          json={"name": "anthropic", "api_key": "sk-A-secret"})
        await client.post("/api/providers", headers=B,
                          json={"name": "anthropic", "api_key": "sk-B-secret"})

        a_list = (await client.get("/api/providers", headers=A)).json()
        b_list = (await client.get("/api/providers", headers=B)).json()
        assert {p["name"] for p in a_list} == {"anthropic"}
        assert {p["name"] for p in b_list} == {"anthropic"}

        # Each org's key is its own — B's create must NOT have overwritten A's.
        a_key = (await client.get("/api/providers/anthropic/key", headers=A)).json()
        b_key = (await client.get("/api/providers/anthropic/key", headers=B)).json()
        assert a_key["api_key"] == "sk-A-secret"
        assert b_key["api_key"] == "sk-B-secret"

    async def test_cross_org_provider_access_is_404(self, client):
        await client.post("/api/providers", headers=A,
                          json={"name": "openai", "api_key": "sk-A"})
        # Org B never created "openai" → 404 on read-key and delete.
        assert (await client.get("/api/providers/openai/key", headers=B)).status_code == 404
        assert (await client.delete("/api/providers/openai", headers=B)).status_code == 404
        # Owner still has it.
        assert (await client.get("/api/providers/openai/key", headers=A)).status_code == 200


@pytest.mark.usefixtures("multi_tenant_mode")
class TestAgentsIsolation:
    async def test_agents_scoped(self, client):
        await client.post("/api/agents", headers=A,
                          json={"name": "agent-a", "framework": "openai", "gateway_id": "g"})
        await client.post("/api/agents", headers=B,
                          json={"name": "agent-b", "framework": "openai", "gateway_id": "g"})
        a = {x["name"] for x in (await client.get("/api/agents", headers=A)).json()}
        b = {x["name"] for x in (await client.get("/api/agents", headers=B)).json()}
        assert a == {"agent-a"}
        assert b == {"agent-b"}


@pytest.mark.usefixtures("multi_tenant_mode")
class TestQuotasIsolation:
    async def test_quotas_scoped_and_ids_per_org(self, client):
        r_a = await client.post("/api/quotas", headers=A,
                                json={"name": "q-a", "scope": "gateway", "scope_id": "g", "rate_limit_rpm": 60})
        r_b = await client.post("/api/quotas", headers=B,
                                json={"name": "q-b", "scope": "gateway", "scope_id": "g", "rate_limit_rpm": 99})
        assert r_a.status_code in (200, 201) and r_b.status_code in (200, 201)
        a = (await client.get("/api/quotas", headers=A)).json()
        b = (await client.get("/api/quotas", headers=B)).json()
        assert len(a) == 1 and a[0]["rate_limit_rpm"] == 60
        assert len(b) == 1 and b[0]["rate_limit_rpm"] == 99


@pytest.mark.usefixtures("multi_tenant_mode")
class TestExperimentsIsolation:
    async def test_experiments_scoped(self, client):
        await client.post("/api/experiments", headers=A,
                          json={"name": "exp-a", "model_a": "x", "model_b": "y",
                                "traffic_pct_b": 10, "gateway_id": "g"})
        await client.post("/api/experiments", headers=B,
                          json={"name": "exp-b", "model_a": "x", "model_b": "y",
                                "traffic_pct_b": 10, "gateway_id": "g"})
        a = {e["name"] for e in (await client.get("/api/experiments", headers=A)).json()}
        b = {e["name"] for e in (await client.get("/api/experiments", headers=B)).json()}
        assert "exp-a" in a and "exp-b" not in a
        assert "exp-b" in b and "exp-a" not in b


class TestBackCompatSingleOrg:
    async def test_no_token_defaults_to_default_org(self, client):
        """Without a token everything lands in the default org, as before."""
        r = await client.post("/api/agents",
                              json={"name": "solo", "framework": "openai", "gateway_id": "g"})
        assert r.status_code in (200, 201)
        names = {x["name"] for x in (await client.get("/api/agents")).json()}
        assert "solo" in names
