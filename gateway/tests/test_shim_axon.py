"""Tests for the /v1/messages shim routing through AxonLLM (single authority).

When AxonLLM is available it is the routing authority for the shim; these tests
mock AxonRouter so they run without AxonLLM installed. Also covers the
result→Anthropic translation and ensemble being disabled on the shim path.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from ostiari_gateway.models import ModulesConfig, SidecarConfig
from ostiari_gateway.modules.llm_gateway.axon_router import AxonResult
from ostiari_gateway.modules.llm_gateway.messages_proxy import _axon_result_to_anthropic
from starlette.testclient import TestClient


def _app() -> TestClient:
    from ostiari_gateway.server import create_app
    return TestClient(create_app(initial_config=SidecarConfig(
        sidecar_id="shim-axon", modules=ModulesConfig(llm_gateway=True),
        llm={"default_model": "claude-sonnet-4-6"})))


class TestResultTranslation:
    def test_text_result_to_anthropic(self):
        res = AxonResult(content="hello", model="claude-sonnet", provider="bedrock",
                         input_tokens=5, output_tokens=2)
        msg = _axon_result_to_anthropic(res, None)
        assert msg["type"] == "message" and msg["role"] == "assistant"
        assert msg["content"][0] == {"type": "text", "text": "hello"}
        assert msg["stop_reason"] == "end_turn"
        assert msg["usage"] == {"input_tokens": 5, "output_tokens": 2}

    def test_tool_call_result_restores_dotted_name(self):
        res = AxonResult(content=None, model="m", provider="p", input_tokens=1, output_tokens=1,
                         tool_calls=[{"id": "c1", "function": {"name": "fs_delete", "arguments": '{"path":"/a"}'}}])
        msg = _axon_result_to_anthropic(res, [{"name": "fs.delete"}])
        block = msg["content"][0]
        assert block["type"] == "tool_use"
        assert block["name"] == "fs.delete"          # dotted name restored
        assert block["input"] == {"path": "/a"}
        assert msg["stop_reason"] == "tool_use"


class TestShimThroughAxon:
    def test_shim_routes_through_axon_and_returns_anthropic(self):
        async def _route(self_inner, **kwargs):
            return AxonResult(content="from axon", model="claude-haiku", provider="bedrock",
                              input_tokens=7, output_tokens=3)
        c = _app()
        with patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available", True), \
             patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route", new=_route):
            r = c.post("/v1/messages", headers={"X-Agent-Id": "claude-code"},
                       json={"model": "claude-sonnet-4-6",
                             "messages": [{"role": "user", "content": "hi"}], "stream": False})
        assert r.status_code == 200
        d = r.json()
        assert d["type"] == "message"
        assert d["content"][0]["text"] == "from axon"
        assert d["usage"] == {"input_tokens": 7, "output_tokens": 3}

    def test_shim_never_requests_ensemble(self):
        captured = {}

        async def _route(self_inner, **kwargs):
            captured.update(kwargs)
            return AxonResult(content="ok", model="m", provider="p", input_tokens=1, output_tokens=1)

        c = _app()
        with patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available", True), \
             patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route", new=_route):
            c.post("/v1/messages", headers={"X-Agent-Id": "claude-code"},
                   json={"model": "", "messages": [{"role": "user", "content": "hi"}]})
        # ensemble must always be False on the shim; empty model => smart auto-select
        assert captured.get("ensemble") is False
        assert captured.get("smart") is True

    def test_streaming_emits_anthropic_sse(self):
        async def _route(self_inner, **kwargs):
            return AxonResult(content="streamed", model="m", provider="p",
                              input_tokens=1, output_tokens=1)
        c = _app()
        with patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available", True), \
             patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route", new=_route):
            r = c.post("/v1/messages", headers={"X-Agent-Id": "claude-code"},
                       json={"model": "claude-sonnet-4-6",
                             "messages": [{"role": "user", "content": "hi"}], "stream": True})
        body = r.content.decode()
        assert "event: message_start" in body
        assert "streamed" in body
        assert "event: message_stop" in body


class TestClaudeCodeShape:
    def test_block_list_content_routes_through_axon(self):
        """Claude Code sends content as a list of blocks + system as blocks.
        The shim must normalize these before AxonLLM (no 'list'.lower crash)."""
        captured = {}

        async def _route(self_inner, **kwargs):
            captured.update(kwargs)
            return AxonResult(content="ok", model="m", provider="p",
                              input_tokens=1, output_tokens=1)
        c = _app()
        with patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available", True), \
             patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route", new=_route):
            r = c.post("/v1/messages",
                       headers={"x-claude-code-session-id": "cc-sess-1"},
                       json={"model": "claude-sonnet-4-6", "max_tokens": 8,
                             "system": [{"type": "text", "text": "be brief"}],
                             "messages": [{"role": "user",
                                           "content": [{"type": "text", "text": "hi"}]}]})
        assert r.status_code == 200
        # messages passed to Axon must be OpenAI-shaped string content, not blocks
        msgs = captured.get("messages", [])
        assert all(isinstance(m.get("content"), str) for m in msgs if m.get("content") is not None)
        # session id captured from the Claude Code header
        assert captured.get("session_id") == "cc-sess-1"


class TestFallbackWhenAxonAbsent:
    def test_shim_uses_direct_path_when_axon_unavailable(self, monkeypatch):
        # With Axon disabled, the shim should hit the direct path (500 for no cred)
        monkeypatch.setenv("OSTIARI_DISABLE_AXON_ROUTER", "1")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        c = _app()
        r = c.post("/v1/messages", headers={"X-Agent-Id": "claude-code"},
                   json={"model": "claude-sonnet-4-6",
                         "messages": [{"role": "user", "content": "hi"}], "stream": False})
        # direct path with no credential -> 500 (proves it did NOT route through axon)
        assert r.status_code == 500
