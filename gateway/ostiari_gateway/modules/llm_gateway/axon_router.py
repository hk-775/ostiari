"""AxonLLM as Ostiari's embedded LLM router.

Ostiari governs (auth, injection, quota, trace, HITL) and delegates the *routing*
of the actual model call to AxonLLM's in-process ``GatewayAgent`` — no extra
network hop, one Python call. AxonLLM owns model/provider selection, health-aware
fallback, cost tracking, smart routing, and ensemble; Ostiari owns everything
around it.

``build_gateway_agent()`` (AxonLLM's own bootstrap) wires the whole router graph
standalone — no AWS/Dynamo required (persistence auto-disables). If AxonLLM isn't
installed, ``AxonRouter.available`` is False and the caller falls back to its own
direct provider calls.

Routing modes are selected by the request/context, matching AxonLLM's contract:
  - ensemble:  model == "ensemble" | "ensemble:<preset>", or context["ensemble"]=True
  - smart:     context["smart_routing"]=True, or empty model
  - fallback:  a concrete model (health-aware fallback across its backends)
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("ostiari.sidecar.llm.axon")


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

    def __init__(self) -> None:
        self._agent: Any = None
        self._built = False
        self._available = False

    @property
    def available(self) -> bool:
        """Whether AxonLLM's router could be built (lazy on first use)."""
        self._ensure()
        return self._available

    def _ensure(self) -> None:
        if self._built:
            return
        self._built = True

        # Disabled explicitly?
        import os
        if os.environ.get("OSTIARI_DISABLE_AXON_ROUTER", "").lower() in ("1", "true", "yes"):
            self._available = False
            log.info("AxonLLM router disabled via OSTIARI_DISABLE_AXON_ROUTER")
            return

        try:
            import src.gateway  # noqa: F401  — locate the installed AxonLLM package
            from src.gateway.bootstrap import build_gateway_agent

            # AxonLLM resolves its config files relative to cwd (its own CLI
            # chdir's to the repo root). Do the same transiently while building,
            # deriving the root from the installed package, then restore cwd.
            axon_root = _axon_root()
            prev = os.getcwd()
            try:
                if axon_root:
                    os.chdir(axon_root)
                self._agent = build_gateway_agent()
            finally:
                os.chdir(prev)

            self._available = True
            log.info("AxonLLM router embedded — GatewayAgent routing active (root=%s)", axon_root)
        except Exception as e:  # noqa: BLE001 — any failure => unavailable, degrade
            self._agent = None
            self._available = False
            log.warning("AxonLLM router unavailable (%s) — falling back to direct provider calls", e)

    def knows_model(self, model: str) -> bool:
        """Whether AxonLLM's registry recognizes this model name.

        AxonLLM's registry uses undated names ("claude-sonnet"), not Anthropic's
        dated IDs ("claude-sonnet-4-6"), so asking it to honor a configured
        default verbatim 404s. Callers pass an unknown name as smart-route
        instead, letting AxonLLM pick a model it can actually serve.
        """
        if not model:
            return False
        try:
            reg = getattr(getattr(self._agent, "router", None), "model_registry", None)
            return bool(reg and model in reg.models)
        except Exception:  # noqa: BLE001
            return False

    def supports_tools(self) -> bool:
        """Whether AxonLLM can carry tool specs through to the provider.

        It cannot: ``src.gateway.models.ChatCompletionRequest`` has no ``tools``
        field and ``GatewayAgent._parse_request`` reads only the fields it does
        have, so a ``tools`` key in the request dict is dropped on the floor —
        no error, just a model that was never told any tools exist. AxonLLM's own
        PRD lists tool-use pass-through as an open question, not a feature.

        Probed off the dataclass rather than hardcoded so this flips to True on
        its own once AxonLLM gains the field.
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
        temperature: float = 0.7,
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

        Raises if ``tools`` is set but AxonLLM can't carry them — routing the call
        anyway would return a confident tool-free answer ("I don't have access to
        a database") that reads like a successful response. Callers must check
        ``supports_tools()`` first and take their direct-provider path instead.
        """
        self._ensure()
        if not self._available or self._agent is None:
            raise RuntimeError("AxonLLM router not available")

        if tools and not self.supports_tools():
            raise RuntimeError(
                f"AxonLLM cannot carry tool specs ({len(tools)} requested) — "
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
            "temperature": temperature,
            "stream": False,
        }
        if tools:
            request_data["tools"] = tools

        out = await self._agent.handle_chat_completion(request_data, ctx)
        if not isinstance(out, dict):
            raise RuntimeError("AxonLLM returned a streaming iterator (unsupported here)")
        return _to_result(out)


def _axon_root() -> str | None:
    """Locate AxonLLM's repo root (which holds its ``config/`` dir).

    Prefer an explicit override, else derive it from the installed
    ``src.gateway`` package (…/<root>/src/gateway → <root>).
    """
    import os
    override = os.environ.get("OSTIARI_AXON_ROOT", "")
    if override and os.path.isdir(os.path.join(override, "config")):
        return override
    try:
        import src.gateway
        root = os.path.dirname(os.path.dirname(os.path.dirname(src.gateway.__file__)))
        if os.path.isdir(os.path.join(root, "config")):
            return root
    except Exception:  # noqa: BLE001
        pass
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
