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


class TestHitlSurvivesFailClosed:
    """Production is fail-closed, and a fail-closed Guard has no way to resolve an
    intervene in-process: it collapses it to a block and *raises*. That used to
    return 403 straight from the exception handler, which reaches the caller
    before the approval gate ever runs — so production silently deleted the
    intervene tier and the Approvals queue stayed empty however OSTIARI_HITL was
    set. The gateway has a human to ask, so a collapsed intervene must still be
    deferrable.
    """

    @pytest.fixture(autouse=True)
    def _production(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_ENV", "production")   # → fail_open False
        monkeypatch.setenv("OSTIARI_HITL", "on")

    def test_intervene_still_pauses_for_approval_in_production(self, httpserver, monkeypatch):
        created = {}
        async def fake_create(cp_url, payload):
            created.update(payload)
            return {"id": "apr-prod-1"}
        monkeypatch.setattr(server_mod, "_create_approval", fake_create)

        c = TestClient(_app(httpserver))
        r = c.post("/tool/send_email", json={"to": "x@y.com"}, headers={"X-Agent-Id": "a"})
        assert r.status_code == 202, "fail-closed intervene must queue, not 403"
        assert r.json()["pending_approval"] is True
        assert created["action"] == "send_email"
        assert len(httpserver.log) == 0   # still never executes unapproved

    def test_approval_loop_completes_in_production(self, httpserver, monkeypatch):
        async def fake_check(cp_url, approval_id):
            return "approved" if approval_id == "apr-ok" else None
        monkeypatch.setattr(server_mod, "_check_approval", fake_check)

        c = TestClient(_app(httpserver))
        r = c.post("/tool/send_email", json={"to": "x@y.com"},
                   headers={"X-Agent-Id": "a", "X-Approval-Id": "apr-ok"})
        assert r.status_code == 200
        assert r.json()["result"] == {"sent": True}
        assert len(httpserver.log) == 1

    def test_pending_approval_explains_why(self, httpserver, monkeypatch):
        """The signals have to survive the raise too — the explanation is what the
        approver reads to decide."""
        async def fake_create(cp_url, payload):
            return {"id": "apr-prod-2"}
        monkeypatch.setattr(server_mod, "_create_approval", fake_create)

        c = TestClient(_app(httpserver))
        r = c.post("/tool/send_email", json={"to": "x@y.com"}, headers={"X-Agent-Id": "a"})
        d = r.json()["decision"]
        assert d["tier"] == "intervene"          # not "block"
        assert d["score"] == r.json()["score"]
        assert d["summary"]

    def test_real_block_still_403s_in_production(self, httpserver, monkeypatch):
        """The escape hatch must not swallow genuine blocks: a policy deny is not
        an intervene and has no business in the approvals queue."""
        httpserver.expect_request("/send", method="POST").respond_with_json({"sent": True})
        config = SidecarConfig(
            sidecar_id="crm-agent", control_plane_url="http://cp.local",
            tools=[ToolDefinition(name="send_email", endpoint=httpserver.url_for("/send"))],
            policy=PolicyConfig(block=["send_email"]),
        )
        called = False
        async def fake_create(cp_url, payload):
            nonlocal called
            called = True
            return {"id": "should-not-happen"}
        monkeypatch.setattr(server_mod, "_create_approval", fake_create)

        c = TestClient(create_app(initial_config=config))
        r = c.post("/tool/send_email", json={"to": "x@y.com"}, headers={"X-Agent-Id": "a"})
        assert r.status_code == 403
        assert r.json()["blocked"] is True
        assert called is False, "a policy block must not create an approval"
        assert len(httpserver.log) == 0

    def test_hitl_off_in_production_still_blocks(self, httpserver, monkeypatch):
        """Without HITL there is nobody to defer to, so fail-closed still means
        blocked — the deferral is an escalation path, not a bypass."""
        monkeypatch.setenv("OSTIARI_HITL", "off")
        c = TestClient(_app(httpserver))
        r = c.post("/tool/send_email", json={"to": "x@y.com"}, headers={"X-Agent-Id": "a"})
        assert r.status_code == 403
        assert len(httpserver.log) == 0

    def test_denied_in_production_never_runs_the_tool(self, httpserver, monkeypatch):
        async def fake_check(cp_url, approval_id):
            return "denied"
        monkeypatch.setattr(server_mod, "_check_approval", fake_check)

        c = TestClient(_app(httpserver))
        r = c.post("/tool/send_email", json={"to": "x@y.com"},
                   headers={"X-Agent-Id": "a", "X-Approval-Id": "apr-no"})
        assert r.status_code == 403
        assert r.json()["limit_type"] == "intervention"
        assert len(httpserver.log) == 0

    def test_validate_reports_the_scored_tier_not_just_the_verdict(self, httpserver):
        """/validate is read-only, so there is nothing to defer — but it must not
        report a fail-closed intervene as if it had scored 'block'."""
        c = TestClient(_app(httpserver))
        r = c.post("/validate", json={"action": "send_email", "params": {"to": "x@y.com"}})
        assert r.status_code == 200
        body = r.json()
        assert body["allowed"] is False        # fail-closed: not permitted
        assert body["tier"] == "block"         # the enforced decision
        assert body["original_tier"] == "intervene"   # what it actually scored


class TestShadowModeUnaffected:
    """Shadow mode never enforces, and its short-circuit runs ahead of the
    approval gate — a fail-closed intervene there is an observation to record,
    not a call to pause."""

    @pytest.fixture(autouse=True)
    def _production_shadow(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_ENV", "production")
        monkeypatch.setenv("OSTIARI_HITL", "on")

    def test_shadow_does_not_queue_approvals(self, httpserver, monkeypatch):
        httpserver.expect_request("/send", method="POST").respond_with_json({"sent": True})
        config = SidecarConfig(
            sidecar_id="crm-agent", control_plane_url="http://cp.local", mode="shadow",
            tools=[ToolDefinition(name="send_email", endpoint=httpserver.url_for("/send"))],
            policy=PolicyConfig(rules=[
                {"type": "risk_adjust", "action": "send_email", "risk_adjust": 50},
            ]),
        )
        called = False
        async def fake_create(cp_url, payload):
            nonlocal called
            called = True
            return {"id": "should-not-happen"}
        monkeypatch.setattr(server_mod, "_create_approval", fake_create)

        c = TestClient(create_app(initial_config=config))
        r = c.post("/tool/send_email", json={"to": "x@y.com"}, headers={"X-Agent-Id": "a"})
        assert r.status_code == 200
        assert called is False
        assert len(httpserver.log) == 0   # shadow never runs the real tool


class TestDecisionExplanation:
    """Every tool response carries a 'decision' explanation (why it scored)."""

    def test_allowed_response_has_decision(self, httpserver, monkeypatch):
        monkeypatch.delenv("OSTIARI_HITL", raising=False)
        httpserver.expect_request("/q", method="POST").respond_with_json({"ok": True})
        config = SidecarConfig(
            sidecar_id="crm-agent", control_plane_url="http://cp.local",
            tools=[ToolDefinition(name="db_query", endpoint=httpserver.url_for("/q"))],
            policy=PolicyConfig(allow=["db_query"]),
        )
        c = TestClient(create_app(initial_config=config))
        r = c.post("/tool/db_query", json={"sql": "SELECT 1"}, headers={"X-Agent-Id": "a"})
        assert r.status_code == 200
        d = r.json().get("decision")
        assert d and "tier" in d and "score" in d and "summary" in d and "factors" in d

    def test_risky_response_explains_the_factor(self, httpserver, monkeypatch):
        monkeypatch.delenv("OSTIARI_HITL", raising=False)
        httpserver.expect_request("/q", method="POST").respond_with_json({"ok": True})
        config = SidecarConfig(
            sidecar_id="crm-agent", control_plane_url="http://cp.local",
            tools=[ToolDefinition(name="db_delete", endpoint=httpserver.url_for("/q"))],
            policy=PolicyConfig(allow=["db_delete"]),
        )
        c = TestClient(create_app(initial_config=config))
        # unbounded delete → parameter-risk factor should appear in the decision
        r = c.post("/tool/db_delete", json={"sql": "DELETE FROM t WHERE 1=1"},
                   headers={"X-Agent-Id": "a"})
        d = r.json().get("decision", {})
        assert any(f["source"] == "parameter-risk" for f in d.get("factors", []))
