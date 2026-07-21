"""Agentic loop executor — runs the LLM → validate → execute → respond cycle.

Integrates AxonLLM for:
- Smart routing (task classification → model selection)
- Security (PII redaction, prompt injection detection)
- Cost tracking
- Provider health and fallback

The agentic tool loop (validate → execute → feed back) is Ostiari-native.
"""

import logging
from typing import Any

from ostiari.exceptions import ActionBlockedError
from ostiari_gateway.config_manager import ConfigManager
from ostiari_gateway.intent_cache import IntentCache
from ostiari_gateway.modules.llm_gateway.cost_reporter import CostReporter
from ostiari_gateway.modules.llm_gateway.models import InvokeRequest, InvokeResponse, LLMConfig
from ostiari_gateway.modules.llm_gateway.providers import LLMProvider, LLMResponse, ToolCall
from ostiari_gateway.modules.llm_gateway.router import ModelRouter
from ostiari_gateway.modules.llm_gateway.security import SecurityLayer

log = logging.getLogger("ostiari.sidecar.llm")


def _parse_args(args: Any) -> dict[str, Any]:
    """Tool-call arguments may arrive as a JSON string or a dict."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        import json as _json
        try:
            return _json.loads(args or "{}")
        except Exception:
            return {}
    return {}


class AgenticExecutor:
    """Executes the full agentic loop: LLM → tool calls → validation → execution.

    Powered by AxonLLM for routing/security, Ostiari for tool safety.
    """

    def __init__(
        self, config: LLMConfig, manager: ConfigManager, mcp_manager: Any = None,
        trace_reporter: Any = None, quota_enforcer: Any = None, agent_auth: Any = None,
    ) -> None:
        self._config = config
        self._manager = manager
        self._mcp_manager = mcp_manager
        self._trace_reporter = trace_reporter
        self._quota_enforcer = quota_enforcer
        self._agent_auth = agent_auth
        self._router = ModelRouter(config)
        self._provider = LLMProvider(config.credentials)
        # AxonLLM as the embedded LLM router (health-aware fallback, smart, ensemble,
        # multi-provider) — used when available; otherwise the direct provider path.
        from ostiari_gateway.modules.llm_gateway.axon_router import AxonRouter
        self._axon = AxonRouter()
        self._security = SecurityLayer(config.security if hasattr(config, "security") else None)
        self._intent_cache = IntentCache(ttl_seconds=300.0, max_entries=200)
        self._cost_reporter = CostReporter(
            control_plane_url=manager.config.control_plane_url,
            sidecar_id=manager.config.sidecar_id,
            quota_enforcer=quota_enforcer,
        )

    def update_config(self, config: LLMConfig) -> None:
        self._config = config
        self._router.update_config(config)
        self._provider.update_credentials(config.credentials)
        self._security = SecurityLayer(config.security if hasattr(config, "security") else None)

    async def invoke(self, request: InvokeRequest) -> InvokeResponse:
        """Run the full agentic loop."""
        # Pass messages to router context for smart routing (task classification)
        router_context = {**request.context, "messages": request.messages}
        model = request.model_override or self._router.select_model(router_context)
        fallback_chain = self._router.get_fallback_chain(model)

        # Build tool specs from registered tools
        tool_specs = self._build_tool_specs(request.tools)

        # Security: PII redaction + injection detection
        messages, security_meta = self._security.process_messages(list(request.messages))
        if security_meta.get("injection_detected"):
            return InvokeResponse(
                response="Request blocked: potential prompt injection detected.",
                model_used=model,
                total_tokens=0,
                rounds=0,
            )

        all_tool_calls: list[dict[str, Any]] = []
        blocked_actions: list[dict[str, Any]] = []
        total_tokens = 0

        # Cap max_tokens against quota (silent cap, no rejection)
        effective_max_tokens = self._config.max_tokens
        if self._quota_enforcer:
            effective_max_tokens = self._quota_enforcer.cap_max_tokens(effective_max_tokens)

        # Pre-request budget projection: estimate cost and check quota before calling LLM
        if self._quota_enforcer:
            estimated_cost = self._quota_enforcer.estimate_cost(model)
            quota_decision = self._quota_enforcer.check(model=model, estimated_cost=estimated_cost)
            if not quota_decision.allowed:
                return InvokeResponse(
                    response=f"Request blocked by quota: {quota_decision.reason}",
                    model_used=model,
                    total_tokens=0,
                    rounds=0,
                )

        # Intent Cache: check if we've seen this intent before in this agent's session
        # Template mode: if intent_template provided, use it as cache key (excludes variables)
        agent_id = request.context.get("agent_id", "")
        session_id = request.context.get("session_id", "")
        use_template = bool(request.intent_template)
        cache_key_intent = request.intent_template if use_template else (request.messages[-1].get("content", "") if request.messages else "")
        _ = request.messages[-1].get("content", "") if request.messages else ""
        cached_plan = self._intent_cache.get(agent_id, session_id, cache_key_intent)
        cache_hit = cached_plan is not None

        for round_num in range(self._config.max_tool_rounds):
            if cache_hit and round_num == 0 and cached_plan:
                # CACHE HIT: reuse the tool plan from last time (skip LLM call entirely)
                # If template mode: substitute variables into cached plan arguments
                resolved_calls = cached_plan.resolve_with_variables(request.intent_variables) if use_template else cached_plan.tool_calls
                log.info("Intent cache HIT — reusing tool plan, skipping LLM call ($0). template=%s", use_template)
                llm_response = LLMResponse(
                    content=None,
                    tool_calls=[ToolCall(id=f"cached-{i}", name=tc["name"], arguments=tc["arguments"]) for i, tc in enumerate(resolved_calls)],
                    tokens_used=0,
                    model=cached_plan.model_used,
                )
                model_used = cached_plan.model_used
                cache_hit = False  # only use cache for first round
            else:
                # CACHE MISS or subsequent rounds: call LLM (AxonLLM router if
                # available, else direct provider fallback).
                llm_response = await self._call_llm(
                    model, fallback_chain, messages, tool_specs, effective_max_tokens,
                    context=request.context,
                )
                total_tokens += llm_response.tokens_used
                model_used = llm_response.model

                # Cache the tool plan for this intent (if LLM returned tool calls)
                if round_num == 0 and llm_response.has_tool_calls and agent_id and session_id:
                    # In template mode: cache the plan with variable placeholders intact
                    # The LLM's response arguments will contain the actual values,
                    # but for template caching we store the plan keyed by template
                    self._intent_cache.put(
                        agent_id, session_id, cache_key_intent,
                        [tc.to_dict() for tc in llm_response.tool_calls],
                        model_used,
                        is_template=use_template,
                    )

                # Report usage to control plane (fire-and-forget)
                await self._cost_reporter.report(
                    model=llm_response.model,
                    input_tokens=llm_response.tokens_used // 2,
                    output_tokens=llm_response.tokens_used // 2,
                    total_tokens=llm_response.tokens_used,
                    agent_id=request.context.get("agent_id", "unknown"),
                    action="invoke",
                )

            if not llm_response.has_tool_calls:
                await self._cost_reporter.flush()
                return InvokeResponse(
                    response=llm_response.content or "",
                    model_used=model_used,
                    tool_calls=all_tool_calls,
                    ab_experiment=request.context.get("_ab_experiment"),
                    ab_variant=request.context.get("_ab_variant"),
                    blocked_actions=blocked_actions,
                    total_tokens=total_tokens,
                    rounds=round_num + 1,
                    cache_hit=cached_plan is not None and round_num == 0 and total_tokens == 0,
                )

            # Process tool calls
            tool_results = []
            for tc in llm_response.tool_calls:
                all_tool_calls.append(tc.to_dict())

                # Agent Auth: check if this agent is granted access to this specific tool
                if self._agent_auth:
                    auth_allowed, auth_reason = self._agent_auth.check(
                        request.context.get("agent_id", ""), tc.name
                    )
                    if not auth_allowed:
                        blocked_actions.append({
                            "action": tc.name,
                            "score": 0,
                            "reason": auth_reason,
                        })
                        tool_results.append({
                            "tool_call_id": tc.id,
                            "name": tc.name,
                            "result": f"BLOCKED: {auth_reason}",
                            "blocked": True,
                        })
                        continue

                # Quota Check #2: per-tool execution quota (can the agent afford this tool call?)
                if self._quota_enforcer:
                    tool_quota = self._quota_enforcer.check(model=model_used)
                    if not tool_quota.allowed:
                        blocked_actions.append({
                            "action": tc.name,
                            "score": 0,
                            "reason": f"Quota exceeded before tool execution: {tool_quota.reason}",
                        })
                        tool_results.append({
                            "tool_call_id": tc.id,
                            "name": tc.name,
                            "result": f"BLOCKED: Quota exceeded — {tool_quota.reason}",
                            "blocked": True,
                        })
                        continue

                # Validate with Ostiari Guard (Policy Engine)
                try:
                    self._manager.guard.validate(
                        action=tc.name,
                        params=tc.arguments,
                        context=request.context,
                    )
                except ActionBlockedError as e:
                    blocked_actions.append({
                        "action": tc.name,
                        "score": e.score,
                        "reason": e.reason,
                    })
                    tool_results.append({
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "result": f"BLOCKED: {e.reason}. Try a different approach.",
                        "blocked": True,
                    })
                    if self._trace_reporter:
                        await self._trace_reporter.report(
                            action=tc.name, tier="block", score=e.score, duration_ms=0,
                            agent_id=request.context.get("agent_id", "unknown"),
                            framework=request.context.get("framework", "unknown"),
                            is_mcp=bool(self._mcp_manager and self._mcp_manager.has_tool(tc.name)),
                            blocked_reason=e.reason,
                            endpoint="mcp://" + tc.name.split(".")[0] if "." in tc.name else "",
                            session_id=request.context.get("session_id", ""),
                            plan=request.context.get("plan", ""),
                            step=request.context.get("step", ""),
                            params=tc.arguments,
                            model=model_used,
                        )
                    continue

                # Execute via tool proxy (HTTP) or MCP manager
                import time as _time
                _start = _time.monotonic()
                is_mcp = bool(self._mcp_manager and self._mcp_manager.has_tool(tc.name))
                if is_mcp:
                    result = await self._mcp_manager.call_tool(tc.name, tc.arguments)
                else:
                    result = await self._manager.tool_proxy.execute(tc.name, tc.arguments)
                _duration = (_time.monotonic() - _start) * 1000

                tool_results.append({
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "result": result.get("result", result.get("content", result.get("error", "No result"))),
                    "blocked": False,
                })

                if self._trace_reporter:
                    tool_def = self._manager.tool_proxy.get(tc.name)
                    await self._trace_reporter.report(
                        action=tc.name, tier="allow", score=0, duration_ms=_duration,
                        agent_id=request.context.get("agent_id", "unknown"),
                        framework=request.context.get("framework", "unknown"),
                        is_mcp=is_mcp,
                        endpoint=("mcp://" + tc.name.split(".")[0]) if is_mcp else (tool_def.endpoint if tool_def else ""),
                        session_id=request.context.get("session_id", ""),
                        plan=request.context.get("plan", ""),
                        step=request.context.get("step", ""),
                        params=tc.arguments,
                        model=model_used,
                    )

            # Feed results back to LLM
            messages = self._append_tool_results(messages, llm_response, tool_results)

        # Max rounds reached
        await self._cost_reporter.flush()
        return InvokeResponse(
            response="Max tool rounds reached. Partial results may be available.",
            model_used=model,
            tool_calls=all_tool_calls,
            blocked_actions=blocked_actions,
            total_tokens=total_tokens,
            rounds=self._config.max_tool_rounds,
        )

    async def _call_llm(
        self,
        primary: str,
        fallback_chain: list[str],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Route the call through AxonLLM if available, else the direct provider path.

        AxonLLM owns model/provider selection, health-aware fallback, smart
        routing and ensemble. Ensemble/smart are opt-in via context flags:
        ``context["ensemble"]`` (True or preset name) / ``context["smart_routing"]``.
        """
        context = context or {}
        effective_max_tokens = max_tokens or self._config.max_tokens
        if self._axon.available:
            try:
                res = await self._axon.route(
                    messages=messages,
                    model=primary,
                    max_tokens=effective_max_tokens,
                    temperature=self._config.temperature,
                    tools=tools,
                    smart=bool(context.get("smart_routing")),
                    ensemble=context.get("ensemble", False),
                    agent_id=str(context.get("agent_id", "")),
                    session_id=str(context.get("session_id", "")),
                )
                tcs = [
                    ToolCall(id=tc.get("id", f"call-{i}"),
                             name=(tc.get("function", {}) or {}).get("name", tc.get("name", "")),
                             arguments=_parse_args((tc.get("function", {}) or {}).get("arguments", tc.get("arguments", {}))))
                    for i, tc in enumerate(res.tool_calls)
                ]
                return LLMResponse(
                    content=res.content or None,
                    tool_calls=tcs,
                    tokens_used=res.input_tokens + res.output_tokens,
                    model=res.model or primary,
                )
            except Exception as e:  # noqa: BLE001 — fall back to direct provider path
                log.warning("AxonLLM route failed (%s) — using direct provider fallback", e)

        return self._call_with_fallback(primary, fallback_chain, messages, tools, max_tokens)

    def _call_with_fallback(
        self,
        primary: str,
        fallback_chain: list[str],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Try primary model, fall through to fallback chain on failure."""
        effective_max_tokens = max_tokens or self._config.max_tokens
        models = [primary, *fallback_chain]
        last_error: Exception | None = None

        for model in models:
            try:
                return self._provider.call(
                    model=model,
                    messages=messages,
                    tools=tools,
                    max_tokens=effective_max_tokens,
                    temperature=self._config.temperature,
                )
            except Exception as e:
                last_error = e
                log.warning("Model %s failed: %s. Trying fallback.", model, e)
                continue

        raise RuntimeError(
            f"All models failed. Last error: {last_error}"
        )

    def _build_tool_specs(self, tool_filter: list[str] | None) -> list[dict[str, Any]] | None:
        """Build tool specifications from registered tools (HTTP + MCP)."""
        specs = []

        # HTTP tools
        for t in self._manager.tool_proxy.list_tools():
            specs.append({
                "name": t["name"],
                "description": t.get("description", ""),
                "schema": {"type": "object", "properties": {}},
            })

        # MCP tools
        if self._mcp_manager is not None:
            for t in self._mcp_manager.list_tools():
                specs.append({
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "schema": t.get("input_schema", {"type": "object", "properties": {}}),
                })

        if not specs:
            return None

        if tool_filter:
            specs = [s for s in specs if s["name"] in tool_filter]

        return specs

    def _append_tool_results(
        self,
        messages: list[dict[str, Any]],
        llm_response: LLMResponse,
        tool_results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Append assistant message + tool results in Anthropic-compatible format.

        Anthropic expects:
        - Assistant message with tool_use content blocks
        - User message with tool_result content blocks
        """
        import json as _json

        # Assistant message with tool_use blocks
        assistant_content: list[dict[str, Any]] = []
        if llm_response.content:
            assistant_content.append({"type": "text", "text": llm_response.content})
        for tc in llm_response.tool_calls:
            assistant_content.append({
                "type": "tool_use",
                "id": tc.id,
                "name": tc.name,
                "input": tc.arguments,
            })
        messages.append({"role": "assistant", "content": assistant_content})

        # User message with tool_result blocks
        result_content: list[dict[str, Any]] = []
        for tr in tool_results:
            content = tr["result"]
            if not isinstance(content, str):
                content = _json.dumps(content)
            result_content.append({
                "type": "tool_result",
                "tool_use_id": tr["tool_call_id"],
                "content": content,
            })
        messages.append({"role": "user", "content": result_content})

        return messages
