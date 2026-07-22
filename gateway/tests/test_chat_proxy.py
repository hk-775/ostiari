"""Tests for the Codex/OpenAI /v1/chat/completions shim."""

from __future__ import annotations

from unittest.mock import patch

from ostiari_gateway.models import ModulesConfig, SidecarConfig
from ostiari_gateway.modules.llm_gateway.axon_router import AxonResult
from ostiari_gateway.modules.llm_gateway.chat_proxy import _openai_completion, _openai_sse
from starlette.testclient import TestClient


def _app() -> TestClient:
    from ostiari_gateway.server import create_app
    return TestClient(create_app(initial_config=SidecarConfig(
        sidecar_id="chat-test", modules=ModulesConfig(llm_gateway=True),
        llm={"default_model": "gpt-4o"})))


class TestResponseShape:
    def test_completion_from_normalized_result(self):
        res = AxonResult(content="hi there", model="gpt-4o", provider="openai",
                         input_tokens=5, output_tokens=2)
        c = _openai_completion(res)
        assert c["object"] == "chat.completion"
        assert c["choices"][0]["message"]["content"] == "hi there"
        assert c["choices"][0]["finish_reason"] == "stop"
        assert c["usage"] == {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}

    def test_completion_passthrough_raw(self):
        raw = {"id": "cmpl_1", "choices": [{"index": 0,
               "message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
               "model": "gpt-4o"}
        res = AxonResult(content="x", model="gpt-4o", provider="openai",
                         input_tokens=1, output_tokens=1, raw=raw)
        c = _openai_completion(res)
        assert c["id"] == "cmpl_1" and c["object"] == "chat.completion"

    def test_tool_calls_finish_reason(self):
        res = AxonResult(content=None, model="gpt-4o", provider="openai",
                         input_tokens=1, output_tokens=1,
                         tool_calls=[{"id": "c1", "type": "function",
                                      "function": {"name": "f", "arguments": "{}"}}])
        c = _openai_completion(res)
        assert c["choices"][0]["finish_reason"] == "tool_calls"

    def test_sse_stream_ends_with_done(self):
        completion = _openai_completion(AxonResult(
            content="stream me", model="gpt-4o", provider="openai",
            input_tokens=1, output_tokens=1))
        chunks = list(_openai_sse(completion))
        joined = "".join(chunks)
        assert joined.endswith("data: [DONE]\n\n")
        assert '"object": "chat.completion.chunk"' in joined
        assert "stream me" in joined


class TestGovernance:
    def test_missing_messages_400(self):
        c = _app()
        r = c.post("/v1/chat/completions", json={"model": "gpt-4o"})
        assert r.status_code == 400
        assert r.json()["error"]["type"] == "invalid_request_error"

    def test_agent_auth_blocks(self):
        c = _app()
        c.post("/config/agent-auth", json={
            "enabled": True, "default_grants": [],
            "agents": {"allowed": {"allowed_tools": ["/v1/chat/completions"]}}})
        r = c.post("/v1/chat/completions", headers={"X-Agent-Id": "denied"},
                   json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 403
        assert r.json()["error"]["type"] == "permission_error"

    def test_routes_through_axon_returns_openai_shape(self):
        async def _route(self_inner, **kwargs):
            return AxonResult(content="pong", model="gpt-4o", provider="openai",
                              input_tokens=3, output_tokens=1)
        c = _app()
        with patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available", True), \
             patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route", new=_route):
            r = c.post("/v1/chat/completions", headers={"X-Agent-Id": "a"},
                       json={"model": "gpt-4o", "messages": [{"role": "user", "content": "ping"}],
                             "stream": False})
        assert r.status_code == 200
        d = r.json()
        assert d["object"] == "chat.completion"
        assert d["choices"][0]["message"]["content"] == "pong"

    def test_streaming_returns_sse(self):
        async def _route(self_inner, **kwargs):
            return AxonResult(content="streamed", model="gpt-4o", provider="openai",
                              input_tokens=1, output_tokens=1)
        c = _app()
        with patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available", True), \
             patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route", new=_route):
            r = c.post("/v1/chat/completions", headers={"X-Agent-Id": "a"},
                       json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}],
                             "stream": True})
        body = r.content.decode()
        assert "chat.completion.chunk" in body
        assert "streamed" in body
        assert "[DONE]" in body

    def test_router_unavailable_503(self):
        c = _app()
        with patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available", False):
            r = c.post("/v1/chat/completions", headers={"X-Agent-Id": "a"},
                       json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 503
