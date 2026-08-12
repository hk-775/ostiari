"""AxonLLM as Ostiari's embedded LLM router.

Ostiari governs (auth, injection, quota, trace, HITL) and delegates the *routing*
of the actual model call to AxonLLM's in-process ``GatewayAgent`` — no extra
network hop, one Python call. AxonLLM owns model/provider selection, health-aware
fallback, cost tracking, smart routing, and ensemble; Ostiari owns everything
around it.

``build_gateway_agent()`` (AxonLLM's own bootstrap) wires the whole router graph
standalone — no AWS/Dynamo required (persistence auto-disables).

AxonLLM is an **optional** runtime dependency, but a load-bearing one: it is where
routing governance and token cost tracking happen, so a gateway that quietly runs
without it enforces less than it claims to while still returning 200s. It is a
separate private repo and not on PyPI, so requiring it made it a deployment
dependency of every gateway, CI runner, and contributor checkout — including the
ones that only ever proxy tools. So ``require()`` is called at startup to *warn*,
naming what is off, and ``/health`` reports ``llm_router`` for anything reading
machine-side. ``OSTIARI_REQUIRE_AXON=1`` restores refuse-to-boot, which is the
right setting in production. The direct-provider fallback in each caller remains
for a *mid-flight* failure (one call, logged), and
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
    """Thin adapter over AxonLLM's GatewayAgent for in-process routed calls."""

    def __init__(self, broker_policy: Any = None) -> None:
        self._agent: Any = None
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

        The caller decides what to do about it. At startup ``_check_axon`` logs
        this as a warning and continues, because AxonLLM is a separate private
        repo and a hard requirement makes it a deployment dependency of every
        gateway — including ones that never make an LLM call. Set
        ``OSTIARI_REQUIRE_AXON=1`` to have that warning refuse to start instead.
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
            "governance and token cost tracking happen in AxonLLM, so LLM calls return "
            "200s while enforcing neither. Install AxonLLM "
            "(pip install -e /path/to/AxonLLM) or point OSTIARI_AXON_ROOT at its "
            "checkout."
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

            from src.gateway.bootstrap import build_gateway_agent

            # AxonLLM resolves its config files relative to cwd (its own CLI
            # chdir's to the repo root). Do the same transiently while building,
            # then restore cwd.
            prev = os.getcwd()
            try:
                if axon_root:
                    os.chdir(axon_root)
                self._agent = build_gateway_agent()
            finally:
                os.chdir(prev)

            self._available = True
            self._error = ""
            self._apply_model_registry()
            self._apply_provider_routes()
            log.info("AxonLLM router embedded — GatewayAgent routing active (root=%s)", axon_root)
        except Exception as e:  # noqa: BLE001 — any failure => unavailable, degrade
            self._agent = None
            self._available = False
            # Keep the class name: "No module named 'src'" and a config
            # KeyError need different fixes, and the message alone hides which.
            self._error = f"{type(e).__name__}: {e}"
            log.warning("AxonLLM router unavailable (%s) — falling back to direct provider calls", e)

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
        if self._available and self._agent is not None:
            factory = getattr(self._agent, "provider_fn_factory", None)
            snapshot = getattr(factory, "route_snapshot", None)
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
            or self._agent is None
        ):
            return {
                "routes": len(self._provider_routes_config or []),
                "providers": 0,
            }
        factory = getattr(self._agent, "provider_fn_factory", None)
        configure = getattr(factory, "configure_routes", None)
        if not callable(configure):
            raise RuntimeError(
                "embedded AxonLLM does not support provider route pools"
            )
        result = configure(deepcopy(self._provider_routes_config))
        router = getattr(self._agent, "router", None)
        if router is not None:
            router.available_providers = factory.available_providers
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
            or self._agent is None
        ):
            return
        router = getattr(self._agent, "router", None)
        registry = getattr(router, "model_registry", None)
        if registry is None:
            raise RuntimeError("AxonLLM router has no model registry")

        errors = registry.validate(self._model_registry_config)
        if errors:
            details = "; ".join(f"{error.field}: {error.message}" for error in errors)
            raise ValueError(f"invalid model registry: {details}")

        parsed = {
            entry["name"]: registry._parse_entry(entry)
            for entry in self._model_registry_config["models"]
        }
        registry.models = parsed
        # The broker filter caches the router's original provider set. Rebuild
        # that baseline after a catalog change so newly-added mappings can route.
        if self._broker_router_id == id(router):
            router.available_providers = self._base_available_providers
        self._base_available_providers = None
        self._broker_router_id = None
        self._apply_broker_policy()
        log.info("AxonLLM model registry applied: %d models", len(parsed))

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
            reg = getattr(getattr(self._agent, "router", None), "model_registry", None)
            return bool(reg and model in reg.models)
        except Exception:  # noqa: BLE001
            return False

    def model_available(self, model: str) -> bool:
        """Whether a known Axon model has at least one non-depleted provider."""
        self._ensure()
        self._apply_broker_policy()
        try:
            router = getattr(self._agent, "router", None)
            return bool(router and router.is_model_available(model))
        except Exception:  # noqa: BLE001
            return False

    def has_available_models(self) -> bool:
        """Whether Axon can route any configured model under the pool policy."""
        self._ensure()
        self._apply_broker_policy()
        try:
            router = getattr(self._agent, "router", None)
            registry = getattr(router, "model_registry", None)
            return bool(
                router
                and registry
                and any(router.is_model_available(name) for name in registry.models)
            )
        except Exception:  # noqa: BLE001
            return False

    def _apply_broker_policy(self) -> None:
        """Intersect Axon's configured providers with funded pool availability."""
        if not self._available or self._agent is None or self._broker_policy is None:
            return
        router = getattr(self._agent, "router", None)
        registry = getattr(router, "model_registry", None)
        if router is None or registry is None:
            return

        router_id = id(router)
        if self._broker_router_id != router_id:
            current = getattr(router, "available_providers", None)
            self._base_available_providers = (
                frozenset(current) if current is not None else None
            )
            self._broker_router_id = router_id

        blocked = self._broker_policy.blocked_providers
        if not blocked:
            router.available_providers = self._base_available_providers
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
        router.available_providers = frozenset(
            provider
            for provider in base
            if self._broker_policy.is_provider_available(provider)
        )

    def supports_tools(self) -> bool:
        """Whether AxonLLM can carry tool specs through to the provider.

        Probed off the dataclass rather than hardcoded either way. AxonLLM used to
        lack a ``tools`` field entirely, so a ``tools`` key was dropped on the
        floor — no error, just a model that was never told any tools exist, which
        answers confidently that it has no such capability. It carries them now,
        translating into each provider's dialect.

        Kept as a runtime probe because Ostiari doesn't pin an AxonLLM version: an
        older checkout still returns False here and callers still degrade rather
        than lose the caller's tools silently.
        """
        try:
            import dataclasses

            from src.gateway.models import ChatCompletionRequest
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
        tools: list[dict[str, Any]] | None = None,
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
        if not self._available or self._agent is None:
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

        # Mode → model string / context flags per AxonLLM's detection contract.
        req_model = model
        ctx: dict[str, Any] = {"project_id": "default", "user_id": agent_id or "ostiari",
                               "scopes": [], "session_id": session_id}
        if ensemble:
            req_model = "ensemble" if ensemble is True else f"ensemble:{ensemble}"
        elif smart or not model:
            ctx["smart_routing"] = True
            req_model = ""  # empty model => smart auto-select

        request_data: dict[str, Any] = {
            "model": req_model,
            "messages": msgs,
            "max_tokens": max_tokens,
            "stream": False,
        }
        # Omitted entirely when None — see the docstring. A key present with value
        # None is NOT equivalent: AxonLLM reads it with ``data.get("temperature")``,
        # which returns None either way, but Mantle's paths test
        # ``is not None`` on the parsed value, so only genuine absence keeps the
        # parameter off the wire.
        if temperature is not None:
            request_data["temperature"] = temperature
        if tools:
            request_data["tools"] = tools

        out = await self._agent.handle_chat_completion(request_data, ctx)
        if not isinstance(out, dict):
            raise RuntimeError("AxonLLM returned a streaming iterator (unsupported here)")
        return _to_result(out)


def _prepare_axon_path() -> str | None:
    """Put AxonLLM's repo root on sys.path so ``src.gateway`` imports work.

    AxonLLM's modules import each other as ``src.gateway.*``, but its editable
    install puts ``<root>/src`` on sys.path — which makes ``gateway`` importable
    and ``src.gateway`` not. So the root has to go on the path *before* importing,
    rather than importing ``src.gateway`` in order to find the root, which can
    never succeed. That ordering is why the router silently ran unavailable: every
    call took the direct-provider fallback, with no AxonLLM cost tracking or
    routing governance, and nothing looked wrong.

    Returns the root, or None if AxonLLM couldn't be located. Idempotent, and
    shared with ModelRouter's TaskClassifier import, which hit the same trap.
    """
    import sys

    axon_root = _axon_root()
    if axon_root and axon_root not in sys.path:
        sys.path.insert(0, axon_root)
    return axon_root


def _axon_root() -> str | None:
    """Locate AxonLLM's repo root (which holds its ``config/`` dir).

    Prefer an explicit override, else derive it from the installed package.

    Deliberately locates the package as ``gateway``, not ``src.gateway``: the
    editable install exposes it under the former, and the latter is exactly what
    isn't importable until this function's result is on sys.path. Importing
    ``src.gateway`` here made the whole probe fail on a fresh install, which read
    as "AxonLLM not installed" when it was.
    """
    import importlib.util
    import os

    override = os.environ.get("OSTIARI_AXON_ROOT", "")
    if override and os.path.isdir(os.path.join(override, "config")):
        return override

    # find_spec avoids importing the package (importing it as `gateway` would
    # register a second copy of modules that also live under `src.gateway`).
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


def _flatten_blocks(system: Any) -> str:
    if isinstance(system, list):
        return "\n".join(b.get("text", "") for b in system
                         if isinstance(b, dict) and b.get("type") == "text")
    return str(system or "")


def _to_result(out: dict[str, Any]) -> AxonResult:
    # AxonLLM signals failure by returning an {"error": ...} dict, not by raising
    # (e.g. an unknown model id → {"error": {...}, "status_code": 404}). Such a
    # payload has no "choices", so parsing it optimistically yields content="" and
    # 0 tokens — an empty HTTP 200 that looks like a successful call. Raise instead
    # so callers fall back to the direct provider path.
    if "choices" not in out and (err := out.get("error")):
        detail = err.get("message") or err.get("type") if isinstance(err, dict) else err
        raise RuntimeError(f"AxonLLM error: {detail} (status {out.get('status_code', '?')})")

    choices = out.get("choices") or [{}]
    msg = (choices[0] or {}).get("message", {}) if choices else {}
    content = msg.get("content") or ""
    tool_calls = msg.get("tool_calls") or []
    usage = out.get("usage") or {}
    return AxonResult(
        content=content,
        model=out.get("model", ""),
        provider=out.get("provider", ""),
        input_tokens=int(usage.get("prompt_tokens", 0) or 0),
        output_tokens=int(usage.get("completion_tokens", 0) or 0),
        tool_calls=tool_calls,
        raw=out,
    )
