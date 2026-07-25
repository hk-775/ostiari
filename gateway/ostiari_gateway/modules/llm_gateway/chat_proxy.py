"""OpenAI Chat Completions shim — the Codex CLI (and any OpenAI-SDK client) seam.

Codex CLI is configured with a custom OpenAI-compatible provider in
``~/.codex/config.toml`` pointing at ``<gateway>/v1``; it then calls
``POST /v1/chat/completions``. This shim governs and routes those calls exactly
like the Claude Code ``/v1/messages`` shim, but in the OpenAI wire format the
client already speaks — so no cross-format translation is needed on the way out
(AxonLLM is OpenAI-shaped end to end).

Per call: agent auth (+ per-agent model/provider/budget) -> injection/PII
(fail-closed) -> Ostiari quota -> AxonLLM routing (single-response) -> return
OpenAI ChatCompletion (or SSE stream). Traced as ``llm.chat``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from ostiari_gateway.modules.llm_gateway import translate as T

log = logging.getLogger("ostiari.sidecar.llm.chat")


def _err(status: int, message: str, err_type: str = "invalid_request_error") -> JSONResponse:
    """OpenAI-shaped error so the client surfaces it natively."""
    return JSONResponse(status_code=status, content={
        "error": {"message": message, "type": err_type, "code": None}})


def _provider_of(model: str) -> str:
    m = (model or "").lower()
    if m.startswith("bedrock/") or ("anthropic" in m and "amazon" in m):
        return "bedrock"
    if m.startswith("azure/"):
        return "azure"
    if m.startswith("vertex/") or "gemini" in m:
        return "vertex"
    if "claude" in m or m.startswith("anthropic"):
        return "anthropic"
    if "command" in m or "cohere" in m:
        return "cohere"
    return "openai"


class ChatProxy:
    """Governed OpenAI /v1/chat/completions shim (Codex CLI target)."""

    def __init__(self, config: Any, axon: Any = None, security: Any = None,
                 quota_enforcer: Any = None, trace_reporter: Any = None,
                 agent_auth: Any = None) -> None:
        self._config = config
        self._axon = axon
        self._security = security
        self._quota = quota_enforcer
        self._trace = trace_reporter
        self._agent_auth = agent_auth

    async def handle(self, request: Request) -> Any:
        try:
            body: dict[str, Any] = await request.json()
        except Exception:
            return _err(400, "Malformed JSON body")
        if not isinstance(body, dict) or "messages" not in body:
            return _err(400, "Missing 'messages'")

        agent_id = request.headers.get("X-Agent-Id", "unknown")
        framework = request.headers.get("X-Framework", "codex")
        session_id = (request.headers.get("X-Session-Id")
                      or request.headers.get("x-codex-session-id", ""))
        requested_model = body.get("model", "")
        streaming = bool(body.get("stream", False))
        messages = body.get("messages", [])

        # ── Gate 1: agent authorization (endpoint + model/provider/budget) ──
        if self._agent_auth:
            allowed, reason = self._agent_auth.check(agent_id, "/v1/chat/completions")
            if not allowed:
                await self._report(agent_id, framework, session_id, requested_model,
                                    tier="block", reason=reason, limit_type="agent_authorization")
                return _err(403, reason or "Agent not authorized", "permission_error")
            allowed, reason = self._agent_auth.authorize_llm(
                agent_id, requested_model, _provider_of(requested_model))
            if not allowed:
                await self._report(agent_id, framework, session_id, requested_model,
                                    tier="block", reason=reason, limit_type="agent_authorization")
                return _err(403, reason, "permission_error")

        # ── Gate 2: security (injection/PII), fail-closed ───────────────────
        if self._security is not None:
            flat = [{"role": m.get("role", "user"), "content": T.text_of(m.get("content", ""))}
                    for m in messages]
            _, meta = self._security.process_messages(flat)
            if meta.get("blocked") or meta.get("pii_redacted"):
                reason = meta.get("block_reason") or "PII detected in prompt"
                limit_type = "prompt_injection" if meta.get("injection_detected") else "pii"
                await self._report(agent_id, framework, session_id, requested_model,
                                    tier="block", reason=reason, limit_type=limit_type)
                return _err(403, f"Request blocked by Ostiari: {reason}", "permission_error")

        # ── Gate 3: Ostiari quota ceiling ───────────────────────────────────
        # reserve=True books the estimated cost as an in-flight reservation so
        # concurrent calls can't all pass on the same stale spend total. The
        # reservation is released/settled in _report (or self-expires on TTL).
        reservation_id: int | None = None
        if self._quota is not None:
            try:
                est = self._quota.estimate_cost(requested_model)
                decision = self._quota.check(model=requested_model, estimated_cost=est,
                                             reserve=True)
                if not decision.allowed:
                    await self._report(agent_id, framework, session_id, requested_model,
                                        tier="block", reason=decision.reason, limit_type="quota")
                    return _err(429, f"Request blocked by quota: {decision.reason}", "rate_limit_error")
                reservation_id = decision.reservation_id
                self._quota.record_request()
            except Exception as e:  # noqa: BLE001
                log.debug("Quota check failed: %s", e)

        # ── Route via AxonLLM (single-response; no ensemble on the shim) ────
        if self._axon is None or not self._axon.available:
            return _err(503, "LLM router unavailable", "api_error")

        axon_model = requested_model if self._axon_knows(requested_model) else ""
        try:
            res = await self._axon.route(
                messages=messages,
                model=axon_model,
                max_tokens=int(body.get("max_tokens", getattr(self._config, "max_tokens", 4096))),
                temperature=float(body.get("temperature", getattr(self._config, "temperature", 0.7))),
                tools=body.get("tools"),
                smart=not axon_model,
                ensemble=False,
                agent_id=agent_id,
                session_id=session_id,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Codex shim route failed: %s", e)
            if self._quota is not None:
                self._quota.release_reservation(reservation_id)
            await self._report(agent_id, framework, session_id, requested_model,
                                tier="block", reason=f"router error: {e}", limit_type="router")
            return _err(502, f"Upstream routing failed: {e}", "api_error")

        await self._report(agent_id, framework, session_id, res.model or requested_model,
                           tier="allow",
                           usage={"input_tokens": res.input_tokens, "output_tokens": res.output_tokens},
                           routed=(res.model or "") != requested_model,
                           reservation_id=reservation_id)

        completion = _openai_completion(res)
        if not streaming:
            return JSONResponse(status_code=200, content=completion)

        def gen() -> Any:
            for chunk in _openai_sse(completion):
                yield chunk

        return StreamingResponse(gen(), media_type="text/event-stream")

    def _axon_knows(self, model: str) -> bool:
        if not model:
            return False
        try:
            reg = getattr(getattr(self._axon, "_agent", None), "router", None)
            reg = getattr(reg, "model_registry", None)
            return bool(reg and model in reg.models)
        except Exception:  # noqa: BLE001
            return False

    async def _report(self, agent_id: str, framework: str, session_id: str, model: str,
                      *, tier: str, usage: dict[str, Any] | None = None,
                      reason: str | None = None, routed: bool = False,
                      limit_type: str = "", reservation_id: int | None = None) -> None:
        in_tok = int((usage or {}).get("input_tokens", 0) or 0)
        out_tok = int((usage or {}).get("output_tokens", 0) or 0)
        if self._quota is not None and tier == "allow" and (in_tok or out_tok):
            try:
                cost = self._quota.calculate_cost(model, in_tok, out_tok)
                self._quota.record_spend(cost, reservation_id=reservation_id)
                if self._agent_auth is not None and cost:
                    self._agent_auth.record_agent_spend(agent_id, cost)
            except Exception as e:  # noqa: BLE001
                log.debug("Spend accounting failed: %s", e)
        elif self._quota is not None and reservation_id is not None:
            # Not recording spend on this path — release the reservation so it
            # doesn't linger until TTL.
            self._quota.release_reservation(reservation_id)
        if self._trace is not None:
            try:
                await self._trace.report(
                    action="llm.chat", tier=tier, score=0, duration_ms=0.0,
                    agent_id=agent_id, framework=framework, endpoint=f"llm://{model}",
                    session_id=session_id, model=model, blocked_reason=reason, limit_type=limit_type,
                    params={"input_tokens": in_tok, "output_tokens": out_tok, "routed": routed})
            except Exception as e:  # noqa: BLE001
                log.debug("Trace report failed: %s", e)


def _openai_completion(res: Any) -> dict[str, Any]:
    """Build an OpenAI ChatCompletion dict from an AxonResult.

    Prefers the raw OpenAI-shaped response AxonLLM already returned; falls back
    to assembling one from the normalized fields.
    """
    raw = getattr(res, "raw", None)
    if isinstance(raw, dict) and raw.get("choices"):
        # Already OpenAI-shaped (id/choices/usage/model) — pass through.
        raw.setdefault("object", "chat.completion")
        return raw
    message: dict[str, Any] = {"role": "assistant", "content": res.content or ""}
    if getattr(res, "tool_calls", None):
        message["tool_calls"] = res.tool_calls
        message["content"] = res.content or None
    return {
        "id": "chatcmpl-ostiari",
        "object": "chat.completion",
        "model": res.model or "",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
        "usage": {"prompt_tokens": res.input_tokens, "completion_tokens": res.output_tokens,
                  "total_tokens": res.input_tokens + res.output_tokens},
    }


def _openai_sse(completion: dict[str, Any]):
    """Re-emit a completed ChatCompletion as OpenAI streaming chunks + [DONE].

    Buffered-then-chunked (like the messages shim's cross-provider path): one
    role delta, one content delta, a finish chunk, then the [DONE] sentinel.
    """
    choice = (completion.get("choices") or [{}])[0]
    msg = choice.get("message", {})
    base = {"id": completion.get("id", "chatcmpl-ostiari"), "object": "chat.completion.chunk",
            "model": completion.get("model", "")}

    def _chunk(delta: dict[str, Any], finish: Any = None) -> str:
        return "data: " + json.dumps({
            **base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"

    yield _chunk({"role": "assistant"})
    if msg.get("content"):
        yield _chunk({"content": msg["content"]})
    if msg.get("tool_calls"):
        yield _chunk({"tool_calls": msg["tool_calls"]})
    yield _chunk({}, finish=choice.get("finish_reason", "stop"))
    yield "data: [DONE]\n\n"
