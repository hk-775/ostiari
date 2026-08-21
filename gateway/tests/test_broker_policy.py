"""Gateway enforcement for depleted token-broker provider pools."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from ostiari_gateway.models import ModulesConfig, SidecarConfig
from ostiari_gateway.modules.llm_gateway.axon_router import AxonRouter
from ostiari_gateway.modules.llm_gateway.broker_policy import (
    BrokerPoolDepletedError,
    BrokerPoolPolicy,
)
from ostiari_gateway.modules.llm_gateway.providers import LLMResponse
from starlette.testclient import TestClient


class TestBrokerPoolPolicy:
    def test_depleted_primary_reroutes_to_active_fallback(self):
        policy = BrokerPoolPolicy()
        policy.configure(
            [
                {"provider": "anthropic", "status": "depleted"},
                {"provider": "openai", "status": "active"},
            ]
        )

        assert policy.require_direct_route(
            ["claude-sonnet-4-6", "gpt-4o"]
        ) == ["gpt-4o"]

    def test_all_depleted_blocks_before_provider_call(self):
        policy = BrokerPoolPolicy()
        policy.configure(
            [
                {"provider": "anthropic", "status": "depleted"},
                {"provider": "openai", "status": "depleted"},
            ]
        )

        with pytest.raises(BrokerPoolDepletedError):
            policy.require_direct_route(["claude-sonnet-4-6", "gpt-4o"])

    def test_unprovisioned_provider_remains_available(self):
        policy = BrokerPoolPolicy()
        policy.configure([{"provider": "anthropic", "status": "depleted"}])

        assert policy.require_direct_route(["gpt-4o"]) == ["gpt-4o"]

    def test_runtime_aliases_share_one_pool(self):
        policy = BrokerPoolPolicy()
        policy.configure([{"provider": "bedrock", "status": "depleted"}])

        assert policy.is_provider_available("bedrock-mantle") is False
        assert policy.is_model_available("bedrock/claude-sonnet") is False


class TestAxonProviderFiltering:
    @staticmethod
    def _axon(policy: BrokerPoolPolicy):
        mappings = [
            SimpleNamespace(provider="anthropic"),
            SimpleNamespace(provider="openai"),
        ]
        registry = SimpleNamespace(
            models={"claude-sonnet": SimpleNamespace(providers=mappings)}
        )

        class _Router:
            available_providers = None
            model_registry = registry

            def is_model_available(self, name):
                model = self.model_registry.models[name]
                if self.available_providers is None:
                    return bool(model.providers)
                return any(
                    mapping.provider in self.available_providers
                    for mapping in model.providers
                )

        core = _Router()

        class _EmbeddedRouter:
            _runtime = SimpleNamespace(router=core)

            def model_available(self, name):
                return core.is_model_available(name)

            def has_available_models(self):
                return any(
                    core.is_model_available(name)
                    for name in core.model_registry.models
                )

        axon = AxonRouter(broker_policy=policy)
        axon._built = True
        axon._available = True
        axon._router = _EmbeddedRouter()
        return axon

    def test_depleted_provider_is_removed_and_funding_restores_it(self):
        policy = BrokerPoolPolicy()
        policy.configure([{"provider": "anthropic", "status": "depleted"}])
        axon = self._axon(policy)

        axon._apply_broker_policy()
        assert axon._core_router().available_providers == frozenset({"openai"})
        assert axon.model_available("claude-sonnet") is True

        policy.configure([{"provider": "anthropic", "status": "active"}])
        axon._apply_broker_policy()
        assert axon._core_router().available_providers is None


def _app(monkeypatch, pools, *, fallback=True):
    monkeypatch.setenv("OSTIARI_DISABLE_AXON_ROUTER", "1")
    monkeypatch.delenv("OSTIARI_REQUIRE_AXON", raising=False)
    from ostiari_gateway.server import create_app

    llm = {"default_model": "claude-sonnet-4-6"}
    if fallback:
        llm["fallback_chain"] = ["gpt-4o"]
    return create_app(
        initial_config=SidecarConfig(
            sidecar_id="broker-gateway",
            modules=ModulesConfig(llm_gateway=True),
            llm=llm,
            broker_pools=pools,
        )
    )


class TestRuntimeEnforcement:
    def test_invoke_reroutes_to_funded_fallback(self, monkeypatch):
        app = _app(
            monkeypatch,
            [
                {"provider": "anthropic", "status": "depleted"},
                {"provider": "openai", "status": "active"},
            ],
        )
        called: list[str] = []

        def _call(_self, *, model, **_kwargs):
            called.append(model)
            return LLMResponse(
                content="routed",
                model=model,
                provider="openai",
                input_tokens=2,
                output_tokens=1,
            )

        with patch(
            "ostiari_gateway.modules.llm_gateway.providers.LLMProvider.call",
            new=_call,
        ):
            response = TestClient(app).post(
                "/invoke",
                json={"messages": [{"role": "user", "content": "hello"}]},
            )

        assert response.status_code == 200, response.text
        assert called == ["gpt-4o"]
        assert response.json()["model_used"] == "gpt-4o"

    def test_invoke_blocks_when_every_direct_route_is_depleted(
        self, monkeypatch
    ):
        app = _app(
            monkeypatch,
            [
                {"provider": "anthropic", "status": "depleted"},
                {"provider": "openai", "status": "depleted"},
            ],
        )

        with patch(
            "ostiari_gateway.modules.llm_gateway.providers.LLMProvider.call"
        ) as provider_call:
            response = TestClient(app).post(
                "/invoke",
                json={"messages": [{"role": "user", "content": "hello"}]},
            )

        assert response.status_code == 503
        assert response.json()["limit_type"] == "broker_pool"
        provider_call.assert_not_called()

    def test_messages_shim_reroutes_direct_fallback(self, monkeypatch):
        app = _app(
            monkeypatch,
            [
                {"provider": "anthropic", "status": "depleted"},
                {"provider": "openai", "status": "active"},
            ],
        )
        fake = SimpleNamespace(
            id="cmpl-broker",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="from funded pool",
                        tool_calls=None,
                    ),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1),
        )
        from ostiari_gateway.modules.llm_gateway.messages_proxy import MessagesProxy

        with patch.object(
            MessagesProxy, "_openai_like_call", return_value=fake
        ) as openai_call:
            response = TestClient(app).post(
                "/v1/messages",
                json={
                    "model": "claude-sonnet-4-6",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )

        assert response.status_code == 200, response.text
        assert response.json()["content"][0]["text"] == "from funded pool"
        assert openai_call.call_args.args[1] == "gpt-4o"

    def test_chat_shim_surfaces_broker_depletion_as_503(self, monkeypatch):
        from ostiari_gateway.server import create_app

        app = create_app(
            initial_config=SidecarConfig(
                sidecar_id="broker-chat",
                modules=ModulesConfig(llm_gateway=True),
                llm={"default_model": "gpt-4o"},
                broker_pools=[
                    {"provider": "openai", "status": "depleted"}
                ],
            )
        )

        async def _depleted(_self, **_kwargs):
            raise BrokerPoolDepletedError(
                "All providers for model 'gpt-4o' have depleted token pools"
            )

        with patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available",
            True,
        ), patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route",
            new=_depleted,
        ):
            response = TestClient(app).post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )

        assert response.status_code == 503
        assert response.json()["error"]["type"] == "service_unavailable"

    def test_bundle_updates_and_clears_pool_state(self):
        from ostiari_gateway.server import create_app

        app = create_app(
            initial_config=SidecarConfig(
                sidecar_id="bundle-broker",
                control_plane_url="http://control-plane",
            )
        )
        assert app.state.apply_bundle is not None

        app.state.apply_bundle(
            {"broker_pools": [{"provider": "openai", "status": "depleted"}]}
        )
        assert app.state.broker_policy.blocked_providers == {"openai"}

        app.state.apply_bundle({"broker_pools": []})
        assert app.state.broker_policy.blocked_providers == set()
