"""Tests for the /v1/messages shim routing through AxonLLM (single authority).

When AxonLLM is available it is the routing authority for the shim; these tests
mock AxonRouter so they run without AxonLLM installed. Also covers the
result→Anthropic translation and ensemble being disabled on the shim path.
"""

from __future__ import annotations

from unittest.mock import patch

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


class TestToolsBypassAxon:
    """Claude Code always sends tools; AxonLLM silently drops them.

    ``src.gateway.models.ChatCompletionRequest`` has no ``tools`` field, so a
    tool-bearing request routed through AxonLLM comes back as prose from a model
    that was never told the tools exist — HTTP 200, no error, whole tool-use loop
    gone. The shim must take its direct Anthropic path instead.
    """

    _TOOLS = [{"name": "db_query", "description": "run sql",
               "input_schema": {"type": "object", "properties": {}}}]

    def test_tool_request_does_not_reach_axon(self, monkeypatch):
        """Records whether route() ran at all.

        Asserting only on the status code is not enough: the shim wraps its
        AxonLLM call in `except Exception` and falls back to the direct path, so
        a route() that raises produces the same 500 as never calling it. The
        flag distinguishes "bypassed" from "tried and fell back".
        """
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        called = []

        async def _route(self_inner, **kwargs):
            called.append(kwargs)
            return AxonResult(content="tool-free prose", model="m", provider="p",
                              input_tokens=1, output_tokens=1)

        c = _app()
        with patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available", True), \
             patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.supports_tools",
                   lambda self: False), \
             patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route", new=_route):
            r = c.post("/v1/messages", headers={"X-Agent-Id": "claude-code"},
                       json={"model": "claude-sonnet-4-6", "tools": self._TOOLS,
                             "messages": [{"role": "user", "content": "hi"}], "stream": False})
        assert not called, "tool-bearing call must not be routed through AxonLLM"
        # Direct path with no credential -> 500, not a tool-free 200.
        assert r.status_code == 500

    def test_toolless_request_still_routes_through_axon(self):
        """The bypass is scoped to tool requests — plain chat keeps AxonLLM routing."""
        async def _route(self_inner, **kwargs):
            return AxonResult(content="from axon", model="m", provider="p",
                              input_tokens=1, output_tokens=1)

        c = _app()
        with patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available", True), \
             patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.supports_tools",
                   lambda self: False), \
             patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route", new=_route):
            r = c.post("/v1/messages", headers={"X-Agent-Id": "claude-code"},
                       json={"model": "claude-sonnet-4-6",
                             "messages": [{"role": "user", "content": "hi"}], "stream": False})
        assert r.status_code == 200
        assert r.json()["content"][0]["text"] == "from axon"

    def test_tools_route_through_axon_once_supported(self):
        """When AxonLLM gains tool support, the shim resumes routing tool calls."""
        captured = {}

        async def _route(self_inner, **kwargs):
            captured.update(kwargs)
            return AxonResult(content="ok", model="m", provider="p",
                              input_tokens=1, output_tokens=1)

        c = _app()
        with patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available", True), \
             patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.supports_tools",
                   lambda self: True), \
             patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route", new=_route):
            r = c.post("/v1/messages", headers={"X-Agent-Id": "claude-code"},
                       json={"model": "claude-sonnet-4-6", "tools": self._TOOLS,
                             "messages": [{"role": "user", "content": "hi"}], "stream": False})
        assert r.status_code == 200
        assert captured.get("tools"), "tools must be forwarded once supported"


class TestCodexShimRefusesTools:
    """The /v1/chat/completions shim has no direct-provider fallback, so it must
    refuse tool calls outright rather than answer as if no tools existed."""

    _TOOLS = [{"type": "function", "function": {"name": "db_query", "parameters": {}}}]

    def test_tool_request_is_refused_not_silently_answered(self):
        async def _must_not_run(self_inner, **kwargs):
            raise AssertionError("tool-bearing call must not be routed through AxonLLM")

        c = _app()
        with patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available", True), \
             patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.supports_tools",
                   lambda self: False), \
             patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route",
                   new=_must_not_run):
            r = c.post("/v1/chat/completions", headers={"X-Agent-Id": "codex"},
                       json={"model": "gpt-4o", "tools": self._TOOLS,
                             "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 501
        assert "tool" in r.json()["error"]["message"].lower()

    def test_toolless_request_still_works(self):
        async def _route(self_inner, **kwargs):
            return AxonResult(content="ok", model="m", provider="p",
                              input_tokens=1, output_tokens=1)

        c = _app()
        with patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.available", True), \
             patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.supports_tools",
                   lambda self: False), \
             patch("ostiari_gateway.modules.llm_gateway.axon_router.AxonRouter.route", new=_route):
            r = c.post("/v1/chat/completions", headers={"X-Agent-Id": "codex"},
                       json={"model": "gpt-4o",
                             "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
