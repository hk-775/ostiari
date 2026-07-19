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


class TestReconcile:
    def test_seen_and_known_is_governed(self):
        c = _FakeCollector("t", [_s("research-agent", "t", call_count=5)])
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
        c = _FakeCollector("t", [_s("a2a.Research-Agent", "t")])
        out = discovery.reconcile([c], known_agent_ids=["research-agent"])
        assert any(x.status == "governed" for x in out)

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
def seeded_traces(app_and_db):
    """Put a known + an unknown agent into the trace buffer; isolate the
    in-memory agents registry so onboard-mutations don't leak across tests."""
    from control_plane.routers import agents as agents_mod
    from control_plane.routers.traces import _recent_traces
    saved = dict(agents_mod._agents)   # snapshot (conftest may have cleared it)
    # Ensure a KNOWN agent exists so we can assert governed vs. discovered.
    agents_mod._agents["research-agent"] = agents_mod.AgentConfig(
        name="research-agent", framework="openai", gateway_id="crm-agent",
    )
    _recent_traces.clear()
    _recent_traces.extend([
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

    async def test_onboard_moves_shadow_to_governed(self, client):
        before = (await client.get("/api/discovery/agents")).json()
        assert next(a for a in before["agents"] if a["agent_id"] == "rogue-scraper")["status"] == "discovered"

        r = await client.post("/api/discovery/onboard",
                              json={"agent_id": "rogue-scraper", "gateway_id": "crm-agent"})
        assert r.status_code == 200

        after = (await client.get("/api/discovery/agents")).json()
        assert next(a for a in after["agents"] if a["agent_id"] == "rogue-scraper")["status"] == "governed"

    async def test_onboard_duplicate_409(self, client):
        r = await client.post("/api/discovery/onboard", json={"agent_id": "research-agent"})
        assert r.status_code == 409
