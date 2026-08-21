"""Contracts for the governed OpenAI ``POST /v1/responses`` surface."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from ostiari_gateway.models import ModulesConfig, SidecarConfig
from ostiari_gateway.modules.llm_gateway.axon_router import AxonResult
from ostiari_gateway.server import create_app
from starlette.testclient import TestClient


def _app() -> TestClient:
    return TestClient(create_app(initial_config=SidecarConfig(
        sidecar_id="responses-test",
        modules=ModulesConfig(llm_gateway=True),
        llm={"default_model": "gpt-4o", "max_tokens": 1024},
    )))


def _route_result(
    *,
    content: str | None = "pong",
    tool_calls: list[dict] | None = None,
) -> AxonResult:
    return AxonResult(
        content=content,
        model="gpt-4o",
        provider="openai",
        input_tokens=3,
        output_tokens=2,
        tool_calls=tool_calls,
    )


class TestResponseShape:
    def test_text_response_uses_responses_envelope(self):
        async def _route(self_inner, **kwargs):
            return _route_result()

        client = _app()
        with patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available",
            True,
        ), patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route",
            new=_route,
        ):
            response = client.post(
                "/v1/responses",
                headers={"X-Agent-Id": "codex"},
                json={
                    "model": "gpt-4o",
                    "input": "ping",
                    "metadata": {"request": "test"},
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["id"].startswith("resp_")
        assert data["object"] == "response"
        assert data["status"] == "completed"
        assert data["model"] == "gpt-4o"
        assert data["output"][0]["type"] == "message"
        assert data["output"][0]["content"][0] == {
            "type": "output_text",
            "text": "pong",
            "annotations": [],
            "logprobs": [],
        }
        assert data["usage"] == {
            "input_tokens": 3,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 2,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 5,
        }
        assert data["metadata"] == {"request": "test"}
        assert data["store"] is False

    def test_tool_calls_become_function_call_output_items(self):
        async def _route(self_inner, **kwargs):
            return _route_result(
                content=None,
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "arguments": '{"id":"42"}',
                        },
                    }
                ],
            )

        client = _app()
        with patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available",
            True,
        ), patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route",
            new=_route,
        ):
            response = client.post(
                "/v1/responses",
                headers={"X-Agent-Id": "codex"},
                json={
                    "model": "gpt-4o",
                    "input": "look it up",
                    "tools": [
                        {
                            "type": "function",
                            "name": "lookup",
                            "parameters": {
                                "type": "object",
                                "properties": {"id": {"type": "string"}},
                            },
                        }
                    ],
                },
            )

        assert response.status_code == 200
        item = response.json()["output"][0]
        assert item["type"] == "function_call"
        assert item["call_id"] == "call_1"
        assert item["name"] == "lookup"
        assert item["arguments"] == '{"id":"42"}'

    def test_stream_uses_typed_responses_events(self):
        async def _route(self_inner, **kwargs):
            return _route_result(content="streamed")

        client = _app()
        with patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available",
            True,
        ), patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route",
            new=_route,
        ):
            response = client.post(
                "/v1/responses",
                headers={"X-Agent-Id": "codex"},
                json={"model": "gpt-4o", "input": "ping", "stream": True},
            )

        assert response.status_code == 200
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        types = [event["type"] for event in events]
        assert types[:2] == ["response.created", "response.in_progress"]
        assert "response.output_text.delta" in types
        assert types[-1] == "response.completed"
        assert [event["sequence_number"] for event in events] == list(
            range(len(events))
        )
        assert "[DONE]" not in response.text


class TestTranslation:
    def test_input_instructions_tools_and_sampling_are_forwarded(self):
        seen: dict = {}

        async def _route(self_inner, **kwargs):
            seen.update(kwargs)
            return _route_result()

        client = _app()
        with patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available",
            True,
        ), patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route",
            new=_route,
        ):
            response = client.post(
                "/v1/responses",
                headers={"X-Agent-Id": "codex"},
                json={
                    "model": "gpt-4o",
                    "instructions": "Be concise.",
                    "input": [
                        {
                            "role": "developer",
                            "content": [
                                {"type": "input_text", "text": "Use JSON."}
                            ],
                        },
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "ping"}
                            ],
                        },
                    ],
                    "max_output_tokens": 99,
                    "temperature": 0,
                    "top_p": 0.8,
                    "tools": [
                        {
                            "type": "function",
                            "name": "lookup",
                            "description": "Lookup an item.",
                            "parameters": {"type": "object", "properties": {}},
                            "strict": True,
                        }
                    ],
                    "tool_choice": {"type": "function", "name": "lookup"},
                },
            )

        assert response.status_code == 200
        assert seen["messages"][0] == {
            "role": "system",
            "content": "Be concise.\n\nUse JSON.",
        }
        assert seen["messages"][1] == {"role": "user", "content": "ping"}
        assert seen["max_tokens"] == 99
        assert seen["temperature"] == 0.0
        assert seen["top_p"] == 0.8
        assert seen["tools"][0]["function"]["name"] == "lookup"
        assert seen["tool_choice"] == {
            "type": "function",
            "function": {"name": "lookup"},
        }

    def test_function_call_round_trip_input(self):
        seen: dict = {}

        async def _route(self_inner, **kwargs):
            seen.update(kwargs)
            return _route_result()

        client = _app()
        with patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available",
            True,
        ), patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route",
            new=_route,
        ):
            response = client.post(
                "/v1/responses",
                headers={"X-Agent-Id": "codex"},
                json={
                    "model": "gpt-4o",
                    "input": [
                        {
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "lookup",
                            "arguments": {"id": "42"},
                        },
                        {
                            "type": "function_call_output",
                            "call_id": "call_1",
                            "output": {"name": "item"},
                        },
                    ],
                },
            )

        assert response.status_code == 200
        assert seen["messages"] == [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "arguments": '{"id":"42"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": '{"name":"item"}',
            },
        ]

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("previous_response_id", "resp_old"),
            ("conversation", "conv_1"),
            ("prompt", {"id": "pmpt_1"}),
            ("store", True),
            ("background", True),
            ("reasoning", {"effort": "high"}),
            ("reasoning", {"context": "current_turn"}),
            ("reasoning", {"context": "all_turns"}),
            ("text", {"format": {"type": "json_schema"}}),
            ("include", ["message.output_text.logprobs"]),
            ("max_tool_calls", 1),
            ("parallel_tool_calls", "false"),
            ("service_tier", "priority"),
        ],
    )
    def test_unsupported_stateful_or_unimplemented_fields_fail_closed(
        self,
        field,
        value,
    ):
        client = _app()
        response = client.post(
            "/v1/responses",
            headers={"X-Agent-Id": "codex"},
            json={"model": "gpt-4o", "input": "ping", field: value},
        )
        assert response.status_code == 400
        assert field in response.json()["error"]["message"]

    @pytest.mark.parametrize(
        "reasoning",
        [
            None,
            {},
            {"effort": "none"},
            {"summary": "none"},
            {"effort": "none", "summary": "none"},
        ],
    )
    def test_noop_reasoning_configuration_is_accepted(self, reasoning):
        async def _route(self_inner, **kwargs):
            return _route_result()

        client = _app()
        with patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available",
            True,
        ), patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route",
            new=_route,
        ):
            response = client.post(
                "/v1/responses",
                headers={"X-Agent-Id": "codex"},
                json={
                    "model": "gpt-4o",
                    "input": "ping",
                    "reasoning": reasoning,
                },
            )
        assert response.status_code == 200

    def test_standard_codex_transport_metadata_is_accepted(self):
        async def _route(self_inner, **kwargs):
            return _route_result()

        client = _app()
        with patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available",
            True,
        ), patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route",
            new=_route,
        ):
            response = client.post(
                "/v1/responses",
                headers={"X-Agent-Id": "codex"},
                json={
                    "model": "gpt-4o",
                    "input": "ping",
                    "reasoning": {},
                    "include": ["reasoning.encrypted_content"],
                    "parallel_tool_calls": True,
                },
            )
        assert response.status_code == 200

    def test_codex_encrypted_reasoning_transport_metadata_is_accepted(self):
        async def _route(self_inner, **kwargs):
            return _route_result()

        client = _app()
        with patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available",
            True,
        ), patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route",
            new=_route,
        ):
            response = client.post(
                "/v1/responses",
                headers={"X-Agent-Id": "codex"},
                json={
                    "model": "gpt-4o",
                    "input": "ping",
                    "reasoning": {"context": "all_turns"},
                    "include": ["reasoning.encrypted_content"],
                    "parallel_tool_calls": False,
                },
            )
        assert response.status_code == 200

    def test_single_tool_mode_fails_closed_on_multiple_upstream_calls(self):
        async def _route(self_inner, **kwargs):
            return _route_result(
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "first", "arguments": "{}"},
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "second", "arguments": "{}"},
                    },
                ]
            )

        client = _app()
        with patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available",
            True,
        ), patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route",
            new=_route,
        ):
            response = client.post(
                "/v1/responses",
                headers={"X-Agent-Id": "codex"},
                json={
                    "model": "gpt-4o",
                    "input": "ping",
                    "parallel_tool_calls": False,
                },
            )

        assert response.status_code == 502
        assert "parallel_tool_calls=false" in response.json()["error"]["message"]


class TestGovernance:
    def test_agent_auth_uses_responses_grant(self):
        client = _app()
        client.post(
            "/config/agent-auth",
            json={
                "enabled": True,
                "default_grants": [],
                "agents": {
                    "allowed": {
                        "allowed_tools": ["/v1/responses"],
                        "allowed_models": ["*"],
                    }
                },
            },
        )
        denied = client.post(
            "/v1/responses",
            headers={"X-Agent-Id": "denied"},
            json={"model": "gpt-4o", "input": "ping"},
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["type"] == "permission_error"

    def test_router_unavailable_is_503(self):
        client = _app()
        with patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available",
            False,
        ):
            response = client.post(
                "/v1/responses",
                headers={"X-Agent-Id": "codex"},
                json={"model": "gpt-4o", "input": "ping"},
            )
        assert response.status_code == 503

    def test_max_output_tokens_uses_agent_cap(self):
        seen: dict = {}

        async def _route(self_inner, **kwargs):
            seen.update(kwargs)
            return _route_result()

        client = _app()
        client.post(
            "/config/agent-auth",
            json={
                "enabled": True,
                "default_grants": [],
                "agents": {
                    "limited": {
                        "allowed_tools": ["/v1/responses"],
                        "allowed_models": ["*"],
                        "allowed_providers": ["*"],
                        "max_tokens_per_request": 64,
                    }
                },
            },
        )
        with patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available",
            True,
        ), patch(
            "ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route",
            new=_route,
        ):
            response = client.post(
                "/v1/responses",
                headers={"X-Agent-Id": "limited"},
                json={
                    "model": "gpt-4o",
                    "input": "ping",
                    "max_output_tokens": 1000,
                },
            )
        assert response.status_code == 200
        assert seen["max_tokens"] == 64
