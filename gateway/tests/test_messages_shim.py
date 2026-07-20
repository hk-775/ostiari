"""Tests for the Claude Code shim: /v1/messages interception + cross-provider routing.

Split into:
  - TestTranslate: pure Anthropic<->provider translation (no network, no SDK).
  - TestSSE: Anthropic-object -> Anthropic SSE re-emission.
  - TestGovernance: auth/injection/quota gates (no network).
  - TestAnthropicPassthrough: forward to Anthropic with a mocked httpx client.
  - TestCrossProviderRouting: routed to OpenAI, provider call mocked, response
    returned in Anthropic Messages format.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from ostiari_gateway.models import ModulesConfig, SidecarConfig
from ostiari_gateway.modules.llm_gateway import translate as T
from starlette.testclient import TestClient


def _app(llm: dict | None = None) -> TestClient:
    from ostiari_gateway.server import create_app
    config = SidecarConfig(
        sidecar_id="shim-test",
        modules=ModulesConfig(llm_gateway=True),
        llm=llm or {"default_model": "claude-sonnet-4-6"},
    )
    return TestClient(create_app(initial_config=config))


# ── translation ─────────────────────────────────────────────────────────────

class TestTranslate:
    def test_flatten_system_string_and_blocks(self):
        assert T.flatten_system("hello") == "hello"
        assert T.flatten_system([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"

    def test_text_of_blocks(self):
        assert T.text_of("plain") == "plain"
        assert T.text_of([{"type": "text", "text": "x"}, {"type": "tool_use", "name": "t"}]) == "x"

    def test_anthropic_to_openai_simple(self):
        out = T.anthropic_to_openai_messages("sys", [{"role": "user", "content": "hi"}])
        assert out[0] == {"role": "system", "content": "sys"}
        assert out[1] == {"role": "user", "content": "hi"}

    def test_tool_use_round_trip_to_openai(self):
        # assistant tool_use -> tool_calls; user tool_result -> role:tool message
        messages = [
            {"role": "user", "content": "delete file"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "sure"},
                {"type": "tool_use", "id": "tu_1", "name": "fs.delete", "input": {"path": "/a"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"},
            ]},
        ]
        out = T.anthropic_to_openai_messages(None, messages)
        assistant = next(m for m in out if m["role"] == "assistant")
        assert assistant["tool_calls"][0]["function"]["name"] == "fs_delete"  # dot sanitized
        assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"path": "/a"}
        tool_msg = next(m for m in out if m["role"] == "tool")
        assert tool_msg["tool_call_id"] == "tu_1" and tool_msg["content"] == "ok"

    def test_tools_translation_and_name_map(self):
        tools = [{"name": "fs.delete", "description": "d", "input_schema": {"type": "object"}}]
        oai, name_map = T.anthropic_tools_to_openai(tools)
        assert oai[0]["function"]["name"] == "fs_delete"
        assert name_map["fs_delete"] == "fs.delete"

    def test_openai_response_to_anthropic_text(self):
        resp = SimpleNamespace(
            id="cmpl_1",
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="hello", tool_calls=None),
                finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=3),
        )
        msg = T.openai_response_to_anthropic(resp, "gpt-4o")
        assert msg["type"] == "message" and msg["role"] == "assistant"
        assert msg["content"][0] == {"type": "text", "text": "hello"}
        assert msg["stop_reason"] == "end_turn"
        assert msg["usage"] == {"input_tokens": 10, "output_tokens": 3}

    def test_openai_response_to_anthropic_tool_use(self):
        tc = SimpleNamespace(id="call_1",
                             function=SimpleNamespace(name="fs_delete", arguments='{"path":"/a"}'))
        resp = SimpleNamespace(
            id="cmpl_2",
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=None, tool_calls=[tc]),
                finish_reason="tool_calls")],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=8),
        )
        msg = T.openai_response_to_anthropic(resp, "gpt-4o", {"fs_delete": "fs.delete"})
        block = msg["content"][0]
        assert block["type"] == "tool_use" and block["name"] == "fs.delete"  # restored
        assert block["input"] == {"path": "/a"}
        assert msg["stop_reason"] == "tool_use"


# ── SSE re-emission ───────────────────────────────────────────────────────────

class TestSSE:
    def test_text_message_sse_sequence(self):
        msg = {"id": "m1", "type": "message", "role": "assistant", "model": "gpt-4o",
               "content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
               "stop_sequence": None, "usage": {"input_tokens": 4, "output_tokens": 2}}
        events = list(T.anthropic_message_to_sse(msg))
        joined = "".join(events)
        # Correct ordered event set that the Anthropic SDK expects
        for evt in ("message_start", "content_block_start", "content_block_delta",
                    "content_block_stop", "message_delta", "message_stop"):
            assert f"event: {evt}" in joined
        # text delta carries the content
        assert '"text":"hi"' in joined.replace(" ", "")

    def test_tool_use_sse_has_input_json_delta(self):
        msg = {"id": "m2", "type": "message", "role": "assistant", "model": "gpt-4o",
               "content": [{"type": "tool_use", "id": "tu", "name": "fs.delete", "input": {"path": "/a"}}],
               "stop_reason": "tool_use", "stop_sequence": None,
               "usage": {"input_tokens": 1, "output_tokens": 1}}
        joined = "".join(T.anthropic_message_to_sse(msg))
        assert "input_json_delta" in joined
        assert "tool_use" in joined


# ── governance gates ─────────────────────────────────────────────────────────

class TestGovernance:
    def test_missing_messages_is_400(self):
        c = _app()
        r = c.post("/v1/messages", json={"model": "claude-sonnet-4-6"})
        assert r.status_code == 400
        assert r.json()["error"]["type"] == "invalid_request_error"

    def test_no_credential_is_500(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        c = _app()
        r = c.post("/v1/messages",
                   json={"model": "claude-sonnet-4-6",
                         "messages": [{"role": "user", "content": "hi"}], "stream": False})
        assert r.status_code == 500
        assert "credential" in r.json()["error"]["message"].lower()

    def test_agent_auth_blocks(self):
        c = _app()
        # configure agent auth to deny an agent for /v1/messages
        c.post("/config/agent-auth", json={
            "enabled": True, "default_allow": False,
            "grants": {"allowed-agent": ["/v1/messages"]},
        })
        r = c.post("/v1/messages",
                   headers={"X-Agent-Id": "denied-agent"},
                   json={"model": "claude-sonnet-4-6",
                         "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 403
        assert r.json()["error"]["type"] == "permission_error"


# ── Anthropic passthrough (mocked upstream) ──────────────────────────────────

def _mock_async_client(handler):
    """Return an httpx.AsyncClient subclass whose .post uses a MockTransport handler."""
    class _Client(httpx.AsyncClient):
        def __init__(self, *a, **k):
            k["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **k)
    return _Client


class TestAnthropicPassthrough:
    def test_non_streaming_forward(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/messages"
            assert request.headers["x-api-key"] == "sk-test"
            return httpx.Response(200, json={
                "id": "msg_1", "type": "message", "role": "assistant",
                "content": [{"type": "text", "text": "pong"}],
                "usage": {"input_tokens": 3, "output_tokens": 1},
            })

        with patch("ostiari_gateway.modules.llm_gateway.messages_proxy.httpx.AsyncClient",
                   _mock_async_client(handler)):
            c = _app()
            r = c.post("/v1/messages",
                       json={"model": "claude-sonnet-4-6",
                             "messages": [{"role": "user", "content": "ping"}], "stream": False})
        assert r.status_code == 200
        assert r.json()["content"][0]["text"] == "pong"

    def test_streaming_relays_sse(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        sse = (b'event: message_start\ndata: {"type":"message_start",'
               b'"message":{"usage":{"input_tokens":5}}}\n\n'
               b'event: message_stop\ndata: {"type":"message_stop"}\n\n')

        class _Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield sse

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=_Stream(),
                                  headers={"content-type": "text/event-stream"})

        with patch("ostiari_gateway.modules.llm_gateway.messages_proxy.httpx.AsyncClient",
                   _mock_async_client(handler)):
            c = _app()
            r = c.post("/v1/messages",
                       json={"model": "claude-sonnet-4-6",
                             "messages": [{"role": "user", "content": "ping"}], "stream": True})
        assert r.status_code == 200
        body = r.content.decode()
        assert "event: message_start" in body and "event: message_stop" in body


# ── cross-provider routing ────────────────────────────────────────────────────

class TestCrossProviderRouting:
    def test_routed_to_openai_returns_anthropic_format(self):
        """A routing rule sends the request to gpt-4o; provider call is mocked,
        and the shim must return an Anthropic-shaped Messages object."""
        c = _app(llm={
            "default_model": "claude-sonnet-4-6",
            "routing_rules": [{"condition": "route_openai == 'yes'", "model": "gpt-4o"}],
        })
        # Force the router to pick gpt-4o regardless of content.
        from ostiari_gateway.modules.llm_gateway.messages_proxy import MessagesProxy

        fake = SimpleNamespace(
            id="cmpl_x",
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="from openai", tool_calls=None),
                finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2),
        )
        with patch.object(MessagesProxy, "_route", return_value="gpt-4o"), \
             patch.object(MessagesProxy, "_openai_like_call", return_value=fake):
            r = c.post("/v1/messages",
                       json={"model": "claude-sonnet-4-6",
                             "messages": [{"role": "user", "content": "hi"}], "stream": False})
        assert r.status_code == 200
        data = r.json()
        assert data["type"] == "message"
        assert data["content"][0]["text"] == "from openai"
        assert data["usage"] == {"input_tokens": 7, "output_tokens": 2}

    def test_routed_to_openai_streams_anthropic_sse(self):
        c = _app()
        from ostiari_gateway.modules.llm_gateway.messages_proxy import MessagesProxy

        fake = SimpleNamespace(
            id="cmpl_y",
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="streamed", tool_calls=None),
                finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )
        with patch.object(MessagesProxy, "_route", return_value="gpt-4o"), \
             patch.object(MessagesProxy, "_openai_like_call", return_value=fake):
            r = c.post("/v1/messages",
                       json={"model": "claude-sonnet-4-6",
                             "messages": [{"role": "user", "content": "hi"}], "stream": True})
        assert r.status_code == 200
        body = r.content.decode()
        assert "event: message_start" in body
        assert "streamed" in body
        assert "event: message_stop" in body
