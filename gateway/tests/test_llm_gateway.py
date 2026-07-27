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


class TestInvokeBadRequests:
    """A malformed or wrong-shaped body is the CALLER's error.

    Both used to escape as unhandled exceptions: `/invoke` parses the body by
    hand rather than declaring a typed parameter, so FastAPI's own 422 handler
    never sees it and the client got a 500 with a stack trace in the gateway log
    — indistinguishable from a real gateway fault, and with nothing actionable
    in the response.
    """

    @pytest.fixture
    def client(self):
        from ostiari_gateway.server import create_app

        config = SidecarConfig(
            sidecar_id="test-badreq",
            modules=ModulesConfig(llm_gateway=True),
            llm={"default_model": "claude-sonnet-4-6"},
        )
        return TestClient(create_app(initial_config=config))

    def test_malformed_json_is_400(self, client):
        resp = client.post("/invoke", content=b"{not json",
                           headers={"Content-Type": "application/json"})
        assert resp.status_code == 400
        assert "Malformed JSON" in resp.json()["error"]

    def test_non_object_body_is_400(self, client):
        resp = client.post("/invoke", json="just a string")
        assert resp.status_code == 400

    def test_wrong_field_name_is_422_naming_the_field(self, client):
        """The real-world case: a client sends `prompt` instead of `messages`."""
        resp = client.post("/invoke", json={"prompt": "hello"})
        assert resp.status_code == 422
        body = resp.json()
        # The response must say WHICH field is wrong, or the caller is guessing.
        assert "messages" in str(body["detail"])

    def test_empty_messages_is_422(self, client):
        # InvokeRequest declares min_length=1 on messages.
        resp = client.post("/invoke", json={"messages": []})
        assert resp.status_code == 422

    def test_validation_detail_omits_the_input_and_docs_url(self, client):
        """Echoing the rejected input back can leak whatever the caller sent
        (prompts, credentials pasted into a field) into logs and error surfaces."""
        resp = client.post("/invoke", json={"prompt": "sk-secret-value-do-not-echo"})
        raw = resp.text
        assert "sk-secret-value-do-not-echo" not in raw
        assert "errors.pydantic.dev" not in raw


class TestInvokeReportsCacheHit:
    """The response's `cache_hit` must agree with /cache/stats.

    A cached plan always HAS tool calls, so round 0 executes them and the loop
    can only return from round 1+. The old flag was
    `cached_plan is not None and round_num == 0 and total_tokens == 0`, computed
    from a variable that round 0 had already reset to False — so both the
    `round_num == 0` and `total_tokens == 0` terms were unreachable on exactly
    the path a cache hit takes. `/cache/stats` counted the hit and the token
    count dropped, while the caller was told cache_hit=false.
    """

    @pytest.fixture(autouse=True)
    def _disable_axon(self, monkeypatch):
        # Exercise the direct provider path so _call_with_fallback is the seam.
        monkeypatch.setenv("OSTIARI_DISABLE_AXON_ROUTER", "1")

    @pytest.fixture
    def client(self, httpserver):
        from ostiari_gateway.server import create_app

        httpserver.expect_request("/send", method="POST").respond_with_json({"id": "m-1"})
        config = SidecarConfig(
            sidecar_id="test-cache",
            modules=ModulesConfig(llm_gateway=True),
            llm={"default_model": "claude-sonnet-4-6", "max_tool_rounds": 3},
            tools=[ToolDefinition(name="send_email", endpoint=httpserver.url_for("/send"),
                                  description="Send an email")],
        )
        return TestClient(create_app(initial_config=config))

    @staticmethod
    def _tool_then_text():
        """LLM stub: ask for a tool, then answer once results come back.

        Keyed on conversation state, not a call counter — a cache hit skips
        round 0's LLM call, so counter parity shifts and an alternating stub
        would hand out a second tool call on the follow-up round.
        """
        from ostiari_gateway.modules.llm_gateway.providers import LLMResponse, ToolCall

        calls = [0]

        def mock_call(primary, fallback_chain, messages, tools, max_tokens=None, **kw):
            calls[0] += 1
            has_results = any(
                isinstance(m.get("content"), list)
                and any(b.get("type") == "tool_result" for b in m["content"])
                for m in messages
            )
            if has_results:
                return LLMResponse(content="Sent.", tokens_used=50, model="claude-sonnet-4-6")
            return LLMResponse(
                content="", tokens_used=100, model="claude-sonnet-4-6",
                tool_calls=[ToolCall(id="tc-1", name="send_email",
                                     arguments={"to": "a@b.com"})])

        return mock_call, calls

    def _invoke(self, client, session="s-1"):
        return client.post(
            "/invoke",
            json={"messages": [{"role": "user", "content": "Email a@b.com"}]},
            headers={"X-Agent-Id": "cache-agent", "X-Session-Id": session},
        ).json()

    def test_second_identical_call_reports_the_hit(self, client):
        mock_call, calls = self._tool_then_text()
        target = "ostiari_gateway.modules.llm_gateway.executor.AgenticExecutor._call_with_fallback"
        with patch(target, side_effect=mock_call):
            first = self._invoke(client)
            second = self._invoke(client)

        assert first["cache_hit"] is False, "nothing cached yet on the first call"
        assert second["cache_hit"] is True, "plan was reused but the caller wasn't told"
        # The reported flag must not contradict the counter behind /cache/stats.
        assert client.get("/cache/stats").json()["hits"] == 1
        # 3 LLM calls, not 4: the second call skipped round 0 entirely.
        assert calls[0] == 3
        assert second["total_tokens"] < first["total_tokens"]

    def test_missing_session_id_never_caches(self, client):
        """Both get() and put() no-op on an empty session_id, so a caller that
        omits X-Session-Id can never get a hit — the reason repeated identical
        curls all reported cache_hit=false."""
        mock_call, _ = self._tool_then_text()
        target = "ostiari_gateway.modules.llm_gateway.executor.AgenticExecutor._call_with_fallback"
        with patch(target, side_effect=mock_call):
            for _ in range(2):
                body = client.post(
                    "/invoke",
                    json={"messages": [{"role": "user", "content": "Email a@b.com"}]},
                    headers={"X-Agent-Id": "cache-agent"},  # no X-Session-Id
                ).json()
                assert body["cache_hit"] is False

        stats = client.get("/cache/stats").json()
        assert stats["entries"] == 0 and stats["hits"] == 0

    def test_a_different_session_is_a_miss(self, client):
        """The key is per-agent, per-session — a new session must not inherit."""
        mock_call, _ = self._tool_then_text()
        target = "ostiari_gateway.modules.llm_gateway.executor.AgenticExecutor._call_with_fallback"
        with patch(target, side_effect=mock_call):
            self._invoke(client, session="s-1")
            other = self._invoke(client, session="s-2")
        assert other["cache_hit"] is False

    def test_cache_hit_still_reruns_tools_and_answers(self, client):
        """A reported hit must not mean a truncated result: the cached plan's
        tools still execute and round 1 still produces the final text."""
        mock_call, _ = self._tool_then_text()
        target = "ostiari_gateway.modules.llm_gateway.executor.AgenticExecutor._call_with_fallback"
        with patch(target, side_effect=mock_call):
            self._invoke(client)
            second = self._invoke(client)

        assert second["cache_hit"] is True
        assert second["response"] == "Sent."
        assert [tc["name"] for tc in second["tool_calls"]] == ["send_email"]
        assert second["rounds"] == 2
