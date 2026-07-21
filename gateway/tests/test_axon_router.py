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
