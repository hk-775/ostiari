"""AxonLLM as Ostiari's embedded LLM routing data plane.

Ostiari governs identity, authorization, injection, quotas, tracing, HITL,
durable usage, and lifecycle. It delegates model/provider selection,
health-aware fallback, smart routing, and ensemble execution to AxonLLM's
public in-process ``AsyncRouter`` API. No Axon server, admin surface, identity
service, database, background worker, or extra network hop is constructed.

The pinned AxonLLM 0.3.1 release exposes smart/ensemble configuration on the
core router rather than the public constructor. Those two advanced modes are
wired in one isolated compatibility helper; every request, configuration
update, availability query, and shutdown uses the public embedded API.

AxonLLM is bundled with Ostiari, but the LLM module remains optional. When the
module is enabled, AxonLLM is load-bearing: it is where routing governance and
token cost tracking happen, so production refuses to start or fall back to a
direct provider path when the router is unavailable. Development keeps the
explicit fallback for diagnostics. ``/health`` reports ``llm_router`` for
machine-side verification, and
``OSTIARI_DISABLE_AXON_ROUTER=1`` remains for tests and for deliberately
exercising that path.

Routing modes are selected by the request/context, matching AxonLLM's contract:
  - ensemble:  model == "ensemble" | "ensemble:<preset>", or context["ensemble"]=True
  - smart:     context["smart_routing"]=True, or empty model
  - fallback:  a concrete model (health-aware fallback across its backends)
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("ostiari.sidecar.llm.axon")

_PROVIDER_ROUTE_PUBLIC_FIELDS = frozenset({
    "route_id",
    "provider",
    "endpoint",
    "auth_type",
    "region",
    "allowed_models",
    "weight",
    "adaptive_weight",
    "priority",
    "enabled",
    "max_concurrency",
    "capacity_group",
    "capacity_limit",
    "connect_timeout",
    "read_timeout",
    "max_connections",
    "max_connections_per_host",
    "keepalive_timeout",
    "has_credentials",
    "status",
    "inflight",
    "selected",
    "successes",
    "failures",
    "error_ewma",
    "latency_ewma_ms",
    "latency_per_token_ewma_ms",
    "cooldown_remaining_seconds",
    "last_status_code",
})


def governed_routing_required() -> bool:
    """Whether bypassing AxonLLM must fail closed.

    Production always requires governed routing when the LLM module is active.
    Development can opt into the same contract with ``OSTIARI_REQUIRE_AXON``.
    """
    import os

    production = os.environ.get("OSTIARI_ENV", "").strip().lower() in {
        "production",
        "prod",
    }
    explicit = os.environ.get("OSTIARI_REQUIRE_AXON", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return production or explicit


class AxonResult:
    """Normalized result of an AxonLLM-routed call."""

    def __init__(self, content: str, model: str, provider: str,
                 input_tokens: int, output_tokens: int,
                 tool_calls: list[dict[str, Any]] | None = None,
                 raw: dict[str, Any] | None = None) -> None:
        self.content = content
        self.model = model
        self.provider = provider
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.tool_calls = tool_calls or []
        self.raw = raw or {}


class AxonRouter:
    """Ostiari-owned adapter over AxonLLM's public embedded router."""

    def __init__(self, broker_policy: Any = None) -> None:
        self._router: Any = None
        self._built = False
        self._available = False
        self._error: str = ""
        self._root: str | None = None
        self._disabled = False
        self._broker_policy = broker_policy
        self._base_available_providers: frozenset[str] | None = None
        self._broker_router_id: int | None = None
        self._model_registry_config: dict[str, Any] | None = None
        self._provider_routes_config: list[dict[str, Any]] | None = None

    @property
    def available(self) -> bool:
        """Whether AxonLLM's router could be built (lazy on first use)."""
        self._ensure()
        self._apply_broker_policy()
        return self._available

    @property
    def error(self) -> str:
        """Why the router is unavailable, or "" when it's up.

        Retained so startup and /health can say what actually went wrong instead
        of "unavailable" — the failure modes (not installed, config dir missing,
        bootstrap raised) need different fixes.
        """
        self._ensure()
        return self._error

    @property
    def root(self) -> str | None:
        """The AxonLLM checkout this router loaded, for startup/health output."""
        self._ensure()
        return self._root

    def require(self) -> None:
        """Raise RuntimeError if AxonLLM is unavailable; return silently if not.

        Routing governance and token cost tracking live in AxonLLM, so a gateway
        running on the direct-provider fallback looks healthy and answers requests
        while enforcing none of that — a silent downgrade of the guarantee Ostiari
        exists to make.

        Tool-only gateways never construct this router. When the LLM module is
        active, production startup treats this failure as fatal.
        """
        self._ensure()
        if self._available:
            return
        if self._disabled:
            raise RuntimeError(
                "AxonLLM routing is disabled (OSTIARI_DISABLE_AXON_ROUTER): routing "
                "governance and token cost tracking happen in AxonLLM, so LLM calls "
                "take the ungoverned direct-provider path. Unset the variable to "
                "restore governance."
            )
        raise RuntimeError(
            f"AxonLLM could not be embedded ({self._error or 'unknown error'}): routing "
            "governance and token cost tracking happen in AxonLLM. Reinstall the "
            "bundled vendor/axonllm package or point OSTIARI_AXON_ROOT at a "
            "compatible AxonLLM config root."
        )

    def _ensure(self) -> None:
        if self._built:
            return
        self._built = True

        # Disabled explicitly?
        import os
        if os.environ.get("OSTIARI_DISABLE_AXON_ROUTER", "").lower() in ("1", "true", "yes"):
            self._available = False
            self._disabled = True
            self._error = "disabled via OSTIARI_DISABLE_AXON_ROUTER"
            log.info("AxonLLM router disabled via OSTIARI_DISABLE_AXON_ROUTER")
            return

        try:
            axon_root = _prepare_axon_path()
            self._root = axon_root
            if axon_root is None:
                raise FileNotFoundError(
                    "AxonLLM config root was not found"
                )

            from axonllm import build_router

            paths = _router_config_paths(axon_root)
            self._router = build_router(
                models=paths["models"],
                providers=paths["providers"],
                pricing=paths["pricing"],
                bedrock_region=(
                    os.environ.get("AXON_BEDROCK_REGION")
                    or os.environ.get("AWS_REGION")
                    or os.environ.get("AWS_DEFAULT_REGION")
                    or "us-east-1"
                ),
                require_priced_mappings=governed_routing_required(),
            )
            _configure_advanced_modes(self._router, axon_root)

            self._available = True
            self._error = ""
            self._apply_model_registry()
            self._apply_provider_routes()
            log.info(
                "AxonLLM router embedded through public AsyncRouter API "
                "(root=%s)",
                axon_root,
            )
        except Exception as e:  # noqa: BLE001 — any failure => unavailable
            self._router = None
            self._available = False
            # Keep the class name: "No module named 'src'" and a config
            # KeyError need different fixes, and the message alone hides which.
            self._error = f"{type(e).__name__}: {e}"
            log.warning("AxonLLM router unavailable (%s)", e)

    def configure_model_registry(self, config: dict[str, Any]) -> dict[str, Any]:
        """Replace AxonLLM's in-process model registry with a validated catalog."""
        models = config.get("models")
        if not isinstance(models, list):
            raise ValueError("model registry must contain a models list")
        candidate = deepcopy({"models": models})
        self._ensure()
        if not self._available:
            self._model_registry_config = candidate
            return {
                "status": "pending",
                "models": len(models),
                "reason": self._error or "AxonLLM unavailable",
            }
        previous = self._model_registry_config
        self._model_registry_config = candidate
        try:
            self._apply_model_registry()
        except Exception:
            self._model_registry_config = previous
            raise
        return {"status": "applied", "models": len(models)}

    def model_registry_config(self) -> dict[str, Any]:
        return deepcopy(self._model_registry_config or {"models": []})

    def configure_provider_routes(
        self,
        routes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Atomically replace AxonLLM's concrete credential/endpoint routes."""
        if not isinstance(routes, list) or any(
            not isinstance(route, dict) for route in routes
        ):
            raise ValueError("provider routes must be a list of objects")
        for route in routes:
            if not str(route.get("route_id") or "").strip():
                raise ValueError("provider route_id is required")
            if not str(route.get("provider") or "").strip():
                raise ValueError("provider route provider is required")
            endpoint = str(
                route.get("endpoint") or route.get("base_url") or ""
            ).strip()
            if endpoint:
                parsed = urlparse(endpoint)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise ValueError(
                        "provider route endpoint must be an absolute HTTP(S) URL"
                    )
                if parsed.username or parsed.password:
                    raise ValueError(
                        "provider route endpoint must not contain userinfo"
                    )
                if parsed.query or parsed.fragment:
                    raise ValueError(
                        "provider route endpoint must not contain a query or fragment"
                    )
        candidate = deepcopy(routes)
        self._ensure()
        if not self._available:
            self._provider_routes_config = candidate
            return {
                "status": "pending",
                "routes": len(candidate),
                "reason": self._error or "AxonLLM unavailable",
            }
        previous = self._provider_routes_config
        self._provider_routes_config = candidate
        try:
            result = self._apply_provider_routes()
        except Exception:
            self._provider_routes_config = previous
            raise
        return {"status": "applied", **result}

    def provider_route_snapshot(self) -> list[dict[str, Any]]:
        """Return route configuration and health without credential values."""
        self._ensure()
        if self._available and self._router is not None:
            snapshot = getattr(self._router, "route_snapshot", None)
            if callable(snapshot):
                return snapshot()
        return [
            {
                key: deepcopy(value)
                for key, value in route.items()
                if key in _PROVIDER_ROUTE_PUBLIC_FIELDS
            }
            | {
                "has_credentials": bool(route.get("credentials")),
                "status": "pending",
            }
            for route in (self._provider_routes_config or [])
        ]

    def _apply_provider_routes(self) -> dict[str, int]:
        if (
            self._provider_routes_config is None
            or not self._available
            or self._router is None
        ):
            return {
                "routes": len(self._provider_routes_config or []),
                "providers": 0,
            }
        configure = getattr(self._router, "configure_routes", None)
        if not callable(configure):
            raise RuntimeError(
                "embedded AxonLLM does not support provider route pools"
            )
        result = configure(deepcopy(self._provider_routes_config))
        core = self._core_router()
        if core is not None:
            self._base_available_providers = None
            self._broker_router_id = None
            self._apply_broker_policy()
        log.info(
            "AxonLLM provider routes applied: %d routes across %d providers",
            result["routes"],
            result["providers"],
        )
        return result

    def _apply_model_registry(self) -> None:
        if (
            self._model_registry_config is None
            or not self._available
            or self._router is None
        ):
            return
        current = self._router.config_snapshot()
        snapshot = type(current).from_config(
            self._model_registry_config,
            revision=current.revision + 1,
        )
        self._router.apply_snapshot(snapshot)
        # The broker filter caches the router's original provider set. Rebuild
        # that baseline after a catalog change so newly-added mappings can route.
        core = self._core_router()
        if core is not None and self._broker_router_id == id(core):
            core.available_providers = self._base_available_providers
        self._base_available_providers = None
        self._broker_router_id = None
        self._apply_broker_policy()
        log.info(
            "AxonLLM model registry applied: %d models",
            len(self._model_registry_config["models"]),
        )

    def knows_model(self, model: str) -> bool:
        """Whether AxonLLM's registry recognizes this model name.

        AxonLLM's registry uses undated names ("claude-sonnet"), not Anthropic's
        dated IDs ("claude-sonnet-4-6"), so asking it to honor a configured
        default verbatim 404s. Callers pass an unknown name as smart-route
        instead, letting AxonLLM pick a model it can actually serve.
        """
        if not model:
            return False
        self._ensure()
        try:
            return bool(self._router and self._router.knows_model(model))
        except Exception:  # noqa: BLE001
            return False

    def model_available(self, model: str) -> bool:
        """Whether a known Axon model has at least one non-depleted provider."""
        self._ensure()
        self._apply_broker_policy()
        try:
            return bool(self._router and self._router.model_available(model))
        except Exception:  # noqa: BLE001
            return False

    def has_available_models(self) -> bool:
        """Whether Axon can route any configured model under the pool policy."""
        self._ensure()
        self._apply_broker_policy()
        try:
            return bool(self._router and self._router.has_available_models())
        except Exception:  # noqa: BLE001
            return False

    def _apply_broker_policy(self) -> None:
        """Intersect Axon's configured providers with funded pool availability."""
        if not self._available or self._router is None or self._broker_policy is None:
            return
        core = self._core_router()
        registry = getattr(core, "model_registry", None)
        if core is None or registry is None:
            return

        router_id = id(core)
        if self._broker_router_id != router_id:
            current = getattr(core, "available_providers", None)
            self._base_available_providers = (
                frozenset(current) if current is not None else None
            )
            self._broker_router_id = router_id

        blocked = self._broker_policy.blocked_providers
        if not blocked:
            core.available_providers = self._base_available_providers
            return

        all_providers = {
            mapping.provider
            for model in registry.models.values()
            for mapping in model.providers
        }
        base = (
            set(self._base_available_providers)
            if self._base_available_providers is not None
            else all_providers
        )
        core.available_providers = frozenset(
            provider
            for provider in base
            if self._broker_policy.is_provider_available(provider)
        )

    def _core_router(self) -> Any:
        """Return Axon's documented compatibility alias for policy filtering."""
        if self._router is None:
            return None
        runtime = getattr(self._router, "_runtime", None)
        return getattr(runtime, "router", None)

    def supports_tools(self) -> bool:
        """Whether AxonLLM can carry tool specs through to the provider.

        Probed off the dataclass rather than hardcoded either way. AxonLLM used to
        lack a ``tools`` field entirely, so a ``tools`` key was dropped on the
        floor — no error, just a model that was never told any tools exist, which
        answers confidently that it has no such capability. It carries them now,
        translating into each provider's dialect.

        Ostiari vendors one exact release, but this probe keeps source checkouts
        fail-closed if somebody replaces the bundled package incorrectly.
        """
        try:
            import dataclasses

            from axonllm import ChatCompletionRequest
            return any(f.name == "tools" for f in dataclasses.fields(ChatCompletionRequest))
        except Exception:  # noqa: BLE001
            return False

    async def route(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str = "",
        max_tokens: int = 4096,
        temperature: float | None = None,
        top_p: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        smart: bool = False,
        ensemble: str | bool = False,
        agent_id: str = "",
        session_id: str = "",
        system: Any = None,
    ) -> AxonResult:
        """Route one call through AxonLLM. Raises if unavailable (caller falls back).

        ``ensemble`` may be True (default preset), a preset name, or False.
        ``smart`` requests task-classification routing. Otherwise ``model`` is used.

        ``tools`` are forwarded OpenAI-shaped; AxonLLM translates them into each
        provider's dialect. Raises if an AxonLLM too old to carry them is loaded —
        routing anyway would return a confident tool-free answer ("I don't have
        access to a database") that reads like a successful response. Callers
        check ``supports_tools()`` first so they can degrade deliberately.

        ``temperature`` is None-by-default and omitted from the request when None,
        rather than defaulting to a value here. Newer models *reject* the parameter
        outright — Bedrock Mantle's Claude models answer
        ``400 "`temperature` is deprecated for this model."`` — so sending a
        locally-invented default made every such call fail. AxonLLM already omits
        the key when it is None; the bug was materializing a number here so it
        never could.
        """
        self._ensure()
        if not self._available or self._router is None:
            raise RuntimeError("AxonLLM router not available")
        self._apply_broker_policy()

        if self._broker_policy is not None and self._broker_policy.blocked_providers:
            from ostiari_gateway.modules.llm_gateway.broker_policy import (
                BrokerPoolDepletedError,
            )

            if model and self.knows_model(model) and not self.model_available(model):
                raise BrokerPoolDepletedError(
                    f"All providers for model '{model}' have depleted token pools"
                )
            if (smart or not model or ensemble) and not self.has_available_models():
                raise BrokerPoolDepletedError(
                    "All AxonLLM provider routes have depleted token pools"
                )

        if tools and not self.supports_tools():
            raise RuntimeError(
                f"this AxonLLM cannot carry tool specs ({len(tools)} requested) — "
                "it would silently drop them and answer as if no tools existed"
            )

        # AxonLLM takes OpenAI-shaped messages; fold any Anthropic system prompt in.
        msgs: list[dict[str, Any]] = []
        if system:
            sys_text = system if isinstance(system, str) else _flatten_blocks(system)
            if sys_text:
                msgs.append({"role": "system", "content": sys_text})
        msgs.extend(messages)

        from axonllm import ChatCompletionRequest

        request = ChatCompletionRequest(
            model=model,
            messages=msgs,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            tools=tools,
            tool_choice=tool_choice,
            stream=False,
        )
        prompt = _last_user_text(msgs)
        core = self._core_router()
        if core is None:
            raise RuntimeError("AxonLLM routing core is unavailable")

        if ensemble:
            preset_name = None if ensemble is True else str(ensemble)
            config = getattr(core, "_ensemble_config", None)
            preset = (
                config.default_preset()
                if preset_name is None
                else config.get_preset(preset_name)
            ) if config is not None else None
            if preset is None:
                raise RuntimeError(
                    f"AxonLLM ensemble preset is unavailable: "
                    f"{preset_name or 'default'}"
                )
            response, decision = await core.ensemble_route(
                request,
                self._router._runtime.provider_factory,
                prompt,
                preset,
                project_id="default",
                user_id=agent_id or "ostiari",
                tenant_id="default",
            )
            return _to_public_result(
                response,
                routing={"mode": "ensemble", "decision": decision},
            )

        if smart or not model:
            response, decision = await core.smart_route(
                request,
                self._router._runtime.provider_factory,
                prompt,
                project_id="default",
                user_id=agent_id or "ostiari",
                tenant_id="default",
            )
            return _to_public_result(
                response,
                routing={"mode": "smart", "decision": decision},
            )

        completion_args: dict[str, Any] = {
            "model": model,
            "messages": msgs,
            "max_tokens": max_tokens,
            "tools": tools,
            "stream": False,
        }
        if temperature is not None:
            completion_args["temperature"] = temperature
        if top_p is not None:
            completion_args["top_p"] = top_p
        if tool_choice is not None:
            completion_args["tool_choice"] = tool_choice
        response = await self._router.chat.completions.create(
            **completion_args,
        )
        return _to_public_result(response)

    async def close(self) -> None:
        """Release Axon provider sessions and credential providers."""
        router = self._router
        self._router = None
        self._available = False
        if router is not None:
            await router.close()


def _prepare_axon_path() -> str | None:
    """Expose AxonLLM's pinned config root to the embedded compatibility layer.

    The public ``axonllm`` package supplies the router API. The repository root
    is also placed on ``sys.path`` because AxonLLM 0.3.1 keeps smart and
    ensemble strategy configuration in a compatibility module outside that
    public facade. This workaround is intentionally isolated here.
    """
    import sys

    axon_root = _axon_root()
    if axon_root and axon_root not in sys.path:
        sys.path.insert(0, axon_root)
    return axon_root


def _axon_root() -> str | None:
    """Locate AxonLLM's repo root (which holds its ``config/`` dir).

    Prefer an explicit override, then wheel-packaged configuration, the bundled
    source tree, and finally a compatible standalone AxonLLM checkout.
    """
    import importlib.util
    import os

    override = os.environ.get("OSTIARI_AXON_ROOT", "")
    if override and os.path.isdir(os.path.join(override, "config")):
        return override

    # Wheel installs carry the reviewed routing catalog inside ostiari_gateway.
    # AxonLLM's code is supplied by the exact companion axon-llm distribution.
    from pathlib import Path

    packaged = Path(__file__).resolve().parents[2] / "_embedded" / "axonllm"
    if (packaged / "config").is_dir():
        return str(packaged)

    # Clean Ostiari source checkouts carry an immutable AxonLLM snapshot.
    # Resolve it without requiring contributors to export an environment
    # variable or clone a second repository.
    for parent in Path(__file__).resolve().parents:
        bundled = parent / "vendor" / "axonllm"
        if (bundled / "config").is_dir():
            return str(bundled)

    # Compatibility fallback for operators who deliberately replace the
    # bundled source with an external checkout.
    for name in ("gateway", "src.gateway"):
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            continue
        if spec is None or not spec.origin:
            continue
        # …/<root>/src/gateway/__init__.py → <root>
        root = os.path.dirname(os.path.dirname(os.path.dirname(spec.origin)))
        if os.path.isdir(os.path.join(root, "config")):
            return root
    return None


def _router_config_paths(axon_root: str) -> dict[str, str]:
    """Resolve the routing-only files consumed by Axon's embedded API."""
    import os
    from pathlib import Path

    root = Path(axon_root)

    def _path(env_name: str, default: str) -> str:
        configured = os.environ.get(env_name, "").strip()
        path = Path(configured) if configured else root / default
        if not path.is_absolute():
            path = root / path
        if not path.is_file():
            raise FileNotFoundError(f"{env_name or default} not found: {path}")
        return str(path)

    providers_default = (
        "config/providers.yaml"
        if (root / "config/providers.yaml").is_file()
        else "config/providers.yaml.example"
    )
    return {
        "models": _path("AXON_MODELS_CONFIG", "config/models.yaml"),
        "providers": _path("AXON_PROVIDERS_CONFIG", providers_default),
        "pricing": _path("AXON_PRICING_CONFIG", "config/pricing.yaml"),
    }


def _configure_advanced_modes(router: Any, axon_root: str) -> None:
    """Wire smart/ensemble modes on the exact bundled Axon 0.3.1 core.

    Axon's public ``build_router`` intentionally constructs only the low-latency
    routing runtime. Version 0.3.1 still exposes its smart and ensemble
    strategies on the documented compatibility core, so this is the one
    isolated place where Ostiari binds those optional modes.
    """
    from pathlib import Path

    from src.gateway.ensemble_config import EnsembleConfig
    from src.gateway.feedback_tracker import FeedbackTracker
    from src.gateway.model_leaderboard import ModelLeaderboard
    from src.gateway.models import RoutingStrategy
    from src.gateway.smart_routing import SmartRoutingStrategy
    from src.gateway.task_classifier import TaskClassifier

    runtime = getattr(router, "_runtime", None)
    core = getattr(runtime, "router", None)
    registry = getattr(runtime, "model_registry", None)
    if core is None or registry is None:
        raise RuntimeError("AxonLLM public router lacks its routing runtime")

    root = Path(axon_root)
    leaderboard = ModelLeaderboard()
    leaderboard.load(
        str(root / "config/leaderboard.yaml"),
        valid_models=set(registry.models),
    )
    cost_tracker = getattr(core, "_cost_tracker", None)
    if cost_tracker is None:
        raise RuntimeError("AxonLLM routing core lacks cost tracking")
    smart = SmartRoutingStrategy(
        classifier=TaskClassifier(),
        leaderboard=leaderboard,
        model_registry=registry,
        health_tracker=core.health_tracker,
        cost_tracker=cost_tracker,
        feedback_tracker=FeedbackTracker(),
        confidence_threshold=leaderboard.config.get(
            "confidence_threshold",
            0.3,
        ),
        cost_quality_tradeoff=leaderboard.config.get(
            "cost_quality_tradeoff",
            0.3,
        ),
        default_model=leaderboard.config.get(
            "default_model",
            "claude-sonnet",
        ),
        pricing_config=cost_tracker.pricing_config,
    )
    core._smart_strategy = smart
    core._strategies[RoutingStrategy.SMART] = smart

    ensemble = EnsembleConfig()
    ensemble.load(str(root / "config/ensemble.yaml"))
    core._ensemble_config = ensemble


def _flatten_blocks(system: Any) -> str:
    if isinstance(system, list):
        return "\n".join(b.get("text", "") for b in system
                         if isinstance(b, dict) and b.get("type") == "text")
    return str(system or "")


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    """Return the latest user text used by smart/ensemble classification."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict)
                and part.get("type") in {"text", "input_text"}
            )
    return ""


def _to_public_result(
    response: Any,
    *,
    routing: dict[str, Any] | None = None,
) -> AxonResult:
    """Normalize Axon's public response dataclass for Ostiari's shims."""
    import dataclasses

    choices = response.choices or [{}]
    message = (choices[0] or {}).get("message", {}) if choices else {}
    usage = response.usage
    raw = dataclasses.asdict(response)
    if routing:
        serializable = {
            key: (
                dataclasses.asdict(value)
                if dataclasses.is_dataclass(value)
                else value
            )
            for key, value in routing.items()
        }
        raw["routing"] = serializable
    return AxonResult(
        content=message.get("content") or "",
        model=response.model,
        provider=response.provider,
        input_tokens=int(usage.prompt_tokens or 0),
        output_tokens=int(usage.completion_tokens or 0),
        tool_calls=message.get("tool_calls") or [],
        raw=raw,
    )
