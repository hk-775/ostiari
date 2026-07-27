"""Tests for per-agent LLM authorization (B1 fix): model/provider/budget grants
are now enforced on both the /v1/messages shim and /invoke, and agent spend is
recorded against the budget.
"""

from __future__ import annotations

from unittest.mock import patch

from ostiari_gateway.agent_auth import AgentAuthPolicy
from ostiari_gateway.models import ModulesConfig, SidecarConfig
from ostiari_gateway.modules.llm_gateway.axon_router import AxonResult
from starlette.testclient import TestClient


class TestAuthorizeLLM:
    def _policy(self):
        a = AgentAuthPolicy()
        a.configure({
            "enabled": True, "default_grants": ["*"],
            "agents": {
                "restricted": {
                    "allowed_tools": ["*"], "allowed_models": ["claude-haiku-4-5"],
                    "allowed_providers": ["anthropic"], "budget_usd": 0.01,
                },
            },
        })
        return a

    def test_allowed_model_provider(self):
        a = self._policy()
        assert a.authorize_llm("restricted", "claude-haiku-4-5", "anthropic") == (True, "")

    def test_disallowed_model_blocked(self):
        a = self._policy()
        ok, reason = a.authorize_llm("restricted", "claude-opus-4-8", "anthropic")
        assert not ok and "model" in reason

    def test_disallowed_provider_blocked(self):
        a = self._policy()
        # a provider the agent lacks (model check on gpt-4o also fails first, both are denials)
        ok, reason = a.authorize_llm("restricted", "some-model", "openai")
        assert not ok

    def test_budget_exhaustion_blocks(self):
        a = self._policy()
        a.record_agent_spend("restricted", 0.02)     # over the $0.01 cap
        ok, reason = a.authorize_llm("restricted", "claude-haiku-4-5", "anthropic")
        assert not ok and "budget" in reason.lower()

    def test_disabled_is_noop(self):
        a = AgentAuthPolicy()   # not enabled
        assert a.authorize_llm("anyone", "any-model", "any") == (True, "")

    def test_unlisted_agent_uses_defaults(self):
        a = self._policy()   # default_models defaults to ["*"]
        assert a.authorize_llm("stranger", "claude-opus-4-8", "anthropic") == (True, "")


class TestShimEnforcement:
    def _app(self):
        from ostiari_gateway.server import create_app
        app = create_app(initial_config=SidecarConfig(
            sidecar_id="auth-test", modules=ModulesConfig(llm_gateway=True),
            llm={"default_model": "claude-sonnet-4-6"}))
        c = TestClient(app)
        # restrict an agent to haiku only
        c.post("/config/agent-auth", json={
            "enabled": True, "default_grants": ["*"],
            "agents": {"restricted": {
                "allowed_tools": ["*"], "allowed_models": ["claude-haiku-4-5-20251001"]}}})
        return c

    def test_shim_blocks_disallowed_model(self):
        c = self._app()
        # requests claude-sonnet-4-6 but only haiku is allowed → 403 before any upstream call
        r = c.post("/v1/messages", headers={"X-Agent-Id": "restricted"},
                   json={"model": "claude-sonnet-4-6", "max_tokens": 8,
                         "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 403
        assert r.json()["error"]["type"] == "permission_error"

    def test_shim_allows_permitted_model(self):
        c = self._app()

        async def _route(self_inner, **kwargs):
            return AxonResult(content="ok", model="claude-haiku-4-5-20251001",
                              provider="anthropic", input_tokens=1, output_tokens=1)
        with patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available", True), \
             patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route", new=_route):
            r = c.post("/v1/messages", headers={"X-Agent-Id": "restricted"},
                       json={"model": "claude-haiku-4-5-20251001", "max_tokens": 8,
                             "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
