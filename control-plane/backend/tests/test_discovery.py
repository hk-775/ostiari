"""Tests for agent discovery: reconciliation engine + router (multi-source)."""

import pytest
from control_plane import discovery

pytestmark = pytest.mark.anyio


# ─── Reconciliation engine (unit) ────────────────────────────────────────────

class _FakeCollector:
    def __init__(self, source, sightings):
        self.source = source
        self._s = sightings
    def collect(self):
        return self._s


def _s(agent_id, source, **kw):
    return discovery.Sighting(agent_id=agent_id, source=source, **kw)


async def _gateway(client, gateway_id="crm-agent", headers=None):
    response = await client.post(
        "/api/gateways",
        headers=headers,
        json={
            "id": gateway_id,
            "name": gateway_id,
            "endpoint": f"http://{gateway_id}.local",
        },
    )
    assert response.status_code == 200, response.text


@pytest.fixture
def gateway_push(monkeypatch):
    sent = {}

    class _Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"status": "applied"}

    class _Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, json=None):
            sent["url"] = url
            sent["json"] = json
            return _Response()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return sent


class TestReconcile:
    def test_seen_and_known_is_governed(self):
        c = _FakeCollector(
            "t",
            [_s("research-agent", "t", call_count=5, governed=True)],
        )
        out = discovery.reconcile([c], known_agent_ids=["research-agent"])
        a = next(x for x in out if x.agent_id == "research-agent")
        assert a.status == "governed" and a.registered

    def test_seen_not_known_is_discovered_shadow(self):
        c = _FakeCollector("t", [_s("rogue-bot", "t", call_count=3)])
        out = discovery.reconcile([c], known_agent_ids=["research-agent"])
        a = next(x for x in out if x.agent_id == "rogue-bot")
        assert a.status == "discovered" and not a.registered

    def test_known_not_seen_is_stale(self):
        out = discovery.reconcile([_FakeCollector("t", [])], known_agent_ids=["ghost"])
        a = next(x for x in out if x.agent_id == "ghost")
        assert a.status == "governed_unseen" and a.registered

    def test_multi_source_merges_one_agent(self):
        c1 = _FakeCollector("traces", [_s("x", "traces", call_count=2, gateways=["crm-agent"], confidence=0.5)])
        c2 = _FakeCollector("cloudtrail", [_s("x", "cloudtrail", evidence="bedrock call", confidence=0.9)])
        out = discovery.reconcile([c1, c2], known_agent_ids=[])
        a = next(x for x in out if x.agent_id == "x")
        assert set(a.sources) == {"traces", "cloudtrail"}   # merged, not duplicated
        assert a.call_count == 2
        assert a.confidence == 0.9                          # max across sources (0.5, 0.9)
        assert len([x for x in out if x.agent_id == "x"]) == 1

    def test_normalization_matches_a2a_and_case(self):
        # "a2a.Research-Agent" seen should match registered "research-agent"
        c = _FakeCollector(
            "t",
            [_s("a2a.Research-Agent", "t", governed=True)],
        )
        out = discovery.reconcile([c], known_agent_ids=["research-agent"])
        assert any(x.status == "governed" for x in out)

    def test_registered_cloud_identity_is_not_called_governed(self):
        c = _FakeCollector("cloud", [_s("batch", "cloud")])
        out = discovery.reconcile([c], known_agent_ids={"batch": "gw-a"})
        agent = next(x for x in out if x.agent_id == "batch")
        assert agent.status == "registered_off_gateway"
        assert agent.assigned_gateway == "gw-a"

    def test_trace_through_wrong_gateway_is_still_off_gateway(self):
        c = _FakeCollector(
            "traces",
            [_s("batch", "traces", governed=True, gateways=["gw-b"])],
        )
        out = discovery.reconcile([c], known_agent_ids={"batch": "gw-a"})
        agent = next(x for x in out if x.agent_id == "batch")
        assert agent.status == "registered_off_gateway"
        assert agent.governed_gateways == ["gw-b"]

    def test_shadow_listed_first(self):
        c = _FakeCollector("t", [
            _s("known", "t", call_count=1),
            _s("shadow", "t", call_count=1),
        ])
        out = discovery.reconcile([c], known_agent_ids=["known"])
        assert out[0].status == "discovered"   # shadow sorts before governed

    def test_bad_collector_does_not_break(self):
        class _Boom:
            source = "boom"
            def collect(self): raise RuntimeError("nope")
        out = discovery.reconcile([_Boom(), _FakeCollector("t", [_s("ok", "t")])], known_agent_ids=[])
        assert any(x.agent_id == "ok" for x in out)   # good source still processed

    def test_unknown_agent_id_skipped(self):
        c = _FakeCollector("t", [_s("", "t"), _s("real", "t")])
        out = discovery.reconcile([c], known_agent_ids=[])
        assert all(x.agent_id for x in out)


# ─── Router (integration) ────────────────────────────────────────────────────

@pytest.fixture
def seeded_traces(app_and_db, monkeypatch):
    """Put a known + an unknown agent into the trace buffer; isolate the
    in-memory agents registry so onboard-mutations don't leak across tests.
    Enable the mock cloud collector (off by default) for the multi-source tests."""
    monkeypatch.setenv("OSTIARI_DISCOVERY_MOCK", "1")
    from control_plane.routers import agents as agents_mod
    from control_plane.routers.traces import DEFAULT_ORG, _recent_traces
    # Deep-copy the snapshot: _agents is org -> name -> config, so a shallow
    # dict() would share the inner per-org dicts and the teardown restore would
    # not undo writes made during the test.
    saved = {org: dict(byname) for org, byname in agents_mod._agents.items()}
    # Ensure a KNOWN agent exists so we can assert governed vs. discovered.
    # Must be indexed [org][name] — writing to the outer dict puts an AgentConfig
    # where a per-org dict belongs and the agent is invisible to every reader.
    agents_mod._agents[DEFAULT_ORG]["research-agent"] = agents_mod.AgentConfig(
        name="research-agent", framework="openai", gateway_id="crm-agent",
    )
    _recent_traces.clear()
    _recent_traces[DEFAULT_ORG].extend([
        {"agent_id": "research-agent", "gateway_id": "crm-agent", "action": "web_search", "tier": "allow"},
        {"agent_id": "rogue-scraper", "gateway_id": "crm-agent", "action": "web_search", "tier": "allow"},
        {"agent_id": "rogue-scraper", "gateway_id": "crm-agent", "action": "db_query", "tier": "block"},
    ])
    yield
    _recent_traces.clear()
    agents_mod._agents.clear()
    agents_mod._agents.update(saved)   # restore


@pytest.mark.usefixtures("seeded_traces")
class TestDiscoveryRouter:
    async def test_lists_shadow_and_governed(self, client):
        d = (await client.get("/api/discovery/agents")).json()
        assert d["summary"]["shadow"] >= 1
        ids = {a["agent_id"]: a for a in d["agents"]}
        # research-agent is registered (seed) + seen → governed
        assert ids["research-agent"]["status"] == "governed"
        # rogue-scraper seen only → shadow
        assert ids["rogue-scraper"]["status"] == "discovered"
        assert ids["rogue-scraper"]["call_count"] == 2

    async def test_mock_cloud_sources_present(self, client):
        d = (await client.get("/api/discovery/agents")).json()
        # The mock cloud collector contributes off-gateway shadow agents.
        ids = {a["agent_id"] for a in d["agents"]}
        assert "batch-summarizer" in ids or "nightly-report-bot" in ids
        # multiple sources reported
        assert len(d["summary"]["sources"]) >= 2

    async def test_collector_failure_is_visible_without_breaking_report(
        self, client, monkeypatch
    ):
        class _BrokenCollector:
            source = "aws-test"
            last_error = ""

            def collect(self):
                self.last_error = "access denied"
                return []

        monkeypatch.setattr(
            "control_plane.routers.discovery.default_collectors",
            lambda _org: [_BrokenCollector()],
        )
        response = await client.get("/api/discovery/agents")
        assert response.status_code == 200
        status = response.json()["summary"]["source_status"]
        assert status == [{
            "source": "aws-test",
            "status": "error",
            "detail": "access denied",
        }]

    async def test_onboard_moves_shadow_to_governed(self, client, gateway_push):
        await _gateway(client)
        before = (await client.get("/api/discovery/agents")).json()
        assert next(a for a in before["agents"] if a["agent_id"] == "rogue-scraper")["status"] == "discovered"

        r = await client.post("/api/discovery/onboard",
                              json={"agent_id": "rogue-scraper", "gateway_id": "crm-agent"})
        assert r.status_code == 200, r.text
        result = r.json()
        assert result["traffic_routed"] is True
        assert result["gateway_policy"]["status"] == "pushed"
        assert gateway_push["url"] == "http://crm-agent.local/config/agent-auth"
        assert gateway_push["json"]["enabled"] is True
        assert gateway_push["json"]["default_grants"] == ["*"]
        assert gateway_push["json"]["agents"]["rogue-scraper"]["allowed_tools"] == []

        after = (await client.get("/api/discovery/agents")).json()
        assert next(a for a in after["agents"] if a["agent_id"] == "rogue-scraper")["status"] == "governed"

    async def test_onboard_duplicate_409(self, client):
        r = await client.post(
            "/api/discovery/onboard",
            json={"agent_id": "research-agent", "gateway_id": "crm-agent"},
        )
        assert r.status_code == 409

    async def test_onboarded_agent_appears_in_the_agents_registry(
        self, client, gateway_push
    ):
        """Onboarding must write [org][agent_id]. Writing to the outer org-keyed
        dict put an AgentConfig where a per-org dict belongs: the agent was
        invisible to /api/agents and the bogus key then looked like an org."""
        await _gateway(client)
        await client.post("/api/discovery/onboard",
                          json={"agent_id": "rogue-scraper", "gateway_id": "crm-agent",
                                "framework": "langchain"})
        listed = (await client.get("/api/agents")).json()
        names = {a["name"] for a in listed}
        assert "rogue-scraper" in names
        assert "research-agent" in names   # the pre-existing agent survived the write

    async def test_cloud_only_agent_stays_off_gateway_after_onboarding(
        self, client, gateway_push
    ):
        await _gateway(client)
        response = await client.post(
            "/api/discovery/onboard",
            json={"agent_id": "batch-summarizer", "gateway_id": "crm-agent"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "registered_off_gateway"
        assert response.json()["traffic_routed"] is False

        report = (await client.get("/api/discovery/agents")).json()
        agent = next(
            item for item in report["agents"]
            if item["agent_id"] == "batch-summarizer"
        )
        assert agent["status"] == "registered_off_gateway"
        assert report["summary"]["off_gateway"] >= 1

    async def test_onboard_preserves_an_existing_authorization_policy(
        self, client, gateway_push
    ):
        await _gateway(client)
        from control_plane.database import async_session
        from control_plane.models.database import Gateway

        existing = {
            "enabled": True,
            "quota_enabled": False,
            "default_grants": ["read_only"],
            "default_models": ["approved-model"],
            "default_providers": ["bedrock"],
            "agents": {
                "existing-agent": {
                    "allowed_tools": ["lookup"],
                },
            },
        }
        async with async_session() as db:
            gateway = await db.get(Gateway, "crm-agent")
            gateway.config = {"agent_auth_base": existing}
            await db.commit()

        response = await client.post(
            "/api/discovery/onboard",
            json={"agent_id": "rogue-scraper", "gateway_id": "crm-agent"},
        )
        assert response.status_code == 200, response.text
        payload = gateway_push["json"]
        assert payload["enabled"] is True
        assert payload["quota_enabled"] is False
        assert payload["default_grants"] == ["read_only"]
        assert payload["default_models"] == ["approved-model"]
        assert payload["default_providers"] == ["bedrock"]
        assert payload["agents"]["existing-agent"] == {
            "allowed_tools": ["lookup"],
        }
        assert payload["agents"]["rogue-scraper"]["allowed_tools"] == []

    async def test_onboard_requires_a_tenant_gateway(self, client):
        response = await client.post(
            "/api/discovery/onboard",
            json={"agent_id": "rogue-scraper", "gateway_id": "missing"},
        )
        assert response.status_code == 404
        assert "Gateway" in response.json()["detail"]

    async def test_onboard_rejects_identity_not_in_discovery(self, client):
        await _gateway(client)
        response = await client.post(
            "/api/discovery/onboard",
            json={"agent_id": "invented-agent", "gateway_id": "crm-agent"},
        )
        assert response.status_code == 404
        assert "current discovery report" in response.json()["detail"]

    async def test_governed_status_requires_reading_agent_names_not_org_names(self, client):
        """`_agents` is org -> name -> config. Taking `.keys()` off the outer dict
        yields ORG names, so every genuinely-registered agent misses the
        seen-vs-known match and gets reported as shadow AI."""
        d = (await client.get("/api/discovery/agents")).json()
        ids = {a["agent_id"]: a for a in d["agents"]}
        assert ids["research-agent"]["registered"] is True
        assert "default" not in ids   # an org name must never appear as an agent


class TestDiscoveryOrgIsolation:
    """One tenant's Discovered view must not name another tenant's agents or
    gateways — the trace buffer is per-org and the collector has to honor that."""

    @pytest.fixture
    def two_org_traces(self, app_and_db, monkeypatch):
        # Mock cloud sightings off: they're org-agnostic seeds and would blur
        # the isolation assertion.
        monkeypatch.delenv("OSTIARI_DISCOVERY_MOCK", raising=False)
        from control_plane.routers.traces import _recent_traces
        _recent_traces.clear()
        _recent_traces["org-a"].append(
            {"agent_id": "a-only-agent", "gateway_id": "gw-a", "action": "web_search", "tier": "allow"})
        _recent_traces["org-b"].append(
            {"agent_id": "b-only-agent", "gateway_id": "gw-b", "action": "web_search", "tier": "allow"})
        yield
        _recent_traces.clear()

    @pytest.mark.usefixtures("two_org_traces")
    async def test_each_org_sees_only_its_own_sightings(self, client):
        from control_plane.auth.service import create_access_token

        def hdr(org):
            tok = create_access_token(user_id=1, email=f"{org}@t.io", role="admin", org=org)
            return {"Authorization": f"Bearer {tok}"}

        a = (await client.get("/api/discovery/agents", headers=hdr("org-a"))).json()
        b = (await client.get("/api/discovery/agents", headers=hdr("org-b"))).json()
        a_ids = {x["agent_id"] for x in a["agents"]}
        b_ids = {x["agent_id"] for x in b["agents"]}
        assert a_ids == {"a-only-agent"}
        assert b_ids == {"b-only-agent"}
        # Gateway names leak too, not just agent ids.
        assert a["agents"][0]["gateways"] == ["gw-a"]

    @pytest.mark.usefixtures("two_org_traces")
    async def test_onboarding_in_one_org_does_not_govern_the_other(
        self, client, gateway_push
    ):
        from control_plane.auth.service import create_access_token
        from control_plane.routers import agents as agents_mod

        saved = {org: dict(byname) for org, byname in agents_mod._agents.items()}
        try:
            def hdr(org):
                tok = create_access_token(user_id=1, email=f"{org}@t.io", role="admin", org=org)
                return {"Authorization": f"Bearer {tok}"}

            await _gateway(client, "gw-a", headers=hdr("org-a"))
            r = await client.post("/api/discovery/onboard", headers=hdr("org-a"),
                                  json={"agent_id": "a-only-agent", "gateway_id": "gw-a"})
            assert r.status_code == 200
            # Same agent id in another org is still un-onboarded → no 409, and
            # org-a's registration must not make it "governed" for org-b.
            b = (await client.get("/api/discovery/agents", headers=hdr("org-b"))).json()
            assert all(not x["registered"] for x in b["agents"])
            assert "a-only-agent" not in {a["name"] for a in
                                         (await client.get("/api/agents", headers=hdr("org-b"))).json()}
        finally:
            agents_mod._agents.clear()
            agents_mod._agents.update(saved)

    @pytest.mark.usefixtures("two_org_traces")
    async def test_cannot_onboard_to_another_org_gateway(self, client):
        from control_plane.auth.service import create_access_token

        def hdr(org):
            token = create_access_token(
                user_id=1,
                email=f"{org}@t.io",
                role="admin",
                org=org,
            )
            return {"Authorization": f"Bearer {token}"}

        await _gateway(client, "gw-b", headers=hdr("org-b"))
        response = await client.post(
            "/api/discovery/onboard",
            headers=hdr("org-a"),
            json={"agent_id": "a-only-agent", "gateway_id": "gw-b"},
        )
        assert response.status_code == 404


async def test_onboarded_agents_survive_a_graceful_restart(
    app_and_db, tmp_path, monkeypatch
):
    import control_plane.persistence as persistence
    from control_plane.app import lifespan
    from control_plane.persistence import load_state
    from control_plane.routers.agents import AgentConfig, _agents

    monkeypatch.setenv("OSTIARI_NO_DEMO", "1")
    monkeypatch.setattr(persistence, "STATE_FILE", tmp_path / "state.json")
    _agents.clear()

    async with lifespan(app_and_db):
        _agents["org-a"]["cloud-agent"] = AgentConfig(
            name="cloud-agent",
            framework="bedrock",
            gateway_id="gw-a",
            status="registered_off_gateway",
        )

    saved = load_state()
    assert saved["agents"] == [{
        "name": "cloud-agent",
        "framework": "bedrock",
        "gateway_id": "gw-a",
        "tools": [],
        "description": "",
        "status": "registered_off_gateway",
        "model": "",
        "_org": "org-a",
    }]

    _agents.clear()
    async with lifespan(app_and_db):
        restored = _agents["org-a"]["cloud-agent"]
        assert restored.gateway_id == "gw-a"
        assert restored.status == "registered_off_gateway"
