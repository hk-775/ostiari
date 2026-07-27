"""Anthropic ``/v1/messages`` interception + cross-provider routing — the Claude Code shim.

Claude Code (and any Anthropic-SDK client) points at ``POST /v1/messages`` and
runs its *own* tool loop: the model returns ``tool_use`` blocks, the client
executes them locally (Bash, Edit, …) and calls back. Ostiari's native
``/invoke`` owns the loop instead, so it can't sit under Claude Code. This proxy
is the correct seam:

  intercept  ->  govern (auth, injection, quota)  ->  ROUTE to any provider
             ->  forward / translate  ->  stream back  ->  trace

Routing (content-based, via the existing ModelRouter) may land on any provider:
  - Anthropic target  -> raw SSE passthrough, true end-to-end streaming.
  - Other provider    -> translate the Anthropic request to the provider, call
                         it, translate the response back to Anthropic Messages
                         and re-emit as Anthropic SSE so the client is unaffected.

The gateway holds the provider credentials; the client never sends a real key.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from ostiari_gateway.modules.llm_gateway import translate

log = logging.getLogger("ostiari.sidecar.llm.messages")

_DEFAULT_ANTHROPIC_BASE = "https://api.anthropic.com"
_DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
_USAGE_SCAN_CAP = 8192  # bytes of SSE tail to scan for token usage


def _err(status: int, err_type: str, message: str) -> JSONResponse:
    """Anthropic-shaped error so the client surfaces it natively."""
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": err_type, "message": message}},
    )


def _provider_of(model: str) -> str:
    """Classify a model string into a provider family."""
    m = model.lower()
    if m.startswith("bedrock/"):
        return "bedrock"
    if m.startswith("azure/"):
        return "azure"
    if m.startswith("vertex/"):
        return "vertex"
    if "gpt" in m or m.startswith("openai/") or "o1" in m or "o3" in m:
        return "openai"
    if "command" in m or "cohere" in m:
        return "cohere"
    if "claude" in m or m.startswith("anthropic"):
        return "anthropic"
    return "anthropic"


class MessagesProxy:
    """Governed, cross-provider passthrough for the Anthropic ``/v1/messages`` API."""

    def __init__(
        self,
        config: Any,
        provider: Any = None,
        router: Any = None,
        security: Any = None,
        quota_enforcer: Any = None,
        trace_reporter: Any = None,
        agent_auth: Any = None,
        axon: Any = None,
    ) -> None:
        self._config = config
        self._provider = provider          # llm_gateway LLMProvider (holds creds + SDK calls)
        self._router = router
        self._security = security
        self._quota = quota_enforcer
        self._trace = trace_reporter
        self._agent_auth = agent_auth
        self._axon = axon                  # AxonLLM router (single routing authority)

    # ── credentials / endpoints ──────────────────────────────────────────
    def _anthropic_key(self) -> str:
        creds = getattr(self._config, "credentials", None)
        return (getattr(creds, "anthropic", "") if creds else "") or os.environ.get("ANTHROPIC_API_KEY", "")

    def _anthropic_base(self) -> str:
        return os.environ.get("OSTIARI_ANTHROPIC_BASE_URL", _DEFAULT_ANTHROPIC_BASE).rstrip("/")

    # ── entry point ──────────────────────────────────────────────────────
    async def handle(self, request: Request) -> Any:
        try:
            body: dict[str, Any] = await request.json()
        except Exception:
            return _err(400, "invalid_request_error", "Malformed JSON body")
        if not isinstance(body, dict) or "messages" not in body:
            return _err(400, "invalid_request_error", "Missing 'messages'")

        agent_id = request.headers.get("X-Agent-Id", "unknown")
        framework = request.headers.get("X-Framework", "claude-code")
        # Claude Code correlates a whole prompt's calls with x-claude-code-session-id;
        # fall back to it when the generic X-Session-Id isn't set. This is what lets
        # a prompt's many sub-calls group under one parent span.
        session_id = (request.headers.get("X-Session-Id")
                      or request.headers.get("x-claude-code-session-id", ""))
        requested_model = body.get("model", "")
        streaming = bool(body.get("stream", False))

        # Flatten content once for detection + routing (never mutates the body).
        flat = self._flatten(body.get("system"), body.get("messages", []))

        # ── Gate 1: agent authorization (endpoint + model/provider/budget) ──
        if self._agent_auth:
            allowed, reason = self._agent_auth.check(agent_id, "/v1/messages")
            if not allowed:
                await self._report(agent_id, framework, session_id, requested_model,
                                    tier="block", reason=reason, limit_type="agent_authorization")
                return _err(403, "permission_error", reason or "Agent not authorized")
            # Enforce per-agent model/provider grants + budget on the requested
            # model (the agent explicitly asked for it). Without this, an agent
            # restricted to one model/provider or a $ cap could use any.
            allowed, reason = self._agent_auth.authorize_llm(
                agent_id, requested_model, _provider_of(requested_model))
            if not allowed:
                await self._report(agent_id, framework, session_id, requested_model,
                                    tier="block", reason=reason, limit_type="agent_authorization")
                return _err(403, "permission_error", reason)

        # ── Gate 2: security (injection detection + PII) — FAIL-CLOSED ───
        # Runs over the flattened text view. If an enabled control is
        # unavailable, errors, or fires, the request is blocked (never silently
        # allowed / forwarded unredacted). Full block-content PII rewriting on
        # the shim is a follow-up; here we block on PII presence when redaction
        # is enabled rather than leak it downstream.
        if self._security is not None:
            _, meta = self._security.process_messages(list(flat))
            if meta.get("blocked") or meta.get("pii_redacted"):
                reason = meta.get("block_reason") or "PII detected in prompt"
                limit_type = "prompt_injection" if meta.get("injection_detected") else "pii"
                await self._report(agent_id, framework, session_id, requested_model,
                                    tier="block", reason=reason, limit_type=limit_type)
                return _err(403, "permission_error", f"Request blocked by Ostiari: {reason}")

        # ── Ostiari-side quota gate (pre-call budget guard) ──────────────
        # AxonLLM does its own cost tracking + model access control; this is
        # Ostiari's own budget ceiling on top.
        reservation_id: int | None = None
        if self._quota is not None:
            try:
                est = self._quota.estimate_cost(requested_model)
                decision = self._quota.check(model=requested_model, estimated_cost=est,
                                             reserve=True)
                if not decision.allowed:
                    await self._report(agent_id, framework, session_id, requested_model,
                                        tier="block", reason=decision.reason, limit_type="quota")
                    return _err(429, "rate_limit_error", f"Request blocked by quota: {decision.reason}")
                reservation_id = decision.reservation_id
                self._quota.record_request()
            except Exception as e:  # noqa: BLE001
                log.debug("Quota check failed: %s", e)

        # ── Dispatch: AxonLLM is the routing authority when available ────
        # It selects the model + provider, enforces model access, tracks cost,
        # and does health-aware fallback. We run it in single-response mode
        # (ensemble stays on /invoke) and translate the result back to Anthropic.
        #
        # Tool-bearing calls route through it too. AxonLLM carries tool specs and
        # translates them per provider; the ``supports_tools()`` check survives
        # only as a version guard, since Ostiari doesn't pin an AxonLLM version and
        # an older checkout would drop the caller's tools without saying so.
        _wants_tools = bool(body.get("tools"))
        if (self._axon is not None and self._axon.available
                and not (_wants_tools and not self._axon.supports_tools())):
            return await self._forward_axon(request, body, requested_model, agent_id,
                                            session_id, framework, streaming, reservation_id)
        if _wants_tools and self._axon is not None and self._axon.available:
            log.warning("AxonLLM predates tool pass-through — using the direct provider "
                        "path for %d tool(s); routing governance and cost tracking are "
                        "bypassed for this call. Upgrade AxonLLM.",
                        len(body.get("tools") or []))

        # ── Fallback: no AxonLLM — Ostiari's own ModelRouter + direct call ─
        model = self._route(agent_id, requested_model, flat, session_id)
        routed = model != requested_model
        provider = _provider_of(model)
        meta = {"agent_id": agent_id, "framework": framework, "session_id": session_id,
                "model": model, "routed": routed, "reservation_id": reservation_id}
        if provider == "anthropic":
            return await self._forward_anthropic(request, body, model, streaming, meta)
        return await self._forward_translated(body, model, provider, streaming, meta)

    # ── routing ──────────────────────────────────────────────────────────
    def _route(self, agent_id: str, requested_model: str, flat: list[dict[str, str]],
               session_id: str = "") -> str:
        if self._router is None:
            return requested_model
        try:
            selected = self._router.select_model(
                {"agent_id": agent_id, "messages": flat, "session_id": session_id})
        except Exception as e:  # noqa: BLE001 — routing must never break the call
            log.debug("Routing failed, using requested model: %s", e)
            return requested_model
        if selected and selected != requested_model:
            log.info("Content routing: %s -> %s (agent=%s)", requested_model or "?", selected, agent_id)
            return selected
        return requested_model or selected

    @staticmethod
    def _flatten(system: Any, messages: list[dict[str, Any]]) -> list[dict[str, str]]:
        flat: list[dict[str, str]] = []
        sys_text = translate.flatten_system(system)
        if sys_text:
            flat.append({"role": "system", "content": sys_text})
        for m in messages:
            flat.append({"role": m.get("role", "user"), "content": translate.text_of(m.get("content", ""))})
        return flat

    # ── Anthropic target: raw passthrough ────────────────────────────────
    async def _forward_anthropic(
        self, request: Request, body: dict[str, Any], model: str, streaming: bool, meta: dict[str, Any]
    ) -> Any:
        key = self._anthropic_key()
        if not key:
            return _err(500, "api_error",
                        "Ostiari has no Anthropic credential (config.credentials.anthropic or "
                        "ANTHROPIC_API_KEY).")
        body = {**body, "model": model}
        url = f"{self._anthropic_base()}/v1/messages"
        headers = {
            "x-api-key": key,
            "anthropic-version": request.headers.get("anthropic-version", _DEFAULT_ANTHROPIC_VERSION),
            "content-type": "application/json",
        }
        if request.headers.get("anthropic-beta"):
            headers["anthropic-beta"] = request.headers["anthropic-beta"]

        if not streaming:
            async with httpx.AsyncClient(timeout=600.0) as client:
                try:
                    resp = await client.post(url, headers=headers, json=body)
                except Exception as e:  # noqa: BLE001
                    return _err(502, "api_error", f"Upstream call failed: {e}")
            payload = resp.json() if resp.content else {}
            await self._report(meta["agent_id"], meta["framework"], meta["session_id"], model,
                                tier="allow" if resp.status_code == 200 else "block",
                                usage=payload.get("usage", {}) if isinstance(payload, dict) else {},
                                routed=meta["routed"],
                                reason=None if resp.status_code == 200 else "upstream error",
                                reservation_id=meta.get("reservation_id"))
            return JSONResponse(status_code=resp.status_code, content=payload)

        # streaming: relay raw SSE bytes untouched, scrape usage from the tail
        client = httpx.AsyncClient(timeout=600.0)
        cm = client.stream("POST", url, headers=headers, json=body)
        try:
            resp = await cm.__aenter__()
        except Exception as e:  # noqa: BLE001
            await client.aclose()
            return _err(502, "api_error", f"Upstream call failed: {e}")
        if resp.status_code != 200:
            data = await resp.aread()
            await cm.__aexit__(None, None, None)
            await client.aclose()
            try:
                content = json.loads(data or b"{}")
            except Exception:
                content = {"type": "error", "error": {"type": "api_error", "message": "upstream error"}}
            await self._report(meta["agent_id"], meta["framework"], meta["session_id"], model,
                                tier="block", reason="upstream error", routed=meta["routed"],
                                reservation_id=meta.get("reservation_id"))
            return JSONResponse(status_code=resp.status_code, content=content)

        proxy = self

        async def relay() -> Any:
            scan = b""
            try:
                # aiter_bytes (not aiter_raw): httpx auto-decompresses any
                # content-encoding so we relay plain SSE text. We intentionally
                # do not forward the upstream content-encoding header.
                async for chunk in resp.aiter_bytes():
                    yield chunk
                    scan = (scan + chunk)[-_USAGE_SCAN_CAP:]
            finally:
                await cm.__aexit__(None, None, None)
                await client.aclose()
                await proxy._report(meta["agent_id"], meta["framework"], meta["session_id"], model,
                                    tier="allow", usage=_scrape_usage(scan), routed=meta["routed"],
                                    reservation_id=meta.get("reservation_id"))

        return StreamingResponse(relay(), media_type="text/event-stream")

    # ── Non-Anthropic target: translate round-trip ───────────────────────
    async def _forward_translated(
        self, body: dict[str, Any], model: str, provider: str, streaming: bool, meta: dict[str, Any]
    ) -> Any:
        """Translate Anthropic -> provider, call it, translate back to Anthropic (+SSE)."""
        if self._provider is None:
            return _err(500, "api_error", "No LLM provider configured for cross-provider routing.")

        try:
            anthropic_msg = await self._call_translated_provider(body, model, provider)
        except Exception as e:  # noqa: BLE001
            log.warning("Cross-provider call to %s (%s) failed: %s", model, provider, e)
            await self._report(meta["agent_id"], meta["framework"], meta["session_id"], model,
                                tier="block", reason=f"provider error: {e}", routed=meta["routed"],
                                reservation_id=meta.get("reservation_id"))
            return _err(502, "api_error", f"Upstream provider call failed: {e}")

        usage = anthropic_msg.get("usage", {})
        await self._report(meta["agent_id"], meta["framework"], meta["session_id"], model,
                           tier="allow", usage=usage, routed=meta["routed"],
                           reservation_id=meta.get("reservation_id"))

        if not streaming:
            return JSONResponse(status_code=200, content=anthropic_msg)

        def gen() -> Any:
            yield from translate.anthropic_message_to_sse(anthropic_msg)

        return StreamingResponse(gen(), media_type="text/event-stream")

    def _axon_knows(self, model: str) -> bool:
        """Whether AxonLLM's registry recognizes a model name."""
        return self._axon.knows_model(model)

    # ── AxonLLM target: single routing authority ─────────────────────────
    async def _forward_axon(
        self, request: Request, body: dict[str, Any], requested_model: str, agent_id: str,
        session_id: str, framework: str, streaming: bool, reservation_id: int | None = None,
    ) -> Any:
        """Route via AxonLLM (selection + access + cost + fallback), translate to Anthropic.

        Single-response mode: ensemble is NOT enabled here — Claude Code needs
        exactly one Anthropic response per call. Empty requested_model lets
        AxonLLM smart-route; a concrete model asks AxonLLM to honor it with
        health-aware fallback.
        """
        # AxonLLM selects from its OWN model registry (e.g. "claude-sonnet"), which
        # does not use Anthropic's dated IDs ("claude-sonnet-4-6"). Only pass the
        # client's model through if AxonLLM knows it; otherwise smart-route so
        # AxonLLM picks a model it can actually serve.
        axon_model = requested_model if self._axon_knows(requested_model) else ""
        # Claude Code sends message content as a LIST of blocks (text/tool_use/
        # tool_result), not a plain string, and system as blocks too. AxonLLM
        # expects OpenAI-shaped string content — translate first (this also folds
        # the system prompt in and maps tool round-trips), else it crashes with
        # "'list' object has no attribute 'lower'".
        oai_messages = translate.anthropic_to_openai_messages(body.get("system"), body.get("messages", []))
        try:
            res = await self._axon.route(
                messages=oai_messages,
                model=axon_model,
                max_tokens=int(body.get("max_tokens", getattr(self._config, "max_tokens", 4096))),
                temperature=float(body.get("temperature", getattr(self._config, "temperature", 0.7))),
                tools=translate.anthropic_tools_to_openai(body.get("tools"))[0],
                smart=not axon_model,        # unknown/absent model → smart auto-select
                ensemble=False,              # never on the interactive shim path
                agent_id=agent_id,
                session_id=session_id,
                system=None,                 # already folded into oai_messages
            )
        except Exception as e:  # noqa: BLE001 — fall back to the direct path
            log.warning("AxonLLM shim route failed (%s) — using direct path", e)
            # Use the client's own requested model (a valid Anthropic ID from
            # Claude Code) rather than an Axon-registry/Bedrock name that the
            # direct provider path can't honor. Dispatch by provider so an
            # Anthropic model goes to the Anthropic endpoint (not the OpenAI SDK).
            model = requested_model or getattr(self._config, "default_model", "") or "claude-sonnet-4-6"
            provider = _provider_of(model)
            meta = {"agent_id": agent_id, "framework": framework, "session_id": session_id,
                    "model": model, "routed": False, "reservation_id": reservation_id}
            if provider == "anthropic":
                return await self._forward_anthropic(request, body, model, streaming, meta)
            return await self._forward_translated(body, model, provider, streaming, meta)

        # Build an Anthropic Messages object from AxonLLM's OpenAI-shaped result.
        anthropic_msg = _axon_result_to_anthropic(res, body.get("tools"))
        routed = (res.model or "") != requested_model
        await self._report(agent_id, framework, session_id, res.model or requested_model,
                           tier="allow",
                           usage={"input_tokens": res.input_tokens, "output_tokens": res.output_tokens},
                           routed=routed, reservation_id=reservation_id)

        if not streaming:
            return JSONResponse(status_code=200, content=anthropic_msg)

        def gen() -> Any:
            yield from translate.anthropic_message_to_sse(anthropic_msg)

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def _call_translated_provider(
        self, body: dict[str, Any], model: str, provider: str
    ) -> dict[str, Any]:
        """Run the actual provider call in a thread (SDKs are sync) and translate back."""
        import anyio

        max_tokens = int(body.get("max_tokens", getattr(self._config, "max_tokens", 4096)))
        temperature = float(body.get("temperature", getattr(self._config, "temperature", 0.7)))
        system = body.get("system")
        messages = body.get("messages", [])
        tools = body.get("tools")

        if provider == "bedrock":
            return await anyio.to_thread.run_sync(
                self._call_bedrock, model, system, messages, tools, max_tokens, temperature
            )

        # OpenAI / Azure — both use OpenAI-format chat completions
        oai_messages = translate.anthropic_to_openai_messages(system, messages)
        oai_tools, name_map = translate.anthropic_tools_to_openai(tools)

        def _run() -> dict[str, Any]:
            resp = self._openai_like_call(provider, model, oai_messages, oai_tools, max_tokens, temperature)
            return translate.openai_response_to_anthropic(resp, model, name_map)

        return await anyio.to_thread.run_sync(_run)

    def _openai_like_call(
        self, provider: str, model: str, messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None, max_tokens: int, temperature: float
    ) -> Any:
        """Direct OpenAI/Azure SDK call using the gateway's credentials."""
        import openai

        creds = self._config.credentials
        if provider == "azure":
            client = openai.AzureOpenAI(
                azure_endpoint=creds.azure_endpoint, api_key=creds.azure_api_key,
                api_version=creds.azure_api_version,
            )
            model_name = model.removeprefix("azure/")
        else:
            client = openai.OpenAI(api_key=creds.openai)
            model_name = model.removeprefix("openai/")

        kwargs: dict[str, Any] = {
            "model": model_name, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
        return client.chat.completions.create(**kwargs)

    def _call_bedrock(
        self, model: str, system: Any, messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None, max_tokens: int, temperature: float
    ) -> dict[str, Any]:
        """Bedrock Converse call, translated back to Anthropic Messages."""
        import boto3

        creds = self._config.credentials
        client = boto3.client("bedrock-runtime", region_name=creds.bedrock_region)
        model_id = model.removeprefix("bedrock/")

        br_messages: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role", "user")
            if role == "system":
                continue
            br_messages.append({"role": role, "content": [{"text": translate.text_of(m.get("content", ""))}]})

        kwargs: dict[str, Any] = {
            "modelId": model_id, "messages": br_messages,
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
        sys_text = translate.flatten_system(system)
        if sys_text:
            kwargs["system"] = [{"text": sys_text}]
        resp = client.converse(**kwargs)
        return translate.bedrock_converse_to_anthropic(resp, model)

    # ── tracing / accounting ─────────────────────────────────────────────
    async def _report(
        self, agent_id: str, framework: str, session_id: str, model: str,
        *, tier: str, usage: dict[str, Any] | None = None, reason: str | None = None,
        routed: bool = False, limit_type: str = "", reservation_id: int | None = None,
    ) -> None:
        in_tok = int((usage or {}).get("input_tokens", 0) or 0)
        out_tok = int((usage or {}).get("output_tokens", 0) or 0)

        if tier == "allow" and (in_tok or out_tok):
            cost = 0.0
            if self._quota is not None:
                try:
                    cost = self._quota.calculate_cost(model, in_tok, out_tok)
                    self._quota.record_spend(cost, reservation_id=reservation_id)
                except Exception as e:  # noqa: BLE001
                    log.debug("Spend accounting failed: %s", e)
        elif self._quota is not None and reservation_id is not None:
            self._quota.release_reservation(reservation_id)
            # Record spend against the agent's own budget so per-agent budget
            # caps (authorize_llm -> check_budget) actually decrement.
            if self._agent_auth is not None and cost:
                try:
                    self._agent_auth.record_agent_spend(agent_id, cost)
                except Exception as e:  # noqa: BLE001
                    log.debug("Agent spend accounting failed: %s", e)

        if self._trace is not None:
            try:
                await self._trace.report(
                    action="llm.messages", tier=tier, score=0, duration_ms=0.0,
                    agent_id=agent_id, framework=framework, endpoint=f"llm://{model}",
                    session_id=session_id, model=model, blocked_reason=reason, limit_type=limit_type,
                    params={"input_tokens": in_tok, "output_tokens": out_tok, "routed": routed},
                )
            except Exception as e:  # noqa: BLE001
                log.debug("Trace report failed: %s", e)


def _axon_result_to_anthropic(res: Any, req_tools: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Build an Anthropic Messages dict from an AxonResult (OpenAI-shaped).

    Restores dotted tool names (AxonLLM/OpenAI sanitize '.' → '_') from the
    original request's tool list so Claude Code sees the names it sent.
    """
    name_map: dict[str, str] = {}
    for t in (req_tools or []):
        original = t.get("name", "")
        name_map[original.replace(".", "_")] = original

    content: list[dict[str, Any]] = []
    if res.content:
        content.append({"type": "text", "text": res.content})
    for tc in (res.tool_calls or []):
        fn = tc.get("function", {}) or {}
        raw_name = fn.get("name", tc.get("name", ""))
        args = fn.get("arguments", tc.get("arguments", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except Exception:
                args = {}
        content.append({
            "type": "tool_use",
            "id": tc.get("id", "toolu_axon"),
            "name": name_map.get(raw_name, raw_name),
            "input": args or {},
        })

    stop_reason = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
    return {
        "id": "msg_axon",
        "type": "message",
        "role": "assistant",
        "model": res.model or "",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": res.input_tokens, "output_tokens": res.output_tokens},
    }


def _scrape_usage(buf: bytes) -> dict[str, int]:
    """Best-effort token usage from a tail of Anthropic SSE bytes."""
    try:
        text = buf.decode("utf-8", errors="ignore")
    except Exception:
        return {}
    usage: dict[str, int] = {}
    if (m := re.findall(r'"input_tokens"\s*:\s*(\d+)', text)):
        usage["input_tokens"] = int(m[-1])
    if (m := re.findall(r'"output_tokens"\s*:\s*(\d+)', text)):
        usage["output_tokens"] = int(m[-1])
    return usage
