"""Tests for AxonLLM embedded as Ostiari's LLM router.

The live routing tests need AxonLLM (src.gateway) importable AND its config
present; they're skipped otherwise. The adapter's normalization and the
executor's graceful-fallback behavior are tested without network.
"""

from __future__ import annotations

import pytest
from ostiari_gateway.modules.llm_gateway.axon_router import (
    AxonResult,
    AxonRouter,
    _to_result,
)
from ostiari_gateway.modules.llm_gateway.executor import _parse_args

_AXON = None
try:
    import src.gateway  # noqa: F401
    _AXON = True
except ImportError:
    _AXON = False

requires_axon = pytest.mark.skipif(not _AXON, reason="AxonLLM (src.gateway) not installed")


class TestResultNormalization:
    def test_to_result_text(self):
        out = {
            "model": "claude-sonnet", "provider": "bedrock",
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }
        r = _to_result(out)
        assert r.content == "hello"
        assert r.model == "claude-sonnet" and r.provider == "bedrock"
        assert r.input_tokens == 5 and r.output_tokens == 2

    def test_to_result_tool_calls(self):
        out = {
            "model": "m", "provider": "p",
            "choices": [{"message": {"content": None, "tool_calls": [
                {"id": "c1", "function": {"name": "f", "arguments": '{"x": 1}'}}]}}],
            "usage": {},
        }
        r = _to_result(out)
        assert r.tool_calls[0]["function"]["name"] == "f"

    def test_parse_args_variants(self):
        assert _parse_args('{"a": 1}') == {"a": 1}
        assert _parse_args({"a": 1}) == {"a": 1}
        assert _parse_args("not json") == {}
        assert _parse_args(None) == {}


class TestAvailabilityAndFallback:
    def test_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_DISABLE_AXON_ROUTER", "1")
        a = AxonRouter()
        assert a.available is False

    @pytest.mark.anyio
    async def test_route_raises_when_unavailable(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_DISABLE_AXON_ROUTER", "1")
        a = AxonRouter()
        with pytest.raises(RuntimeError):
            await a.route(messages=[{"role": "user", "content": "hi"}], model="x")

    def test_executor_falls_back_when_axon_unavailable(self, monkeypatch):
        """When Axon is unavailable, the executor uses the direct provider path."""
        from unittest.mock import patch

        from ostiari_gateway.config_manager import ConfigManager
        from ostiari_gateway.modules.llm_gateway.executor import AgenticExecutor
        from ostiari_gateway.modules.llm_gateway.models import LLMConfig
        from ostiari_gateway.modules.llm_gateway.providers import LLMResponse

        monkeypatch.setenv("OSTIARI_DISABLE_AXON_ROUTER", "1")
        ex = AgenticExecutor(config=LLMConfig(default_model="m"), manager=ConfigManager())
        assert ex._axon.available is False

        import anyio

        with patch.object(ex, "_call_with_fallback",
                          return_value=LLMResponse(content="direct", tokens_used=3, model="m")):
            async def go():
                return await ex._call_llm("m", [], [{"role": "user", "content": "hi"}], None,
                                          context={})
            res = anyio.run(go)
        assert res.content == "direct"


@requires_axon
class TestLiveRouting:
    """These build AxonLLM's real router; skipped if config/creds absent."""

    def _router(self):
        a = AxonRouter()
        if not a.available:
            pytest.skip("AxonLLM router could not be built (config/creds absent)")
        return a

    @pytest.mark.anyio
    async def test_available_from_any_cwd(self):
        a = self._router()
        assert a.available is True

    @pytest.mark.anyio
    async def test_smart_routing_selects_a_model(self):
        a = self._router()
        r = await a.route(messages=[{"role": "user", "content": "write a python function"}],
                          smart=True, max_tokens=8)
        assert isinstance(r, AxonResult) and r.model  # some model was selected


class TestToolPassThrough:
    """AxonLLM has no tool-calling pass-through — callers must not route tools to it.

    ``src.gateway.models.ChatCompletionRequest`` has no ``tools`` field and
    ``GatewayAgent._parse_request`` only reads the fields it does have, so a
    ``tools`` key in the request dict is discarded silently. The model then
    answers as if no tools exist ("I don't have access to a database") — a
    confident, fluent, wrong HTTP 200 that no error surfaces.
    """

    def test_supports_tools_reflects_the_dataclass(self):
        """Probed off AxonLLM's dataclass, not hardcoded, so it self-heals."""
        import dataclasses

        a = AxonRouter()
        try:
            from src.gateway.models import ChatCompletionRequest
        except ImportError:
            assert a.supports_tools() is False
            return
        expected = any(f.name == "tools" for f in dataclasses.fields(ChatCompletionRequest))
        assert a.supports_tools() is expected

    @requires_axon
    def test_axonllm_still_lacks_a_tools_field(self):
        """Pins the upstream gap. If this fails, AxonLLM gained tool support —
        drop the bypasses in executor/messages_proxy/chat_proxy and route again."""
        import dataclasses

        from src.gateway.models import ChatCompletionRequest
        assert not any(f.name == "tools" for f in dataclasses.fields(ChatCompletionRequest)), (
            "AxonLLM now has a tools field — re-enable AxonLLM routing for tool calls"
        )

    @pytest.mark.anyio
    async def test_route_refuses_tools_rather_than_dropping_them(self, monkeypatch):
        """route() must raise, not silently answer without the tools."""
        a = AxonRouter()
        monkeypatch.setattr(a, "supports_tools", lambda: False)
        monkeypatch.setattr(a, "_ensure", lambda: None)
        a._available = True
        a._agent = object()  # never reached — the guard fires first

        with pytest.raises(RuntimeError, match="cannot carry tool specs"):
            await a.route(
                messages=[{"role": "user", "content": "query the db"}],
                model="claude-sonnet",
                tools=[{"type": "function", "function": {"name": "db_query"}}],
            )

    @pytest.mark.anyio
    async def test_route_allows_tools_once_axon_supports_them(self, monkeypatch):
        """The guard is conditional, not a blanket ban on tools."""
        a = AxonRouter()
        monkeypatch.setattr(a, "supports_tools", lambda: True)
        monkeypatch.setattr(a, "_ensure", lambda: None)
        a._available = True

        class _Agent:
            async def handle_chat_completion(self, request_data, ctx):
                assert request_data["tools"], "tools must reach AxonLLM"
                return {"model": "m", "provider": "p", "usage": {},
                        "choices": [{"message": {"content": "ok"}}]}

        a._agent = _Agent()
        res = await a.route(messages=[{"role": "user", "content": "hi"}], model="claude-sonnet",
                            tools=[{"type": "function", "function": {"name": "db_query"}}])
        assert res.content == "ok"

    def test_executor_bypasses_axon_when_tools_are_requested(self, monkeypatch):
        """/invoke with tools must take the direct provider path, which carries them."""
        from unittest.mock import patch

        import anyio
        from ostiari_gateway.config_manager import ConfigManager
        from ostiari_gateway.modules.llm_gateway.executor import AgenticExecutor
        from ostiari_gateway.modules.llm_gateway.models import LLMConfig
        from ostiari_gateway.modules.llm_gateway.providers import LLMResponse

        ex = AgenticExecutor(config=LLMConfig(default_model="m"), manager=ConfigManager())
        # Axon is up and knows the model — the only reason to bypass is the tools.
        monkeypatch.setattr(type(ex._axon), "available", property(lambda self: True))
        monkeypatch.setattr(ex._axon, "supports_tools", lambda: False)
        monkeypatch.setattr(ex._axon, "knows_model", lambda m: True)

        async def _boom(**kwargs):
            raise AssertionError("tool call must not be routed through AxonLLM")

        monkeypatch.setattr(ex._axon, "route", _boom)

        tools = [{"name": "db_query", "description": "", "schema": {}}]
        with patch.object(ex, "_call_with_fallback",
                          return_value=LLMResponse(content="direct", tokens_used=3, model="m")) as m:
            async def go():
                return await ex._call_llm("m", [], [{"role": "user", "content": "hi"}], tools,
                                          context={})
            res = anyio.run(go)
        assert res.content == "direct"
        assert m.call_args[0][3] == tools, "the direct path must receive the tool specs"

    def test_executor_still_routes_toolless_calls_through_axon(self, monkeypatch):
        """No tools → AxonLLM keeps its routing job (smart/fallback/ensemble)."""
        import anyio
        from ostiari_gateway.config_manager import ConfigManager
        from ostiari_gateway.modules.llm_gateway.axon_router import AxonResult
        from ostiari_gateway.modules.llm_gateway.executor import AgenticExecutor
        from ostiari_gateway.modules.llm_gateway.models import LLMConfig

        ex = AgenticExecutor(config=LLMConfig(default_model="m"), manager=ConfigManager())
        monkeypatch.setattr(type(ex._axon), "available", property(lambda self: True))
        monkeypatch.setattr(ex._axon, "supports_tools", lambda: False)
        monkeypatch.setattr(ex._axon, "knows_model", lambda m: True)

        async def _routed(**kwargs):
            return AxonResult(content="routed", model="m2", provider="p",
                              input_tokens=1, output_tokens=1)

        monkeypatch.setattr(ex._axon, "route", _routed)

        async def go():
            return await ex._call_llm("m", [], [{"role": "user", "content": "hi"}], None,
                                      context={})
        assert anyio.run(go).content == "routed"


class TestToolSpecBuilding:
    """The specs handed to the model must describe the tools' real parameters."""

    def _executor(self):
        from ostiari_gateway.config_manager import ConfigManager
        from ostiari_gateway.modules.llm_gateway.executor import AgenticExecutor
        from ostiari_gateway.modules.llm_gateway.models import LLMConfig
        return AgenticExecutor(config=LLMConfig(default_model="m"), manager=ConfigManager())

    def _register(self, ex, name, schema):
        from ostiari_gateway.models import ToolDefinition
        ex._manager.tool_proxy.register(ToolDefinition(
            name=name, endpoint="http://x/t", description="d", schema=schema))

    def test_registered_schema_reaches_the_spec(self):
        """A hardcoded empty schema told the model every tool takes no arguments,
        so it could never emit a usable tool call."""
        ex = self._executor()
        schema = {"type": "object", "properties": {"sql": {"type": "string"}},
                  "required": ["sql"]}
        self._register(ex, "db_query", schema)

        spec = next(s for s in ex._build_tool_specs(["db_query"]))
        assert spec["schema"] == schema

    def test_schemaless_tool_gets_an_empty_object_schema(self):
        ex = self._executor()
        self._register(ex, "ping", None)
        assert ex._build_tool_specs(["ping"])[0]["schema"] == {
            "type": "object", "properties": {}}

    def test_filter_matching_nothing_yields_none_not_every_tool(self):
        """The empty check ran BEFORE the filter, so a non-matching filter fell
        through and offered the model every registered tool."""
        ex = self._executor()
        self._register(ex, "db_query", None)
        assert ex._build_tool_specs(["nonexistent"]) is None

    def test_no_filter_offers_everything(self):
        ex = self._executor()
        self._register(ex, "a", None)
        self._register(ex, "b", None)
        assert {s["name"] for s in ex._build_tool_specs(None)} == {"a", "b"}

    def test_tool_proxy_exposes_the_schema(self):
        """list_tools() dropped schema_ entirely — the executor had nothing to read."""
        ex = self._executor()
        schema = {"type": "object", "properties": {"to": {"type": "string"}}}
        self._register(ex, "send_email", schema)
        assert ex._manager.tool_proxy.list_tools()[0]["schema"] == schema
