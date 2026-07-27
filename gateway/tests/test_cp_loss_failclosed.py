"""Test the CP-unreachable fail-closed posture helper (#4)."""

from __future__ import annotations


class TestFailClosedOnCpLoss:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("OSTIARI_FAIL_CLOSED_ON_CP_LOSS", raising=False)
        monkeypatch.delenv("OSTIARI_ENV", raising=False)
        from ostiari_gateway.server import _fail_closed_on_cp_loss
        assert _fail_closed_on_cp_loss() is False

    def test_explicit_on(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_FAIL_CLOSED_ON_CP_LOSS", "true")
        from ostiari_gateway.server import _fail_closed_on_cp_loss
        assert _fail_closed_on_cp_loss() is True

    def test_production_implies_on(self, monkeypatch):
        monkeypatch.delenv("OSTIARI_FAIL_CLOSED_ON_CP_LOSS", raising=False)
        monkeypatch.setenv("OSTIARI_ENV", "production")
        from ostiari_gateway.server import _fail_closed_on_cp_loss
        assert _fail_closed_on_cp_loss() is True

    def test_explicit_off_overrides_production(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_ENV", "production")
        monkeypatch.setenv("OSTIARI_FAIL_CLOSED_ON_CP_LOSS", "false")
        from ostiari_gateway.server import _fail_closed_on_cp_loss
        assert _fail_closed_on_cp_loss() is False

    def test_deny_by_default_grants_block_unregistered_agent(self):
        """The config applied on CP loss (deny-by-default) blocks unlisted agents."""
        from ostiari_gateway.agent_auth import AgentAuthPolicy
        a = AgentAuthPolicy()
        a.configure({"enabled": True, "default_grants": [],
                     "default_models": [], "default_providers": []})
        allowed, reason = a.check("some-agent", "any-tool")
        assert not allowed  # least-privilege: unregistered agent denied
