"""Tests for AxonLLM embedded as Ostiari's LLM router.

AxonLLM owns routing governance and token cost tracking, so ``TestAxonIsRequired``
covers the gateway warning loudly when it starts without it — and refusing to
start when ``OSTIARI_REQUIRE_AXON=1``. ``TestToolPassThrough`` covers tool specs
reaching it rather than being routed around. The live routing tests need AxonLLM
(src.gateway) importable AND its config present; they're skipped otherwise. Result
normalization and the mid-flight fallback are tested without network.
"""

from __future__ import annotations

import pytest
from ostiari_gateway.modules.llm_gateway.axon_router import (
    AxonResult,
    AxonRouter,
    _to_result,
)
from ostiari_gateway.modules.llm_gateway.executor import _parse_args


def _axon_importable() -> bool:
    """Whether AxonLLM's ``src.gateway`` can be imported here.

    Must prepare sys.path the way AxonRouter._ensure does. A bare
    ``import src.gateway`` always fails — AxonLLM's editable install exposes
    ``<root>/src``, not ``<root>`` — so these tests skipped unconditionally and
    the version guard below never actually ran.
    """
    import sys

    from ostiari_gateway.modules.llm_gateway.axon_router import _axon_root

    root = _axon_root()
    if root and root not in sys.path:
        sys.path.insert(0, root)
    try:
        import src.gateway  # noqa: F401
        return True
    except ImportError:
        return False


requires_axon = pytest.mark.skipif(not _axon_importable(),
                                   reason="AxonLLM (src.gateway) not installed")


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


class TestAxonIsRequired:
    """AxonLLM is optional to install but load-bearing when absent.

    The direct-provider fallback works well enough that a gateway with no
    AxonLLM serves traffic and reports healthy — which is exactly how it ran
    unnoticed while no routing governance or token cost tracking applied. That
    invisibility is the defect, so the check stays and startup says so out loud;
    what it does *not* do is refuse to boot, because AxonLLM is a separate
    private repo and a hard requirement made it a deployment dependency of
    gateways that never make an LLM call. ``OSTIARI_REQUIRE_AXON=1`` opts back
    into refusing.
    """

    def test_require_raises_when_disabled(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_DISABLE_AXON_ROUTER", "1")
        with pytest.raises(RuntimeError, match="OSTIARI_DISABLE_AXON_ROUTER"):
            AxonRouter().require()

    def test_require_raises_when_axon_cannot_be_built(self, monkeypatch):
        a = AxonRouter()
        monkeypatch.setattr(a, "_ensure", lambda: None)
        a._available = False
        a._error = "ModuleNotFoundError: No module named 'src'"
        with pytest.raises(RuntimeError, match="could not be embedded"):
            a.require()

    def test_require_is_a_noop_when_embedded(self, monkeypatch):
        a = AxonRouter()
        monkeypatch.setattr(a, "_ensure", lambda: None)
        a._available = True
        a.require()      # must not raise

    def test_error_keeps_the_exception_class(self, monkeypatch):
        """"unavailable" alone hides whether to install AxonLLM or fix its config."""
        a = AxonRouter()
        monkeypatch.setenv("OSTIARI_AXON_ROOT", "/nonexistent")
        import ostiari_gateway.modules.llm_gateway.axon_router as mod
        monkeypatch.setattr(mod, "_axon_root", lambda: None)
        assert a.available is False
        assert "Error" in a.error or "error" in a.error.lower()

    def test_gateway_starts_without_axon_but_warns(self, monkeypatch, caplog):
        """Default: boot, and say plainly what is no longer being enforced.

        A silent start is the failure mode this whole class exists for, so the
        warning has to name the two things that stopped applying — an operator
        reading "AxonLLM unavailable" alone has no reason to treat it as urgent.
        """
        import logging

        from ostiari_gateway.models import ModulesConfig, SidecarConfig
        from ostiari_gateway.server import create_app

        monkeypatch.setenv("OSTIARI_DISABLE_AXON_ROUTER", "1")
        monkeypatch.delenv("OSTIARI_REQUIRE_AXON", raising=False)
        with caplog.at_level(logging.WARNING):
            app = create_app(initial_config=SidecarConfig(
                sidecar_id="ungoverned", modules=ModulesConfig(llm_gateway=True),
                llm={"default_model": "claude-sonnet-4-6"}))
        assert app is not None
        warned = " ".join(r.getMessage() for r in caplog.records
                          if r.levelno >= logging.WARNING)
        assert "routing governance" in warned and "cost tracking" in warned
        assert "OSTIARI_REQUIRE_AXON" in warned, "the warning must name its own off switch"

    def test_require_axon_refuses_to_start(self, monkeypatch):
        """Opt back in to failing loudly at boot — the production setting."""
        from ostiari_gateway.models import ModulesConfig, SidecarConfig
        from ostiari_gateway.server import create_app

        monkeypatch.setenv("OSTIARI_DISABLE_AXON_ROUTER", "1")
        monkeypatch.setenv("OSTIARI_REQUIRE_AXON", "1")
        with pytest.raises(RuntimeError, match="routing governance"):
            create_app(initial_config=SidecarConfig(
                sidecar_id="needs-axon", modules=ModulesConfig(llm_gateway=True),
                llm={"default_model": "claude-sonnet-4-6"}))

    def test_health_reports_the_ungoverned_state(self, monkeypatch):
        """Every request still 200s on the fallback, so /health has to say so."""
        from ostiari_gateway.models import ModulesConfig, SidecarConfig
        from ostiari_gateway.server import create_app
        from starlette.testclient import TestClient

        monkeypatch.setenv("OSTIARI_DISABLE_AXON_ROUTER", "1")
        monkeypatch.delenv("OSTIARI_REQUIRE_AXON", raising=False)
        c = TestClient(create_app(initial_config=SidecarConfig(
            sidecar_id="ungoverned", modules=ModulesConfig(llm_gateway=True),
            llm={"default_model": "claude-sonnet-4-6"})))
        router = c.get("/health").json()["llm_router"]
        assert router["embedded"] is False
        assert router["governed"] is False and router["cost_tracking"] is False
        assert router["reason"], "an operator needs to know *why*"

    def test_health_omits_router_detail_without_the_llm_module(self):
        """A tool-proxy-only gateway has no LLM router to report on."""
        from ostiari_gateway.models import ModulesConfig, SidecarConfig
        from ostiari_gateway.server import create_app
        from starlette.testclient import TestClient

        c = TestClient(create_app(initial_config=SidecarConfig(
            sidecar_id="tools-only", modules=ModulesConfig(llm_gateway=False))))
        assert c.get("/health").json()["llm_router"]["embedded"] is False


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
    """Tool-bearing calls route through AxonLLM like every other call.

    AxonLLM carries ``tools``/``tool_choice`` on its request model and translates
    them into each provider's dialect, so there is no reason to go around it —
    going around it is how a call loses routing governance and cost tracking.

    ``supports_tools()`` remains as a version guard: Ostiari doesn't pin an
    AxonLLM version, and an older checkout has no ``tools`` field, so a ``tools``
    key in the request dict is discarded silently. The model then answers as if
    no tools exist ("I don't have access to a database") — a confident, fluent,
    wrong HTTP 200 that no error surfaces. Callers degrade rather than ship that.
    """

    def test_supports_tools_reflects_the_dataclass(self):
        """Probed off AxonLLM's dataclass, not hardcoded, so it tracks upstream."""
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
    def test_axonllm_carries_tools(self):
        """The embedded AxonLLM must be new enough to carry tool specs, or every
        tool-using call quietly degrades off the governed routing path."""
        import dataclasses

        from src.gateway.models import ChatCompletionRequest
        fields = {f.name for f in dataclasses.fields(ChatCompletionRequest)}
        assert {"tools", "tool_choice"} <= fields, (
            "the installed AxonLLM predates tool pass-through — upgrade it, or "
            "tool-using traffic bypasses AxonLLM's routing and cost tracking"
        )

    @pytest.mark.anyio
    async def test_route_forwards_tools_to_axon(self, monkeypatch):
        """The whole point: the specs reach AxonLLM rather than being dropped."""
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

    @pytest.mark.anyio
    async def test_route_refuses_tools_on_an_axon_too_old_to_carry_them(self, monkeypatch):
        """Version guard: raise rather than silently answer without the tools."""
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

    def test_executor_routes_tool_calls_through_axon(self, monkeypatch):
        """/invoke with tools stays on the governed path — specs included."""
        from unittest.mock import patch

        import anyio
        from ostiari_gateway.config_manager import ConfigManager
        from ostiari_gateway.modules.llm_gateway.axon_router import AxonResult
        from ostiari_gateway.modules.llm_gateway.executor import AgenticExecutor
        from ostiari_gateway.modules.llm_gateway.models import LLMConfig

        ex = AgenticExecutor(config=LLMConfig(default_model="m"), manager=ConfigManager())
        monkeypatch.setattr(type(ex._axon), "available", property(lambda self: True))
        monkeypatch.setattr(ex._axon, "supports_tools", lambda: True)
        monkeypatch.setattr(ex._axon, "knows_model", lambda m: True)

        captured: dict = {}

        async def _routed(**kwargs):
            captured.update(kwargs)
            return AxonResult(content="routed", model="m2", provider="p",
                              input_tokens=1, output_tokens=1)

        monkeypatch.setattr(ex._axon, "route", _routed)

        tools = [{"name": "db_query", "description": "", "schema": {}}]
        with patch.object(ex, "_call_with_fallback",
                          side_effect=AssertionError("must not leave the governed path")):
            async def go():
                return await ex._call_llm("m", [], [{"role": "user", "content": "hi"}], tools,
                                          context={})
            res = anyio.run(go)
        assert res.content == "routed"
        assert captured["tools"] == tools, "AxonLLM must receive the tool specs"

    def test_executor_degrades_when_axon_is_too_old_for_tools(self, monkeypatch):
        """Version guard on /invoke: the direct path still gets the specs."""
        from unittest.mock import patch

        import anyio
        from ostiari_gateway.config_manager import ConfigManager
        from ostiari_gateway.modules.llm_gateway.executor import AgenticExecutor
        from ostiari_gateway.modules.llm_gateway.models import LLMConfig
        from ostiari_gateway.modules.llm_gateway.providers import LLMResponse

        ex = AgenticExecutor(config=LLMConfig(default_model="m"), manager=ConfigManager())
        # Axon is up and knows the model — the only reason to degrade is its age.
        monkeypatch.setattr(type(ex._axon), "available", property(lambda self: True))
        monkeypatch.setattr(ex._axon, "supports_tools", lambda: False)
        monkeypatch.setattr(ex._axon, "knows_model", lambda m: True)

        async def _boom(**kwargs):
            raise AssertionError("an Axon that drops tools must not be called with them")

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


class TestTemperatureIsOmittedWhenUnset:
    """An unrequested ``temperature`` must not reach the provider.

    ``temperature`` was typed ``float = 0.7`` from the shims down through
    ``providers``, so "the caller sent nothing" and "the caller asked for 0.7"
    were the same value by the time a request was built — Ostiari put the
    parameter on the wire for every call. Bedrock Mantle's current Claude models
    *reject* it (``400 "`temperature` is deprecated for this model."``) rather
    than ignoring it, so every Ostiari→Mantle-Claude call failed on a parameter
    nobody had asked for. Tools were irrelevant: it failed identically with and
    without them, which made it look like the tool path was broken.

    These assert *absence of the key*, not ``temperature is None``. A key present
    with a None value is not equivalent: AxonLLM reads it with
    ``data.get("temperature")`` (None either way) but its Mantle paths test
    ``is not None`` on the parsed value, so only genuine absence keeps the
    parameter off the wire.
    """

    def _router(self, monkeypatch, captured: dict):
        a = AxonRouter()
        monkeypatch.setattr(a, "supports_tools", lambda: True)
        monkeypatch.setattr(a, "_ensure", lambda: None)
        a._available = True

        class _Agent:
            async def handle_chat_completion(self, request_data, ctx):
                captured.update(request_data)
                return {"model": "m", "provider": "p", "usage": {},
                        "choices": [{"message": {"content": "ok"}}]}

        a._agent = _Agent()
        return a

    @pytest.mark.anyio
    async def test_route_omits_temperature_by_default(self, monkeypatch):
        """The regression: no temperature argument => no temperature key."""
        captured: dict = {}
        a = self._router(monkeypatch, captured)
        await a.route(messages=[{"role": "user", "content": "hi"}], model="claude-sonnet")
        assert "temperature" not in captured, (
            "an invented temperature default reaches the provider and Mantle 400s on it"
        )

    @pytest.mark.anyio
    async def test_route_omits_temperature_when_explicitly_none(self, monkeypatch):
        captured: dict = {}
        a = self._router(monkeypatch, captured)
        await a.route(messages=[{"role": "user", "content": "hi"}], model="claude-sonnet",
                      temperature=None)
        assert "temperature" not in captured

    @pytest.mark.anyio
    async def test_route_forwards_a_caller_supplied_temperature(self, monkeypatch):
        """Omission is not suppression — an explicit value still goes through,
        including 0.0, which is falsy and would vanish under a truthiness test."""
        captured: dict = {}
        a = self._router(monkeypatch, captured)
        await a.route(messages=[{"role": "user", "content": "hi"}], model="claude-sonnet",
                      temperature=0.0)
        assert captured["temperature"] == 0.0

    def test_llm_config_default_does_not_invent_one(self):
        """The other source of the same value: the config default fed every
        /invoke call through executor._call_llm."""
        from ostiari_gateway.modules.llm_gateway.models import LLMConfig

        assert LLMConfig().temperature is None
        assert LLMConfig(temperature=0.3).temperature == 0.3

    def test_shipped_demo_config_leaves_temperature_unset(self):
        """gateway/llm-gateway-config.yaml set 0.7 explicitly, which reintroduces
        the failure regardless of what the field defaults to."""
        import pathlib

        import yaml

        cfg = pathlib.Path(__file__).resolve().parents[1] / "llm-gateway-config.yaml"
        llm = yaml.safe_load(cfg.read_text())["llm"]
        assert "temperature" not in llm, (
            "the demo config puts temperature on every call again — Mantle 400s on it"
        )

    @pytest.mark.parametrize("value", ["", "hot", None, {}])
    def test_opt_float_treats_unusable_input_as_absent(self, value):
        """A malformed value degrades to absence rather than raising. The old
        ``float(body.get(...))`` raised ValueError out of the request handler."""
        from ostiari_gateway.modules.llm_gateway import translate

        assert translate.opt_float(value) is None

    @pytest.mark.parametrize(("value", "expected"), [(0, 0.0), (0.5, 0.5), ("0.7", 0.7), (1, 1.0)])
    def test_opt_float_coerces_usable_input(self, value, expected):
        from ostiari_gateway.modules.llm_gateway import translate

        assert translate.opt_float(value) == expected
