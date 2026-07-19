"""Tests for gateway human-in-the-loop enforcement of the intervene tier."""

import pytest
from ostiari_gateway import server as server_mod
from ostiari_gateway.models import PolicyConfig, SidecarConfig, ToolDefinition
from ostiari_gateway.server import create_app
from starlette.testclient import TestClient


def _app(httpserver):
    """A gateway whose tool exists; policy nudges send_email into the intervene
    band so we exercise HITL without depending on exact score math."""
    httpserver.expect_request("/send", method="POST").respond_with_json({"sent": True})
    config = SidecarConfig(
        sidecar_id="crm-agent",
        control_plane_url="http://cp.local",   # non-empty so HITL tries the CP
        tools=[ToolDefinition(name="send_email", endpoint=httpserver.url_for("/send"))],
        # risk_adjust pushes send_email to ~50 → intervene (allow<=30, block>70)
        policy=PolicyConfig(rules=[{"type": "risk_adjust", "action": "send_email", "risk_adjust": 50}]),
    )
    return create_app(initial_config=config)


class TestHitlOffByDefault:
    def test_intervene_executes_when_hitl_off(self, httpserver, monkeypatch):
        monkeypatch.delenv("OSTIARI_HITL", raising=False)
        c = TestClient(_app(httpserver))
        r = c.post("/tool/send_email", json={"to": "x@y.com"}, headers={"X-Agent-Id": "a"})
        # HITL off → intervene doesn't pause; call proceeds (200)
        assert r.status_code == 200


class TestHitlEnabled:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_HITL", "on")

    def test_intervene_returns_202_and_creates_approval(self, httpserver, monkeypatch):
        created = {}
        async def fake_create(cp_url, payload):
            created.update(payload)
            return {"id": "apr-test-1"}
        monkeypatch.setattr(server_mod, "_create_approval", fake_create)

        c = TestClient(_app(httpserver))
        r = c.post("/tool/send_email", json={"to": "x@y.com"}, headers={"X-Agent-Id": "a"})
        assert r.status_code == 202
        body = r.json()
        assert body["pending_approval"] is True
        assert body["approval_id"] == "apr-test-1"
        assert created["action"] == "send_email"   # approval carried the call

    def test_approved_completes_the_loop_and_tool_runs(self, httpserver, monkeypatch):
        """Full APPROVE loop: approved id → tool actually executes → real body."""
        async def fake_check(cp_url, approval_id):
            return "approved" if approval_id == "apr-ok" else None
        monkeypatch.setattr(server_mod, "_check_approval", fake_check)

        c = TestClient(_app(httpserver))
        r = c.post("/tool/send_email", json={"to": "x@y.com"},
                   headers={"X-Agent-Id": "a", "X-Approval-Id": "apr-ok"})
        assert r.status_code == 200
        # The loop completed: the REAL tool ran and returned its real response.
        assert r.json()["result"] == {"sent": True}
        # And the tool endpoint was genuinely hit exactly once.
        assert len(httpserver.log) == 1

    def test_denied_completes_the_loop_and_tool_never_runs(self, httpserver, monkeypatch):
        """Full DENY loop: denied id → 403 → the tool is NEVER executed."""
        async def fake_check(cp_url, approval_id):
            return "denied"
        monkeypatch.setattr(server_mod, "_check_approval", fake_check)

        c = TestClient(_app(httpserver))
        r = c.post("/tool/send_email", json={"to": "x@y.com"},
                   headers={"X-Agent-Id": "a", "X-Approval-Id": "apr-no"})
        assert r.status_code == 403
        assert r.json()["limit_type"] == "intervention"
        # The whole point: a denied action must NOT reach the real tool.
        assert len(httpserver.log) == 0

    def test_pending_does_not_run_the_tool(self, httpserver, monkeypatch):
        """No decision yet → 202 pending, and the tool must not have run."""
        async def fake_create(cp_url, payload):
            return {"id": "apr-pending"}
        monkeypatch.setattr(server_mod, "_create_approval", fake_create)

        c = TestClient(_app(httpserver))
        r = c.post("/tool/send_email", json={"to": "x@y.com"}, headers={"X-Agent-Id": "a"})
        assert r.status_code == 202
        assert len(httpserver.log) == 0   # paused before execution

    def test_allow_tier_unaffected(self, httpserver, monkeypatch):
        # a low-risk call (no risk_adjust) stays allow → not gated
        httpserver.expect_request("/q", method="POST").respond_with_json({"ok": True})
        config = SidecarConfig(
            sidecar_id="crm-agent", control_plane_url="http://cp.local",
            tools=[ToolDefinition(name="db_query", endpoint=httpserver.url_for("/q"))],
            policy=PolicyConfig(allow=["db_query"]),
        )
        c = TestClient(create_app(initial_config=config))
        r = c.post("/tool/db_query", json={"sql": "SELECT 1"}, headers={"X-Agent-Id": "a"})
        assert r.status_code == 200
