"""Tests for cross-agent (A2A) protocol governance:
delegation policy, trust scoring, chain depth, identity propagation, shadow."""

import pytest
from ostiari_gateway.cross_agent import CrossAgentPolicy
from ostiari_gateway.models import SidecarConfig
from ostiari_gateway.server import create_app
from starlette.testclient import TestClient


# ─── Policy engine (unit) ────────────────────────────────────────────────────

class TestCrossAgentPolicy:
    def test_disabled_allows_everything(self):
        p = CrossAgentPolicy()
        assert p.check("a", "b") == (True, "")

    def test_edge_allow_and_deny(self):
        p = CrossAgentPolicy()
        p.configure({"enabled": True, "default_allow": False,
                     "edges": {"research": {"allow": ["coder"], "deny": ["payments"]}}})
        assert p.check("research", "coder")[0] is True
        assert p.check("research", "payments")[0] is False
        # not in allow list, default_allow False -> denied
        assert p.check("research", "random")[0] is False

    def test_deny_wins_over_allow(self):
        p = CrossAgentPolicy()
        p.configure({"enabled": True,
                     "edges": {"a": {"allow": ["*"], "deny": ["b"]}}})
        assert p.check("a", "c")[0] is True   # wildcard allow
        assert p.check("a", "b")[0] is False  # explicit deny wins

    def test_default_allow_true_permits_unlisted(self):
        p = CrossAgentPolicy()
        p.configure({"enabled": True, "default_allow": True})
        assert p.check("anyone", "anyone_else")[0] is True

    def test_trust_threshold_blocks_low_score_callee(self):
        p = CrossAgentPolicy()
        p.configure({"enabled": True, "default_allow": True,
                     "min_trust": 60, "trust_scores": {"sketchy": 20, "trusted": 95}})
        assert p.check("a", "trusted")[0] is True
        allowed, reason = p.check("a", "sketchy")
        assert allowed is False and "trust score" in reason

    def test_default_trust_is_50(self):
        p = CrossAgentPolicy()
        p.configure({"enabled": True, "default_allow": True, "min_trust": 60})
        # unscored agent defaults to 50 < 60 -> blocked
        assert p.check("a", "unscored")[0] is False

    def test_chain_depth_guard(self):
        p = CrossAgentPolicy()
        p.configure({"enabled": True, "default_allow": True, "max_chain_depth": 2})
        assert p.check("a", "b", chain=["x", "y"])[0] is True
        allowed, reason = p.check("a", "b", chain=["x", "y", "z"])
        assert allowed is False and "chain depth" in reason

    def test_get_status_roundtrip(self):
        p = CrossAgentPolicy()
        cfg = {"enabled": True, "default_allow": False, "min_trust": 70,
               "trust_scores": {"x": 80}, "edges": {"a": {"allow": ["b"], "deny": []}}}
        p.configure(cfg)
        s = p.get_status()
        assert s["enabled"] and s["min_trust"] == 70
        assert s["edges"]["a"]["allow"] == ["b"]


# ─── Config endpoint ─────────────────────────────────────────────────────────

class TestCrossAgentConfigEndpoint:
    def test_get_default_disabled(self):
        client = TestClient(create_app())
        r = client.get("/config/cross-agent")
        assert r.status_code == 200
        assert r.json()["enabled"] is False

    def test_post_and_readback(self):
        client = TestClient(create_app())
        r = client.post("/config/cross-agent", json={
            "enabled": True, "min_trust": 50, "edges": {"a": {"allow": ["b"]}},
        })
        assert r.status_code == 200
        assert r.json()["enabled"] is True
        assert client.get("/config/cross-agent").json()["edges"]["a"]["allow"] == ["b"]


# ─── Identity / chain propagation ────────────────────────────────────────────

class TestDelegationChain:
    def test_chain_starts_with_caller(self):
        # Non-a2a tool call: the trace/records should treat the caller as the
        # chain root. We assert via the mode/health path that the header parsing
        # doesn't error and the caller is recognized.
        client = TestClient(create_app())
        # unknown tool -> 404, but header parsing (X-Delegation-Chain) must be safe
        r = client.post("/tool/nope", json={}, headers={
            "X-Agent-Id": "root-agent", "X-Delegation-Chain": "",
        })
        assert r.status_code == 404


# ─── A2A delegation gate (integration through proxy_tool) ────────────────────

def _app_with_fake_agent(callee="coder"):
    """Build an app and inject a fake connected A2A agent so the a2a.* path is
    reachable without a real remote server. Returns (app, fake_client)."""
    from unittest.mock import AsyncMock, MagicMock
    from ostiari_gateway.a2a.models import AgentCard

    app = create_app()
    am = app.state.a2a_manager
    card = AgentCard(name=callee, url="http://fake/a2a", description="fake")
    am._cards[callee] = card
    fake_client = MagicMock()
    fake_client.send_task = AsyncMock(return_value=MagicMock())  # never asserted; gate blocks first
    am._clients[callee] = fake_client
    am._configs[callee] = MagicMock(url="http://fake/a2a")
    return app, am, fake_client


class TestA2ADelegationGate:
    def test_blocked_delegation_returns_403(self):
        app, am, client = _app_with_fake_agent("payments")
        app.state.cross_agent.configure({
            "enabled": True, "default_allow": False,
            "edges": {"research": {"allow": ["coder"]}},  # research may NOT call payments
        })
        tc = TestClient(app)
        r = tc.post("/tool/a2a.payments", json={"message": "pay"},
                    headers={"X-Agent-Id": "research"})
        assert r.status_code == 403
        body = r.json()
        assert body["limit_type"] == "cross_agent_delegation"
        assert body["delegation_chain"] == ["research"]

    def test_allowed_delegation_proceeds(self):
        app, am, client = _app_with_fake_agent("coder")
        app.state.cross_agent.configure({
            "enabled": True, "default_allow": False,
            "edges": {"research": {"allow": ["coder"]}},
        })
        tc = TestClient(app)
        r = tc.post("/tool/a2a.coder", json={"message": "build"},
                    headers={"X-Agent-Id": "research"})
        # gate passes -> the (mocked) A2A client is actually invoked
        assert r.status_code in (200, 502)  # 502 only if mock shape rejected downstream
        assert client.send_task.await_count == 1

    def test_shadow_mode_would_block_not_block(self):
        app, am, client = _app_with_fake_agent("payments")
        app.state.manager.config.mode = "shadow"
        app.state.cross_agent.configure({
            "enabled": True, "default_allow": False,
            "edges": {"research": {"allow": ["coder"]}},
        })
        tc = TestClient(app)
        r = tc.post("/tool/a2a.payments", json={"message": "pay"},
                    headers={"X-Agent-Id": "research"})
        assert r.status_code == 200  # not blocked
        body = r.json()
        assert body["shadow"] is True and body["would_block"] is True
        # In shadow, a would-block delegation must NOT actually call the agent.
        assert client.send_task.await_count == 0

    def test_chain_extends_from_inbound_header(self):
        app, am, client = _app_with_fake_agent("coder")
        app.state.cross_agent.configure({
            "enabled": True, "max_chain_depth": 2, "default_allow": True,
        })
        tc = TestClient(app)
        # inbound chain already 2 deep; adding current agent -> depth 3 > max 2
        r = tc.post("/tool/a2a.coder", json={"message": "x"}, headers={
            "X-Agent-Id": "third", "X-Delegation-Chain": "first>second",
        })
        assert r.status_code == 403
        assert "chain depth" in r.json()["reason"]


# ─── A2A startup reconnect (survives gateway restart) ────────────────────────

class TestA2AStartupReconnect:
    """A2A agents in the control-plane registration bundle are reconnected on
    gateway startup, so they survive a bare restart (no re-register script)."""

    def test_reconnects_from_registration_bundle(self, monkeypatch):
        from unittest.mock import AsyncMock
        from ostiari_gateway.a2a.models import AgentCard, AgentSkill

        # Discovery returns a fake card; add_agent uses it (patched at source).
        card = AgentCard(
            name="DevOps Assistant", url="http://localhost:9200/a2a", description="fake",
            skills=[AgentSkill(id="deploy", name="Deploy")],
        )
        monkeypatch.setattr(
            "ostiari_gateway.a2a.manager.fetch_agent_card", AsyncMock(return_value=card),
        )
        monkeypatch.setattr(
            "ostiari_gateway.a2a.discovery.fetch_agent_card", AsyncMock(return_value=card),
        )

        # Fake lifecycle: register() returns a bundle carrying an a2a_agents entry.
        from ostiari_gateway import server as server_mod

        class _FakeLifecycle:
            def __init__(self, *a, **k): pass
            def set_config_callback(self, cb): pass
            async def register(self):
                return {"config": {"a2a_agents": [{"url": "http://localhost:9200", "name": ""}]}}
            async def start_heartbeat(self, interval=30): pass
            async def stop(self): pass

        monkeypatch.setattr(server_mod, "SidecarConfig", SidecarConfig)
        import ostiari_gateway.lifecycle as lc
        monkeypatch.setattr(lc, "LifecycleManager", _FakeLifecycle)

        config = SidecarConfig(sidecar_id="crm-agent", control_plane_url="http://cp")
        app = create_app(initial_config=config)
        with TestClient(app):  # runs lifespan → register → reconnect a2a
            agents = app.state.a2a_manager.list_agents()
            assert any(a["name"] == "devops_assistant" for a in agents)


# ─── Dynamic (behavior-adjusted) trust ───────────────────────────────────────

class TestDynamicTrust:
    def _policy(self):
        p = CrossAgentPolicy()
        p.configure({
            "enabled": True, "default_allow": True, "min_trust": 60,
            "trust_scores": {"payments": 90, "flaky": 65},
        })
        return p

    def test_good_behavior_stays_at_configured(self):
        p = self._policy()
        for _ in range(10):
            p.record_outcome("payments", risky=False)
        assert p.effective_trust("payments") == 90        # ceiling, unchanged
        assert p.check("research", "payments")[0] is True

    def test_configured_is_a_ceiling_not_raised_by_good_behavior(self):
        p = self._policy()
        for _ in range(10):
            p.record_outcome("flaky", risky=False)
        assert p.effective_trust("flaky") == 65           # never exceeds configured

    def test_risky_behavior_lowers_effective_trust(self):
        p = self._policy()
        for _ in range(10):
            p.record_outcome("payments", risky=True)      # 100% risky → -50
        assert p.effective_trust("payments") == 40        # 90 - 50
        assert p.effective_trust("payments") < 90

    def test_degrading_callee_loses_delegation(self):
        p = self._policy()
        # payments starts trusted (90 ≥ 60) → allowed
        assert p.check("research", "payments")[0] is True
        # it misbehaves repeatedly → effective trust drops below min_trust
        for _ in range(10):
            p.record_outcome("payments", risky=True)
        allowed, reason = p.check("research", "payments")
        assert allowed is False
        assert "lowered by recent risky behavior" in reason

    def test_partial_risk_partial_penalty(self):
        p = self._policy()
        for i in range(10):
            p.record_outcome("payments", risky=(i < 4))   # 40% risky → -20
        assert p.effective_trust("payments") == 70        # 90 - 20

    def test_dynamic_off_uses_configured(self):
        p = CrossAgentPolicy(dynamic_trust=False)
        p.configure({"enabled": True, "min_trust": 60, "trust_scores": {"x": 90}})
        for _ in range(10):
            p.record_outcome("x", risky=True)
        assert p.effective_trust("x") == 90               # dynamic disabled
