"""Tests for the LLM Gateway module."""

from unittest.mock import patch

import pytest
from ostiari_gateway.models import ModulesConfig, PolicyConfig, SidecarConfig, ToolDefinition
from ostiari_gateway.modules.llm_gateway.models import LLMConfig, RoutingRule
from ostiari_gateway.modules.llm_gateway.router import ModelRouter
from starlette.testclient import TestClient


class TestModelRouter:
    def test_default_model(self):
        config = LLMConfig(default_model="claude-sonnet-4-6")
        router = ModelRouter(config)
        assert router.select_model({}) == "claude-sonnet-4-6"

    def test_routing_rule_equals(self):
        config = LLMConfig(
            default_model="claude-sonnet-4-6",
            routing_rules=[
                RoutingRule(condition="task_type == 'code'", model="claude-sonnet-4-6"),
                RoutingRule(condition="task_type == 'chat'", model="claude-haiku-4-5"),
            ],
        )
        router = ModelRouter(config)
        assert router.select_model({"task_type": "code"}) == "claude-sonnet-4-6"
        assert router.select_model({"task_type": "chat"}) == "claude-haiku-4-5"
        assert router.select_model({"task_type": "other"}) == "claude-sonnet-4-6"

    def test_routing_rule_greater_than(self):
        config = LLMConfig(
            default_model="claude-sonnet-4-6",
            routing_rules=[
                RoutingRule(condition="estimated_tokens > 50000", model="claude-haiku-4-5"),
            ],
        )
        router = ModelRouter(config)
        assert router.select_model({"estimated_tokens": 100000}) == "claude-haiku-4-5"
        assert router.select_model({"estimated_tokens": 1000}) == "claude-sonnet-4-6"

    def test_routing_rule_boolean_flag(self):
        config = LLMConfig(
            default_model="claude-sonnet-4-6",
            routing_rules=[
                RoutingRule(condition="cost_budget_exceeded", model="claude-haiku-4-5"),
            ],
        )
        router = ModelRouter(config)
        assert router.select_model({"cost_budget_exceeded": True}) == "claude-haiku-4-5"
        assert router.select_model({"cost_budget_exceeded": False}) == "claude-sonnet-4-6"
        assert router.select_model({}) == "claude-sonnet-4-6"

    def test_fallback_chain(self):
        config = LLMConfig(
            fallback_chain=["claude-sonnet-4-6", "gpt-4o", "claude-haiku-4-5"]
        )
        router = ModelRouter(config)
        assert router.get_fallback_chain("claude-sonnet-4-6") == ["gpt-4o", "claude-haiku-4-5"]
        assert router.get_fallback_chain("gpt-4o") == ["claude-haiku-4-5"]
        assert router.get_fallback_chain("unknown") == ["claude-sonnet-4-6", "gpt-4o", "claude-haiku-4-5"]


class TestModuleActivation:
    def test_module_not_active_by_default(self):
        from ostiari_gateway.server import create_app

        config = SidecarConfig(sidecar_id="test")
        app = create_app(initial_config=config)
        client = TestClient(app)

        # /invoke should not exist
        resp = client.post("/invoke", json={"messages": [{"role": "user", "content": "hi"}]})
        assert resp.status_code == 404 or resp.status_code == 405

    def test_health_shows_modules(self):
        from ostiari_gateway.server import create_app

        config = SidecarConfig(sidecar_id="test")
        app = create_app(initial_config=config)
        client = TestClient(app)

        resp = client.get("/health")
        data = resp.json()
        assert "modules_active" in data
        assert "modules_available" in data
        assert data["modules_active"] == []

    def test_modules_endpoint(self):
        from ostiari_gateway.server import create_app

        config = SidecarConfig(sidecar_id="test")
        app = create_app(initial_config=config)
        client = TestClient(app)

        resp = client.get("/modules")
        data = resp.json()
        assert "active" in data
        assert "available" in data
        assert any(m["name"] == "llm_gateway" for m in data["available"])

    def test_llm_gateway_activates_invoke_endpoint(self):
        from ostiari_gateway.server import create_app

        config = SidecarConfig(
            sidecar_id="test",
            modules=ModulesConfig(llm_gateway=True),
            llm={"default_model": "claude-sonnet-4-6"},
        )
        app = create_app(initial_config=config)
        client = TestClient(app)

        # /models should exist
        resp = client.get("/models")
        assert resp.status_code == 200
        assert resp.json()["default_model"] == "claude-sonnet-4-6"

        # /health should show llm_gateway as active
        resp = client.get("/health")
        assert "llm_gateway" in resp.json()["modules_active"]


class TestLLMGatewayInvoke:
    @pytest.fixture(autouse=True)
    def _disable_axon(self, monkeypatch):
        # These tests mock the direct provider path to exercise the agentic loop
        # deterministically; disable the AxonLLM router so that path runs.
        monkeypatch.setenv("OSTIARI_DISABLE_AXON_ROUTER", "1")

    @pytest.fixture
    def client_with_mock_llm(self, httpserver):
        """Client with LLM Gateway active and a mocked LLM provider."""

        from ostiari_gateway.server import create_app

        # Mock tool endpoint
        httpserver.expect_request("/send", method="POST").respond_with_json(
            {"message_id": "msg-456"}
        )

        config = SidecarConfig(
            sidecar_id="test-llm",
            modules=ModulesConfig(llm_gateway=True),
            llm={"default_model": "claude-sonnet-4-6", "max_tool_rounds": 3},
            tools=[
                ToolDefinition(
                    name="send_email",
                    endpoint=httpserver.url_for("/send"),
                    description="Send an email",
                ),
            ],
            policy=PolicyConfig(block=["dangerous_action"]),
        )
        app = create_app(initial_config=config)
        return TestClient(app)

    def test_invoke_without_tools_returns_response(self, client_with_mock_llm):
        """Test that /invoke calls the LLM and returns a response."""
        from ostiari_gateway.modules.llm_gateway.providers import LLMResponse

        mock_response = LLMResponse(content="Hello! How can I help?", tokens_used=50, model="claude-sonnet-4-6")

        with patch(
            "ostiari_gateway.modules.llm_gateway.executor.AgenticExecutor._call_with_fallback",
            return_value=mock_response,
        ):
            resp = client_with_mock_llm.post(
                "/invoke",
                json={"messages": [{"role": "user", "content": "Say hello"}]},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["response"] == "Hello! How can I help?"
            assert data["model_used"] == "claude-sonnet-4-6"
            assert data["rounds"] == 1

    def test_invoke_with_tool_call(self, client_with_mock_llm):
        """Test the full agentic loop: LLM → tool call → result → final response."""
        from ostiari_gateway.modules.llm_gateway.providers import LLMResponse, ToolCall

        call_count = [0]

        def mock_call(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCall(id="tc-1", name="send_email", arguments={"to": "boss@co.com", "body": "hi"})],
                    tokens_used=100,
                    model="claude-sonnet-4-6",
                )
            else:
                return LLMResponse(
                    content="Done! I sent the email.",
                    tokens_used=50,
                    model="claude-sonnet-4-6",
                )

        with patch(
            "ostiari_gateway.modules.llm_gateway.executor.AgenticExecutor._call_with_fallback",
            side_effect=mock_call,
        ):
            resp = client_with_mock_llm.post(
                "/invoke",
                json={"messages": [{"role": "user", "content": "Email my boss"}]},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["response"] == "Done! I sent the email."
            assert len(data["tool_calls"]) == 1
            assert data["tool_calls"][0]["name"] == "send_email"
            assert data["rounds"] == 2
            assert data["total_tokens"] == 150

    def test_invoke_with_blocked_tool(self, client_with_mock_llm):
        """Test that blocked tool calls are fed back to the LLM."""
        from ostiari_gateway.modules.llm_gateway.providers import LLMResponse, ToolCall

        call_count = [0]

        def mock_call(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCall(id="tc-1", name="dangerous_action", arguments={"target": "all"})],
                    tokens_used=100,
                    model="claude-sonnet-4-6",
                )
            else:
                return LLMResponse(
                    content="I can't do that due to policy restrictions.",
                    tokens_used=50,
                    model="claude-sonnet-4-6",
                )

        with patch(
            "ostiari_gateway.modules.llm_gateway.executor.AgenticExecutor._call_with_fallback",
            side_effect=mock_call,
        ):
            # Register the tool so it exists
            client_with_mock_llm.post(
                "/config/tools/dangerous_action",
                json={"endpoint": "http://localhost:9999/x"},
            )
            resp = client_with_mock_llm.post(
                "/invoke",
                json={"messages": [{"role": "user", "content": "Do something dangerous"}]},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["response"] == "I can't do that due to policy restrictions."
            assert len(data["blocked_actions"]) == 1
            assert data["blocked_actions"][0]["action"] == "dangerous_action"
