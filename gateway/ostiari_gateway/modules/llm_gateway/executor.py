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


def _provider_of(model: str) -> str:
    """Classify a model name into a provider family (for per-agent grants)."""
    m = (model or "").lower()
    if m.startswith("bedrock/") or ".anthropic." in m and "amazon" in m:
        return "bedrock"
    if m.startswith("azure/"):
        return "azure"
    if m.startswith("vertex/") or "gemini" in m:
        return "vertex"
    if "gpt" in m or m.startswith("openai/") or "o1" in m or "o3" in m:
        return "openai"
    if "command" in m or "cohere" in m:
        return "cohere"
    if "claude" in m or m.startswith("anthropic"):
        return "anthropic"
    return "anthropic"


def _reinsert_placeholders(
    tool_calls: list[dict[str, Any]], variables: dict[str, str]
) -> list[dict[str, Any]]:
    """Replace concrete variable values in tool-call args with {var} placeholders.

    The inverse of CachedPlan.resolve_with_variables: turns a resolved plan back
    into a template so it can be re-resolved with different values on a cache hit.
    Longer values first so a value that is a substring of another doesn't
    partially clobber it.
    """
    import json as _json

    items = sorted(
        ((k, v) for k, v in variables.items() if v),
        key=lambda kv: len(kv[1]), reverse=True,
    )
    out = []
    for tc in tool_calls:
        args_str = _json.dumps(tc.get("arguments", {}))
        for var_name, var_value in items:
            args_str = args_str.replace(var_value, f"{{{var_name}}}")
        out.append({**tc, "arguments": _json.loads(args_str)})
    return out


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
        broker_policy: Any = None,
        shared_store: Any = None,
    ) -> None:
        self._config = config
        self._manager = manager
        self._mcp_manager = mcp_manager
        self._trace_reporter = trace_reporter
        self._quota_enforcer = quota_enforcer
        self._agent_auth = agent_auth
        self._broker_policy = broker_policy
        self._router = ModelRouter(config)
        self._provider = LLMProvider(config.credentials)
        # AxonLLM is the embedded routing authority. Development retains a
        # direct-provider diagnostic path; production fails closed.
        from ostiari_gateway.modules.llm_gateway.axon_router import AxonRouter
        self._axon = AxonRouter(broker_policy=broker_policy)
        self._security = SecurityLayer(config.security if hasattr(config, "security") else None)
        self._intent_cache = IntentCache(ttl_seconds=300.0, max_entries=200)
        self._cost_reporter = CostReporter(
            control_plane_url=manager.config.control_plane_url,
            sidecar_id=manager.config.sidecar_id,
            quota_enforcer=quota_enforcer,
            broker_policy=broker_policy,
            shared_store=shared_store,
        )

    def update_config(self, config: LLMConfig) -> None:
        self._config = config
        self._router.update_config(config)
        self._provider.update_credentials(config.credentials)
        self._security = SecurityLayer(config.security if hasattr(config, "security") else None)

    async def close(self) -> None:
        """Flush accounting and release embedded Axon provider resources."""
        await self._cost_reporter.close()
        await self._axon.close()

    async def start(self) -> None:
        """Start background recovery of durable usage events."""
        await self._cost_reporter.start_delivery()

    async def invoke(self, request: InvokeRequest) -> InvokeResponse:
        """Run the full agentic loop."""
        # Pass messages to router context for smart routing (task classification)
        router_context = {**request.context, "messages": request.messages}
        # These keys are router output, not caller input. Clear any forged values
        # before model selection so usage cannot be attributed to an experiment
        # the request never entered (especially when model_override skips routing).
        router_context.pop("_ab_experiment", None)
        router_context.pop("_ab_variant", None)
        model = request.model_override or self._router.select_model(router_context)
        experiment_name = str(router_context.get("_ab_experiment", "") or "")
        experiment_variant = str(router_context.get("_ab_variant", "") or "")
        fallback_chain = self._router.get_fallback_chain(model)

        # Authorize the selected model/provider and consume one request from the
        # agent's rolling RPM window. Budget is projected per actual LLM round
        # below because /invoke can make several provider calls.
        agent_id = request.context.get("agent_id", "")
        if self._agent_auth is not None:
            agent_decision = self._agent_auth.check_llm(
                agent_id,
                model,
                _provider_of(model),
                estimated_cost=0.0,
                count_request=True,
            )
            if not agent_decision.allowed:
                return InvokeResponse(
                    response=f"Request blocked: {agent_decision.reason}",
                    model_used=model, total_tokens=0, rounds=0,
                    ab_experiment=experiment_name or None,
                    ab_variant=experiment_variant or None,
                )

        # Build tool specs from registered tools
        tool_specs = self._build_tool_specs(request.tools)

        # Security: injection detection + PII redaction (FAIL-CLOSED — an enabled
        # control that is unavailable, errors, or fires blocks the request).
        # On /invoke the (string-content) messages ARE redacted in place and the
        # redacted set is what we send downstream.
        messages, security_meta = self._security.process_messages(list(request.messages))
        if security_meta.get("blocked"):
            return InvokeResponse(
                response=f"Request blocked: {security_meta.get('block_reason') or 'security policy'}",
                model_used=model,
                total_tokens=0,
                rounds=0,
                ab_experiment=experiment_name or None,
                ab_variant=experiment_variant or None,
            )

        all_tool_calls: list[dict[str, Any]] = []
        blocked_actions: list[dict[str, Any]] = []
        total_tokens = 0

        # Cap max_tokens against quota (silent cap, no rejection)
        effective_max_tokens = self._config.max_tokens
        if self._quota_enforcer:
            effective_max_tokens = self._quota_enforcer.cap_max_tokens(effective_max_tokens)
        if self._agent_auth:
            effective_max_tokens = self._agent_auth.cap_max_tokens(
                agent_id, effective_max_tokens
            )

        # Pre-request budget projection: estimate cost and check quota before calling LLM.
        # NOTE: unlike the interactive chat/messages shims, /invoke is a multi-round
        # agentic loop that books real spend incrementally via the cost reporter
        # (record_spend per round). We deliberately do NOT hold a single in-flight
        # reservation across the whole loop here; the per-round record_spend keeps
        # _total_spend current so a concurrent /invoke sees it on its next round's
        # quota check. The concurrency window is one round, not the whole call.
        if self._quota_enforcer:
            estimated_cost = self._quota_enforcer.estimate_cost(model)
            quota_decision = self._quota_enforcer.check(model=model, estimated_cost=estimated_cost)
            if not quota_decision.allowed:
                return InvokeResponse(
                    response=f"Request blocked by quota: {quota_decision.reason}",
                    model_used=model,
                    total_tokens=0,
                    rounds=0,
                    ab_experiment=experiment_name or None,
                    ab_variant=experiment_variant or None,
                )

        # Intent Cache: check if we've seen this intent before in this agent's session
        # Template mode: if intent_template provided, use it as cache key (excludes variables)
        # (agent_id resolved above at model-authorization time)
        session_id = request.context.get("session_id", "")
        use_template = bool(request.intent_template)
        cache_key_intent = request.intent_template if use_template else (request.messages[-1].get("content", "") if request.messages else "")
        _ = request.messages[-1].get("content", "") if request.messages else ""
        cached_plan = self._intent_cache.get(agent_id, session_id, cache_key_intent)
        # Two distinct facts, previously conflated into one mutated `cache_hit`:
        # `served_from_cache` is what the caller is told (set once, never reassigned),
        # `use_cached_plan` is loop control and is cleared after round 0 so later
        # rounds still call the LLM. Sharing one variable meant the reported value
        # was always the post-reset False — see the returns below.
        served_from_cache = cached_plan is not None
        use_cached_plan = served_from_cache

        for round_num in range(self._config.max_tool_rounds):
            if use_cached_plan and round_num == 0 and cached_plan:
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
                use_cached_plan = False  # only use cache for first round
            else:
                # CACHE MISS or subsequent rounds: call LLM (AxonLLM router if
                # available, else the explicit development diagnostic path).
                agent_reservation_id: int | None = None
                if self._agent_auth is not None:
                    estimated_agent_cost = 0.0
                    if self._quota_enforcer is not None:
                        estimated_agent_cost = self._quota_enforcer.estimate_cost(model)
                    agent_round = self._agent_auth.check_llm(
                        agent_id,
                        model,
                        _provider_of(model),
                        estimated_cost=estimated_agent_cost,
                        reserve=True,
                        count_request=False,
                    )
                    if not agent_round.allowed:
                        await self._cost_reporter.flush()
                        return InvokeResponse(
                            response=f"Request blocked by agent quota: {agent_round.reason}",
                            model_used=model,
                            tool_calls=all_tool_calls,
                            blocked_actions=blocked_actions,
                            total_tokens=total_tokens,
                            rounds=round_num,
                            cache_hit=served_from_cache,
                            ab_experiment=experiment_name or None,
                            ab_variant=experiment_variant or None,
                        )
                    agent_reservation_id = agent_round.reservation_id
                try:
                    llm_response = await self._call_llm(
                        model, fallback_chain, messages, tool_specs, effective_max_tokens,
                        context=request.context,
                    )
                except Exception:
                    if self._agent_auth is not None:
                        self._agent_auth.release_agent_reservation(
                            agent_id, agent_reservation_id
                        )
                    raise
                total_tokens += llm_response.tokens_used
                model_used = llm_response.model

                # Cache the tool plan for this intent (if LLM returned tool calls)
                if round_num == 0 and llm_response.has_tool_calls and agent_id and session_id:
                    plan = [tc.to_dict() for tc in llm_response.tool_calls]
                    if use_template and request.intent_variables:
                        # B5 fix: the LLM resolved the template with THIS call's
                        # variable values. Reverse-map those concrete values back
                        # to {placeholder} tokens before caching, so a later call
                        # with different intent_variables substitutes correctly
                        # (instead of reusing the first call's concrete args).
                        plan = _reinsert_placeholders(plan, request.intent_variables)
                    self._intent_cache.put(
                        agent_id, session_id, cache_key_intent,
                        plan, model_used, is_template=use_template,
                    )

                # Report usage to control plane (fire-and-forget). Use the REAL
                # input/output split from the provider — output is priced 3-5x
                # input, so a 50/50 estimate drifts budgets in both directions.
                in_tok = llm_response.input_tokens
                out_tok = llm_response.output_tokens
                await self._cost_reporter.report(
                    model=llm_response.model,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    total_tokens=llm_response.tokens_used,
                    agent_id=request.context.get("agent_id", "unknown"),
                    action="invoke",
                    provider=llm_response.provider,
                    experiment_name=experiment_name,
                    experiment_variant=experiment_variant,
                )

                # Settle the per-round agent reservation with actual provider
                # usage. This closes the concurrency window without charging a
                # cached tool plan, which skipped the LLM entirely.
                if self._agent_auth is not None and agent_id:
                    cost = 0.0
                    try:
                        if self._quota_enforcer is not None:
                            cost = self._quota_enforcer.calculate_cost(
                                model_used, in_tok, out_tok
                            )
                        self._agent_auth.record_agent_spend(
                            agent_id,
                            cost,
                            reservation_id=agent_reservation_id,
                        )
                    except Exception as e:  # noqa: BLE001
                        log.debug("Agent spend accounting failed: %s", e)
                        self._agent_auth.release_agent_reservation(
                            agent_id, agent_reservation_id
                        )

            if not llm_response.has_tool_calls:
                await self._cost_reporter.flush()
                return InvokeResponse(
                    response=llm_response.content or "",
                    model_used=model_used,
                    tool_calls=all_tool_calls,
                    ab_experiment=experiment_name or None,
                    ab_variant=experiment_variant or None,
                    blocked_actions=blocked_actions,
                    total_tokens=total_tokens,
                    rounds=round_num + 1,
                    cache_hit=served_from_cache,
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
            cache_hit=served_from_cache,
            ab_experiment=experiment_name or None,
            ab_variant=experiment_variant or None,
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
        """Route through AxonLLM, with a development-only direct fallback.

        AxonLLM owns model/provider selection, health-aware fallback, smart
        routing and ensemble. Ensemble/smart are opt-in via context flags:
        ``context["ensemble"]`` (True or preset name) / ``context["smart_routing"]``.
        """
        context = context or {}
        effective_max_tokens = max_tokens or self._config.max_tokens
        # Tool-bearing rounds route through AxonLLM like everything else — it
        # translates tool specs into each provider's dialect. The supports_tools()
        # check is a source-integrity guard: Ostiari pins AxonLLM, but an
        # incompatible override must fail rather than silently drop tool specs.
        from ostiari_gateway.modules.llm_gateway.axon_router import (
            governed_routing_required,
        )

        route_via_axon = self._axon.available and not (tools and not self._axon.supports_tools())
        if tools and self._axon.available and not route_via_axon:
            if governed_routing_required():
                raise RuntimeError(
                    "The embedded AxonLLM version cannot carry tool definitions"
                )
            log.warning(
                "AxonLLM predates tool pass-through — calling the provider directly "
                "for %d tool(s); routing governance and cost tracking are bypassed "
                "for this call. Upgrade AxonLLM.", len(tools),
            )
        if route_via_axon:
            # Only pass the model through if AxonLLM's registry knows it; its names
            # are undated ("claude-sonnet") while ours are dated
            # ("claude-sonnet-4-6"), so a verbatim pass 404s. Unknown → smart-route
            # and let AxonLLM pick something it can serve. Mirrors the shim's
            # _axon_knows guard, which /invoke previously lacked.
            axon_model = primary if self._axon.knows_model(primary) else ""
            try:
                res = await self._axon.route(
                    messages=messages,
                    model=axon_model,
                    max_tokens=effective_max_tokens,
                    temperature=self._config.temperature,
                    tools=tools,
                    smart=bool(context.get("smart_routing")) or not axon_model,
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
                    model=res.model or primary,
                    provider=res.provider,
                    input_tokens=res.input_tokens,
                    output_tokens=res.output_tokens,
                )
            except Exception as e:
                if governed_routing_required():
                    raise RuntimeError("AxonLLM routing failed closed") from e
                log.warning("AxonLLM route failed (%s) — using direct provider fallback", e)

        if governed_routing_required():
            self._axon.require()
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
        if self._broker_policy is not None:
            models = self._broker_policy.require_direct_route(models)
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

        empty_schema = {"type": "object", "properties": {}}

        # HTTP tools. Honor the registered parameter schema when there is one —
        # hardcoding an empty one advertised every tool as taking no arguments.
        for t in self._manager.tool_proxy.list_tools():
            specs.append({
                "name": t["name"],
                "description": t.get("description", ""),
                "schema": t.get("schema") or empty_schema,
            })

        # MCP tools
        if self._mcp_manager is not None:
            for t in self._mcp_manager.list_tools():
                specs.append({
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "schema": t.get("input_schema") or empty_schema,
                })

        # Filter BEFORE the empty check: a filter that matches nothing must yield
        # None (no tools offered), not fall through to an empty list the provider
        # rejects.
        if tool_filter:
            specs = [s for s in specs if s["name"] in tool_filter]

        return specs or None

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
