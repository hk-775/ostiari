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

    def test_quota_only_bundle_does_not_activate_tool_authorization(self):
        a = AgentAuthPolicy()
        a.configure({
            "enabled": False,
            "quota_enabled": True,
            "default_grants": [],
            "agents": {
                "limited": {
                    "allowed_tools": [],
                    "rate_limit_rpm": 1,
                },
            },
        })

        assert a.check("limited", "db.delete") == (True, "")
        assert a.check_llm("limited", "gpt-4o", "openai").allowed
        blocked = a.check_llm("limited", "gpt-4o", "openai")
        assert not blocked.allowed
        assert blocked.limit_type == "rate_limit"

    def test_rate_limit_uses_rolling_agent_window(self):
        a = AgentAuthPolicy()
        a.configure({
            "enabled": True,
            "default_grants": ["*"],
            "agents": {
                "limited": {
                    "allowed_tools": ["*"],
                    "rate_limit_rpm": 1,
                },
            },
        })
        first = a.check_llm("limited", "gpt-4o", "openai")
        second = a.check_llm("limited", "gpt-4o", "openai")
        assert first.allowed
        assert not second.allowed
        assert second.limit_type == "rate_limit"

    def test_projected_budget_reservations_block_concurrent_overspend(self):
        a = AgentAuthPolicy()
        a.configure({
            "enabled": True,
            "default_grants": ["*"],
            "agents": {
                "limited": {
                    "allowed_tools": ["*"],
                    "budget_usd": 0.01,
                },
            },
        })
        first = a.check_llm(
            "limited", "gpt-4o", "openai",
            estimated_cost=0.006, reserve=True, count_request=False,
        )
        second = a.check_llm(
            "limited", "gpt-4o", "openai",
            estimated_cost=0.006, reserve=True, count_request=False,
        )
        assert first.allowed and first.reservation_id is not None
        assert not second.allowed and second.limit_type == "budget"

        a.release_agent_reservation("limited", first.reservation_id)
        retry = a.check_llm(
            "limited", "gpt-4o", "openai",
            estimated_cost=0.006, reserve=True, count_request=False,
        )
        assert retry.allowed

    def test_complete_runtime_fields_and_configured_alert(self):
        alerts = []
        a = AgentAuthPolicy()
        a.on_budget_alert(lambda *args: alerts.append(args))
        a.configure({
            "enabled": True,
            "default_grants": ["*"],
            "agents": {
                "limited": {
                    "allowed_tools": ["*"],
                    "budget_usd": 10,
                    "spend_usd": 7,
                    "rate_limit_rpm": 5,
                    "max_tokens_per_request": 128,
                    "alert_threshold_pct": 80,
                },
            },
        })

        assert a.cap_max_tokens("limited", 1024) == 128
        row = a.list_agents()[0]
        assert row["spend_usd"] == 7
        assert row["rate_limit_rpm"] == 5
        assert row["max_tokens_per_request"] == 128
        assert row["alert_threshold_pct"] == 80

        a.record_agent_spend("limited", 1.1)
        a.record_agent_spend("limited", 0.1)
        assert alerts == [("80%", "limited", 8.1, 10)]

    def test_two_gateway_instances_share_agent_rate_and_budget(self):
        class _Store:
            def __init__(self):
                self.rates = {}
                self.budgets = {}

            def rate_allow(self, key, limit, _window):
                current = self.rates.get(key, 0)
                if current >= limit:
                    return False
                self.rates[key] = current + 1
                return True

            def budget_reserve(self, key, amount, limit):
                current = self.budgets.get(key, 0.0)
                if current + amount >= limit:
                    return False
                self.budgets[key] = current + amount
                return True

            def budget_adjust(self, key, delta):
                self.budgets[key] = self.budgets.get(key, 0.0) + delta

            def budget_spend(self, key):
                return self.budgets.get(key, 0.0)

        store = _Store()
        policies = [AgentAuthPolicy(), AgentAuthPolicy()]
        for policy in policies:
            policy.attach_shared_store(store, "shared-gateway")
            policy.configure({
                "enabled": True,
                "default_grants": ["*"],
                "agents": {
                    "limited": {
                        "allowed_tools": ["*"],
                        "rate_limit_rpm": 1,
                        "budget_usd": 0.01,
                    },
                },
            })

        assert policies[0].check_llm(
            "limited", "gpt-4o", "openai", estimated_cost=0.006,
            reserve=True, count_request=False,
        ).allowed
        budget_block = policies[1].check_llm(
            "limited", "gpt-4o", "openai", estimated_cost=0.006,
            reserve=True, count_request=False,
        )
        assert not budget_block.allowed and budget_block.limit_type == "budget"

        assert policies[0].check_llm("limited", "gpt-4o", "openai").allowed
        rate_block = policies[1].check_llm("limited", "gpt-4o", "openai")
        assert not rate_block.allowed and rate_block.limit_type == "rate_limit"


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


class TestCompleteRuntimeEnforcement:
    @staticmethod
    def _client(monkeypatch, sidecar_id: str) -> TestClient:
        monkeypatch.delenv("OSTIARI_DISABLE_AXON_ROUTER", raising=False)
        from ostiari_gateway.server import create_app

        client = TestClient(create_app(initial_config=SidecarConfig(
            sidecar_id=sidecar_id,
            modules=ModulesConfig(llm_gateway=True),
            llm={"default_model": "gpt-4o", "max_tokens": 1024},
        )))
        response = client.post("/config/agent-auth", json={
            "enabled": True,
            "default_grants": ["*"],
            "agents": {
                "limited": {
                    "allowed_tools": ["*"],
                    "allowed_models": ["*"],
                    "allowed_providers": ["*"],
                    "rate_limit_rpm": 1,
                    "max_tokens_per_request": 64,
                },
            },
        })
        assert response.status_code == 200
        return client

    def test_chat_caps_tokens_and_blocks_second_request(self, monkeypatch):
        seen = {}

        async def _route(self_inner, **kwargs):
            seen.update(kwargs)
            return AxonResult(
                content="ok", model="gpt-4o", provider="openai",
                input_tokens=1, output_tokens=1,
            )

        client = self._client(monkeypatch, "chat-agent-quota")
        with patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available", True), \
             patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route", new=_route):
            first = client.post(
                "/v1/chat/completions",
                headers={"X-Agent-Id": "limited"},
                json={
                    "model": "gpt-4o",
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
            second = client.post(
                "/v1/chat/completions",
                headers={"X-Agent-Id": "limited"},
                json={
                    "model": "gpt-4o",
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": "again"}],
                },
            )

        assert first.status_code == 200
        assert seen["max_tokens"] == 64
        assert second.status_code == 429
        assert "rate limit" in second.json()["error"]["message"].lower()

    def test_messages_caps_tokens_and_blocks_second_request(self, monkeypatch):
        seen = {}

        async def _route(self_inner, **kwargs):
            seen.update(kwargs)
            return AxonResult(
                content="ok", model="claude-haiku-4-5", provider="anthropic",
                input_tokens=1, output_tokens=1,
            )

        client = self._client(monkeypatch, "messages-agent-quota")
        with patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available", True), \
             patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route", new=_route):
            first = client.post(
                "/v1/messages",
                headers={"X-Agent-Id": "limited"},
                json={
                    "model": "claude-haiku-4-5",
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
            second = client.post(
                "/v1/messages",
                headers={"X-Agent-Id": "limited"},
                json={
                    "model": "claude-haiku-4-5",
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": "again"}],
                },
            )

        assert first.status_code == 200
        assert seen["max_tokens"] == 64
        assert second.status_code == 429
        assert second.json()["error"]["type"] == "rate_limit_error"

    def test_invoke_caps_every_round_and_rate_limits_calls(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_DISABLE_AXON_ROUTER", "1")
        from ostiari_gateway.modules.llm_gateway.providers import LLMResponse
        from ostiari_gateway.server import create_app

        client = TestClient(create_app(initial_config=SidecarConfig(
            sidecar_id="invoke-agent-quota",
            modules=ModulesConfig(llm_gateway=True),
            llm={"default_model": "claude-sonnet-4-6", "max_tokens": 1024},
        )))
        client.post("/config/agent-auth", json={
            "enabled": True,
            "default_grants": ["*"],
            "agents": {
                "limited": {
                    "allowed_tools": ["*"],
                    "rate_limit_rpm": 1,
                    "max_tokens_per_request": 64,
                },
            },
        })
        seen = {}

        def _call(_self, _primary, _fallback, _messages, _tools, max_tokens=None):
            seen["max_tokens"] = max_tokens
            return LLMResponse(
                content="ok", model="claude-sonnet-4-6",
                input_tokens=1, output_tokens=1,
            )

        with patch(
            "ostiari_gateway.modules.llm_gateway.executor.AgenticExecutor._call_with_fallback",
            new=_call,
        ):
            first = client.post(
                "/invoke",
                headers={"X-Agent-Id": "limited"},
                json={"messages": [{"role": "user", "content": "hello"}]},
            )
            second = client.post(
                "/invoke",
                headers={"X-Agent-Id": "limited"},
                json={"messages": [{"role": "user", "content": "again"}]},
            )

        assert first.status_code == 200
        assert first.json()["response"] == "ok"
        assert seen["max_tokens"] == 64
        assert second.status_code == 200
        assert "rate limit" in second.json()["response"].lower()
        assert second.json()["rounds"] == 0

    def test_api_shims_emit_control_plane_usage(self, monkeypatch):
        from ostiari_gateway.modules.llm_gateway.cost_reporter import CostReporter

        calls = []

        async def _route(self_inner, **kwargs):
            model = kwargs.get("model") or "gpt-4o"
            return AxonResult(
                content="ok", model=model, provider="openai",
                input_tokens=10, output_tokens=5,
            )

        async def _report(self_inner, **kwargs):
            calls.append(kwargs)

        async def _flush(self_inner):
            return None

        client = self._client(monkeypatch, "shim-cost-reporting")
        client.post("/config/agent-auth", json={
            "enabled": True,
            "default_grants": ["*"],
            "agents": {
                "limited": {
                    "allowed_tools": ["*"],
                    "rate_limit_rpm": 10,
                },
            },
        })
        with patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available", True), \
             patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route", new=_route), \
             patch.object(CostReporter, "report", new=_report), \
             patch.object(CostReporter, "flush", new=_flush):
            chat = client.post(
                "/v1/chat/completions",
                headers={"X-Agent-Id": "limited"},
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
            messages = client.post(
                "/v1/messages",
                headers={"X-Agent-Id": "limited"},
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )

        assert chat.status_code == 200
        assert messages.status_code == 200
        assert [call["action"] for call in calls] == ["chat", "messages"]
        assert all(call["agent_id"] == "limited" for call in calls)
        assert all(call["cost_usd"] > 0 for call in calls)
        assert all(call["record_quota"] is False for call in calls)
