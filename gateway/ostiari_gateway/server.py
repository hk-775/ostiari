"""Generic sidecar server — validates and proxies tool calls to remote endpoints."""

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace

from ostiari.exceptions import ActionBlockedError
from ostiari_gateway.config_manager import ConfigManager
from ostiari_gateway.models import PolicyConfig, SidecarConfig, ToolDefinition
from ostiari_gateway.telemetry import (
    extract_context_from_headers,
    get_tracer,
    init_telemetry,
    inject_context_into_headers,
    record_proxy_result,
    record_validate_result,
    start_proxy_span,
    start_validate_span,
)

log = logging.getLogger("ostiari.sidecar")


def _shadow_response(
    action: str,
    would_block_type: str,
    reason: str,
    *,
    score: int | None = None,
    rule_id: str | None = None,
) -> dict[str, Any]:
    """Build the response for a call that was blocked-in-shadow.

    Returns HTTP 200 with a synthetic (side-effect-free) result and shadow
    metadata, so the caller behaves as if the tool ran while the gateway
    records that enforce mode WOULD have blocked it.
    """
    body: dict[str, Any] = {
        "result": {"shadow": True, "note": "shadowed — tool not executed"},
        "action": action,
        "shadow": True,
        "would_block": True,
        "would_block_type": would_block_type,
        "reason": reason,
        "duration_ms": 0,
    }
    if score is not None:
        body["score"] = score
    if rule_id is not None:
        body["rule_id"] = rule_id
    return body


def _synthesize_from_schema(schema: dict | None) -> Any:
    """Generate a plausible value from a JSON-Schema-style dict.

    Best-effort: handles object/array/string/number/integer/boolean and enum.
    Used so shadow responses look like the real tool's output shape instead of
    a generic marker. Returns None when the schema is empty/unknown.
    """
    if not isinstance(schema, dict):
        return None
    if "enum" in schema and isinstance(schema["enum"], list) and schema["enum"]:
        return schema["enum"][0]
    if "default" in schema:
        return schema["default"]

    t = schema.get("type")
    if t == "object" or "properties" in schema:
        props = schema.get("properties", {})
        return {k: _synthesize_from_schema(v) for k, v in props.items()}
    if t == "array":
        item = _synthesize_from_schema(schema.get("items", {}))
        return [item] if item is not None else []
    if t == "string":
        return schema.get("example", "sample")
    if t in ("number", "integer"):
        return 0
    if t == "boolean":
        return True
    return None


def _shadow_execute_response(
    action: str, endpoint: str = "", schema: dict | None = None
) -> dict[str, Any]:
    """Synthetic response for an ALLOWED call under shadow mode (tool not run).

    If the tool declares an output schema, synthesize a response shaped to it;
    otherwise return a generic shadow marker.
    """
    synthesized = _synthesize_from_schema(schema)
    if synthesized is not None:
        result: Any = {"shadow": True, "synthesized": synthesized}
    else:
        result = {"shadow": True, "note": "shadowed — tool not executed", "endpoint": endpoint}
    return {
        "result": result,
        "action": action,
        "shadow": True,
        "would_block": False,
        "duration_ms": 0,
    }


def create_app(initial_config: SidecarConfig | None = None) -> FastAPI:
    """Create the generic sidecar FastAPI app."""
    from ostiari_gateway.agent_auth import AgentAuthPolicy
    from ostiari_gateway.mcp import MCPManager
    from ostiari_gateway.modules import ModuleRegistry
    from ostiari_gateway.quota_enforcer import QuotaEnforcer
    from ostiari_gateway.trace_reporter import TraceReporter

    init_telemetry(gateway_id=initial_config.sidecar_id if initial_config else "")

    from ostiari_gateway.a2a.manager import A2AManager

    manager = ConfigManager()
    mcp_manager = MCPManager()
    a2a_manager = A2AManager()
    module_registry = ModuleRegistry()
    module_registry.discover()
    from ostiari_gateway.cross_agent import CrossAgentPolicy

    quota_enforcer = QuotaEnforcer()
    agent_auth = AgentAuthPolicy()
    cross_agent = CrossAgentPolicy()

    # Payment gate — mode chosen by env: simulated (default, no chain) or live.
    import os as _os

    from ostiari_gateway.payments import PaymentGate, SimulatedSettler, X402Settler, parse_402
    if _os.environ.get("OSTIARI_X402_MODE", "simulated").lower() == "live":
        _settler = X402Settler(facilitator_url=_os.environ.get("OSTIARI_X402_FACILITATOR", ""))
    else:
        _settler = SimulatedSettler()
    payment_gate = PaymentGate(settler=_settler)
    trace_reporter = TraceReporter(
        control_plane_url=(initial_config.control_plane_url if initial_config else ""),
        sidecar_id=(initial_config.sidecar_id if initial_config else ""),
    )

    # Apply quota from initial config
    if initial_config and hasattr(initial_config, "quota") and initial_config.quota:
        quota_enforcer.configure(initial_config.quota)

    # Apply agent auth from initial config
    if initial_config and hasattr(initial_config, "agent_auth") and initial_config.agent_auth:
        agent_auth.configure(initial_config.agent_auth)

    # Apply cross-agent (A2A delegation) policy from initial config
    if initial_config and getattr(initial_config, "cross_agent", None):
        cross_agent.configure(initial_config.cross_agent)

    # Apply payment config from initial config
    if initial_config and getattr(initial_config, "payments", None):
        payment_gate.configure(initial_config.payments)

    if initial_config is not None:
        manager.apply_config(initial_config)

    # Lifecycle manager (CP registration + heartbeat)
    lifecycle = None
    if initial_config and initial_config.control_plane_url:
        from ostiari_gateway.lifecycle import LifecycleManager

        lifecycle = LifecycleManager(
            gateway_id=initial_config.sidecar_id,
            control_plane_url=initial_config.control_plane_url,
        )

        def _apply_bundle(bundle: dict) -> None:
            """Apply a config bundle from the control plane."""
            from ostiari_gateway.models import PolicyConfig, ToolDefinition

            # Apply tools
            if "tools" in bundle:
                tools = [ToolDefinition(**t) for t in bundle["tools"]]
                manager.apply_tools(tools)

            # Apply policy
            if "policy" in bundle:
                policy = PolicyConfig(**bundle["policy"])
                manager.apply_policy(policy)

            # Apply quota
            if "quotas" in bundle and bundle["quotas"]:
                quota_enforcer.configure(bundle["quotas"])
            elif "quota" in bundle and bundle["quota"]:
                quota_enforcer.configure(bundle["quota"])

            # Apply agent auth
            if "agent_auth" in bundle and bundle["agent_auth"]:
                agent_auth.configure(bundle["agent_auth"])

            # Apply payment config (pricing + wallets)
            if "payments" in bundle and bundle["payments"]:
                payment_gate.configure(bundle["payments"])

        lifecycle.set_config_callback(_apply_bundle)

    @asynccontextmanager
    async def lifespan(app: Any) -> Any:
        # Initialize MCP servers from config
        if initial_config and hasattr(initial_config, "mcp_servers"):
            for mcp_cfg in initial_config.mcp_servers:
                from ostiari_gateway.mcp.models import MCPServerConfig
                server_config = MCPServerConfig(**mcp_cfg) if isinstance(mcp_cfg, dict) else mcp_cfg
                await mcp_manager.add_server(server_config)

        # Register with control plane and start heartbeat
        if lifecycle:
            try:
                await lifecycle.register()
                await lifecycle.start_heartbeat(interval=30)
            except Exception as e:
                log.warning(f"Control plane registration failed: {e} — running standalone")

        yield

        # Shutdown lifecycle
        if lifecycle:
            await lifecycle.stop()
        await trace_reporter.close()
        await mcp_manager.shutdown()
        await a2a_manager.shutdown()
        module_registry.shutdown_all()
        await manager.shutdown()

    app = FastAPI(title="Ostiari Sidecar", lifespan=lifespan)

    # Activate modules based on config
    if initial_config and initial_config.modules.llm_gateway:
        from ostiari_gateway.modules.llm_gateway.models import LLMConfig

        llm_config = LLMConfig(**initial_config.llm) if initial_config.llm else LLMConfig()
        module_registry.activate(
            "llm_gateway", app, {"manager": manager, "llm_config": llm_config, "mcp_manager": mcp_manager, "trace_reporter": trace_reporter, "quota_enforcer": quota_enforcer, "agent_auth": agent_auth}
        )

    # ─── Tool Execution Endpoints ─────────────────────────────────────────

    @app.post("/tool/{action}")
    async def proxy_tool(action: str, request: Request) -> Any:
        """Validate a tool call, then proxy to its remote endpoint.

        In shadow mode, policy gates evaluate but never block, and tools are
        not really executed (a synthetic response is returned). Every decision
        is reported with shadow=True and would_block set when enforce mode
        would have blocked the call.
        """
        params: dict[str, Any] = await request.json()
        agent_id = request.headers.get("X-Agent-Id", "unknown")
        framework = request.headers.get("X-Framework", "unknown")
        session_id = request.headers.get("X-Session-Id", "")
        plan = request.headers.get("X-Plan", "")
        step = request.headers.get("X-Step", "")

        # Delegation provenance: the chain of agents that led to this call.
        # An inbound X-Delegation-Chain means this request itself arrived via a
        # prior agent's delegation; append the current agent to extend it.
        incoming_chain = request.headers.get("X-Delegation-Chain", "")
        delegation_chain = [c for c in incoming_chain.split(">") if c]
        if agent_id and agent_id not in delegation_chain:
            delegation_chain.append(agent_id)

        shadow = manager.config.mode == "shadow"

        # Extract OTel context from incoming request (if present)
        incoming_headers = dict(request.headers)
        parent_ctx = extract_context_from_headers(incoming_headers)
        tracer = get_tracer()

        # Check tool exists (HTTP tools first, then MCP tools, then A2A agents)
        tool = manager.tool_proxy.get(action)
        is_mcp_tool = False
        is_a2a_tool = action.startswith("a2a.")
        if tool is None and not is_a2a_tool:
            if mcp_manager.has_tool(action):
                is_mcp_tool = True
            else:
                all_tools = [t["name"] for t in manager.tool_proxy.list_tools()]
                all_tools.extend(t["name"] for t in mcp_manager.list_tools())
                all_tools.extend(f"a2a.{a['name']}" for a in a2a_manager.list_agents())
                return JSONResponse(
                    status_code=404,
                    content={"error": f"Unknown tool: {action}", "available": all_tools},
                )
        if is_a2a_tool:
            agent_key = action[len("a2a."):]
            card = a2a_manager.get_agent_card(agent_key)
            if card is None:
                return JSONResponse(
                    status_code=404,
                    content={"error": f"A2A agent not connected: {agent_key}",
                             "available": [f"a2a.{a['name']}" for a in a2a_manager.list_agents()]},
                )

            # Cross-agent delegation gate: may `agent_id` delegate to `agent_key`?
            # (edge rules + callee trust score + chain-depth guard)
            xa_allowed, xa_reason = cross_agent.check(agent_id, agent_key, chain=delegation_chain)
            if not xa_allowed:
                # Report the block either way so the delegation report captures
                # both real (enforce) and would-be (shadow) blocks.
                await trace_reporter.report(
                    action=action, tier="block", score=0, duration_ms=0,
                    agent_id=agent_id, framework=framework, blocked_reason=xa_reason,
                    endpoint=f"a2a://{agent_key}",
                    session_id=session_id, plan=plan, step=step, params=params,
                    shadow=shadow, would_block=True, delegation_chain=delegation_chain,
                    limit_type="cross_agent_delegation",
                )
                if not shadow:
                    return JSONResponse(
                        status_code=403,
                        content={
                            "blocked": True,
                            "action": action,
                            "reason": xa_reason,
                            "limit_type": "cross_agent_delegation",
                            "delegation_chain": delegation_chain,
                        },
                    )
                return _shadow_response(action, "cross_agent_delegation", xa_reason)

        # Agent authorization check (least privilege — before everything else)
        auth_allowed, auth_reason = agent_auth.check(agent_id, action)
        if not auth_allowed:
            if not shadow:
                return JSONResponse(
                    status_code=403,
                    content={
                        "blocked": True,
                        "action": action,
                        "reason": auth_reason,
                        "limit_type": "agent_authorization",
                    },
                )
            await trace_reporter.report(
                action=action, tier="block", score=0, duration_ms=0,
                agent_id=agent_id, framework=framework, blocked_reason=auth_reason,
                session_id=session_id, plan=plan, step=step, params=params,
                shadow=True, would_block=True,
            )
            return _shadow_response(action, "agent_authorization", auth_reason)

        # Quota check (before validation)
        quota_decision = quota_enforcer.check()
        if not quota_decision.allowed:
            if not shadow:
                return JSONResponse(
                    status_code=429,
                    content={
                        "blocked": True,
                        "action": action,
                        "reason": quota_decision.reason,
                        "limit_type": quota_decision.limit_type,
                    },
                )
            await trace_reporter.report(
                action=action, tier="block", score=0, duration_ms=0,
                agent_id=agent_id, framework=framework, blocked_reason=quota_decision.reason,
                session_id=session_id, plan=plan, step=step, params=params,
                shadow=True, would_block=True,
            )
            return _shadow_response(action, quota_decision.limit_type, quota_decision.reason)
        quota_enforcer.record_request()

        # Validate with Ostiari Guard (with tracing)
        validate_span = start_validate_span(tracer, action, agent_id, framework, parent_ctx)
        try:
            result = manager.guard.validate(
                action=action,
                params=params,
                context={"agent_id": agent_id, "framework": framework},
            )
            record_validate_result(validate_span, result.tier, result.score, blocked=False)
        except ActionBlockedError as e:
            record_validate_result(validate_span, "block", e.score, blocked=True)
            await trace_reporter.report(
                action=action, tier="block", score=e.score, duration_ms=0,
                agent_id=agent_id, framework=framework, is_mcp=is_mcp_tool,
                blocked_reason=e.reason,
                endpoint=tool.endpoint if tool else "mcp://" if is_mcp_tool else "",
                session_id=session_id, plan=plan, step=step, params=params,
                shadow=shadow, would_block=shadow,
            )
            if not shadow:
                return JSONResponse(
                    status_code=403,
                    content={
                        "blocked": True,
                        "action": action,
                        "score": e.score,
                        "reason": e.reason,
                        "rule_id": e.rule_id,
                    },
                )
            # Shadow mode: policy WOULD block, but we let it proceed to a mocked
            # execution so the caller sees a realistic (side-effect-free) response.
            return _shadow_response(action, "policy", e.reason, score=e.score, rule_id=e.rule_id)

        # Shadow mode: the call is ALLOWED, but we never run the real tool
        # (no real emails, DB writes, downstream agent calls). Return a
        # synthetic response and record the allowed decision.
        if shadow:
            endpoint = (
                tool.endpoint if tool
                else f"a2a://{action[len('a2a.'):]}" if is_a2a_tool
                else f"mcp://{action.split('.')[0]}" if is_mcp_tool
                else ""
            )
            await trace_reporter.report(
                action=action, tier="allow", score=result.score, duration_ms=0,
                agent_id=agent_id, framework=framework, is_mcp=is_mcp_tool,
                endpoint=endpoint,
                session_id=session_id, plan=plan, step=step, params=params,
                shadow=True, would_block=False,
            )
            return _shadow_execute_response(
                action, endpoint, schema=tool.schema_ if tool else None
            )

        # Payment gate (metered mode): price the call and settle from the agent
        # wallet BEFORE execution. In off/passthrough mode this is a no-op here
        # (passthrough settles reactively on a downstream 402, below). Runs after
        # the safety/risk checks so we never charge for a call we'd block, and
        # is skipped in shadow mode (which returned above — no real money moves).
        pay = await payment_gate.charge_before(agent_id=agent_id, action=action)
        if not pay.settled:
            await trace_reporter.report(
                action=action, tier="block", score=0, duration_ms=0,
                agent_id=agent_id, framework=framework, is_mcp=is_mcp_tool,
                blocked_reason=pay.reason,
                endpoint=tool.endpoint if tool else "",
                session_id=session_id, plan=plan, step=step, params=params,
                limit_type="payment",
            )
            await trace_reporter.report_payment(
                agent_id=agent_id, action=action, amount_usdc=pay.amount_usdc,
                settled=False, mode=payment_gate.settler_mode, source="policy",
            )
            return JSONResponse(
                status_code=402,
                content={
                    "blocked": True,
                    "action": action,
                    "reason": pay.reason,
                    "limit_type": "payment",
                    "amount_usdc": pay.amount_usdc,
                    "wallet_balance_usdc": pay.balance_usdc,
                },
            )
        if not pay.free:
            await trace_reporter.report_payment(
                agent_id=agent_id, action=action, amount_usdc=pay.amount_usdc,
                settled=True, tx_hash=pay.receipt.tx_hash if pay.receipt else "",
                mode=payment_gate.settler_mode, source="policy",
            )

        # Execute: route to A2A agent, MCP client, or HTTP proxy
        if is_a2a_tool:
            import time as _time
            agent_key = action[len("a2a."):]
            # Accept either a raw text message or an A2A JSON-RPC envelope
            message = params.get("message")
            if isinstance(message, dict):
                text_parts = [p.get("text", "") for p in message.get("parts", []) if isinstance(p, dict)]
                message = " ".join(t for t in text_parts if t)
            if not message:
                inner = params.get("params", {}).get("message", {})
                text_parts = [p.get("text", "") for p in inner.get("parts", []) if isinstance(p, dict)]
                message = " ".join(t for t in text_parts if t)
            proxy_span = start_proxy_span(tracer, action, f"a2a://{agent_key}", "CALL", parent_ctx)
            _start = _time.monotonic()
            # Forward delegation provenance so the downstream agent's gateway
            # sees the real caller and the full chain (prevents privilege
            # laundering and enables end-to-end audit).
            delegation_headers = {
                "X-Agent-Id": agent_id,
                "X-Delegation-Chain": ">".join(delegation_chain),
            }
            if session_id:
                delegation_headers["X-Session-Id"] = session_id
            a2a_result = await a2a_manager.call_agent(
                agent_key, message or "", headers=delegation_headers
            )
            _duration = (_time.monotonic() - _start) * 1000
            has_error = "error" in a2a_result
            record_proxy_result(
                proxy_span, status_code=500 if has_error else 200,
                duration_ms=_duration, error=a2a_result.get("error"),
            )
            await trace_reporter.report(
                action=action, tier="allow" if not has_error else "error",
                score=result.score, duration_ms=_duration,
                agent_id=agent_id, framework=framework or "a2a", is_mcp=False,
                endpoint=f"a2a://{agent_key}",
                session_id=session_id, plan=plan, step=step, params=params,
                delegation_chain=delegation_chain,
            )
            if has_error:
                return JSONResponse(status_code=502, content=a2a_result)
            # Wrap in a JSON-RPC-style result so the Sandbox A2A tab can unwrap it
            return {"result": {"result": {
                "id": a2a_result.get("task_id", ""),
                "status": {"state": a2a_result.get("state", "completed")},
                "history": [
                    {"role": "user", "parts": [{"type": "text", "text": message or ""}]},
                    {"role": "agent", "parts": [{"type": "text", "text": a2a_result.get("response", "")}]},
                ],
                "artifacts": a2a_result.get("artifacts", []),
            }}, "action": action, "duration_ms": round(_duration, 2)}
        if is_mcp_tool:
            # MCP tool — call via MCP manager (in-process or remote, depending on config)
            import time as _time
            proxy_span = start_proxy_span(tracer, action, "mcp://", "CALL", parent_ctx)
            _start = _time.monotonic()
            proxy_result = await mcp_manager.call_tool(action, params)
            _duration = (_time.monotonic() - _start) * 1000
            has_error = "error" in proxy_result
            record_proxy_result(
                proxy_span,
                status_code=500 if has_error else 200,
                duration_ms=_duration,
                error=proxy_result.get("error"),
            )
            await trace_reporter.report(
                action=action, tier="allow" if not has_error else "error",
                score=result.score, duration_ms=_duration,
                agent_id=agent_id, framework=framework, is_mcp=True,
                endpoint=f"mcp://{action.split('.')[0]}",
                session_id=session_id, plan=plan, step=step, params=params,
            )
            if has_error:
                return JSONResponse(status_code=500, content=proxy_result)
            return {"result": proxy_result, "action": action, "duration_ms": round(_duration, 2)}
        else:
            # HTTP tool — proxy to remote endpoint (with tracing + context propagation)
            proxy_span = start_proxy_span(
                tracer, action, tool.endpoint, tool.method, parent_ctx
            )
            propagation_headers: dict[str, str] = {}
            with trace.use_span(proxy_span, end_on_exit=False):
                inject_context_into_headers(propagation_headers)

            proxy_result = await manager.tool_proxy.execute(
                action, params, propagate_headers=propagation_headers
            )

            # Passthrough x402: the tool demanded payment (HTTP 402). Settle from
            # the agent wallet, then retry the call carrying the X-PAYMENT proof.
            quote_402 = parse_402(
                proxy_result.get("result"), proxy_result.get("status_code", 200), action
            )
            if quote_402 is not None and payment_gate.mode == "passthrough":
                pay = await payment_gate.settle_402(
                    agent_id=agent_id, action=action, quote=quote_402
                )
                if not pay.settled:
                    await trace_reporter.report(
                        action=action, tier="block", score=0,
                        duration_ms=proxy_result.get("duration_ms", 0),
                        agent_id=agent_id, framework=framework, is_mcp=False,
                        blocked_reason=pay.reason, endpoint=tool.endpoint,
                        session_id=session_id, plan=plan, step=step, params=params,
                        limit_type="payment",
                    )
                    await trace_reporter.report_payment(
                        agent_id=agent_id, action=action, amount_usdc=pay.amount_usdc,
                        settled=False, mode=payment_gate.settler_mode, source="tool_402",
                    )
                    return JSONResponse(
                        status_code=402,
                        content={
                            "blocked": True, "action": action, "reason": pay.reason,
                            "limit_type": "payment", "amount_usdc": pay.amount_usdc,
                            "wallet_balance_usdc": pay.balance_usdc,
                        },
                    )
                await trace_reporter.report_payment(
                    agent_id=agent_id, action=action, amount_usdc=pay.amount_usdc,
                    settled=True, tx_hash=pay.receipt.tx_hash if pay.receipt else "",
                    mode=payment_gate.settler_mode, source="tool_402",
                )
                # Paid — retry with the payment proof header.
                proxy_result = await manager.tool_proxy.execute(
                    action, params,
                    propagate_headers={**propagation_headers, **pay.retry_header},
                )

            record_proxy_result(
                proxy_span,
                status_code=proxy_result.get("status_code", 500),
                duration_ms=proxy_result.get("duration_ms", 0),
                error=proxy_result.get("error"),
            )

            await trace_reporter.report(
                action=action, tier="allow" if proxy_result.get("status_code", 200) < 400 else "error",
                score=result.score, duration_ms=proxy_result.get("duration_ms", 0),
                agent_id=agent_id, framework=framework, is_mcp=False,
                endpoint=tool.endpoint,
                session_id=session_id, plan=plan, step=step, params=params,
            )

            if proxy_result.get("status_code", 200) >= 400:
                return JSONResponse(
                    status_code=proxy_result["status_code"],
                    content=proxy_result,
                )

        return {
            "result": proxy_result.get("result"),
            "action": action,
            "duration_ms": proxy_result.get("duration_ms"),
        }

    @app.post("/validate")
    async def validate_only(request: Request) -> Any:
        """Validate a tool call without executing it."""
        body: dict[str, Any] = await request.json()
        action = body.get("action", "")
        params = body.get("params", {})
        agent_id = request.headers.get("X-Agent-Id", "unknown")
        framework = request.headers.get("X-Framework", "unknown")

        if not action:
            return JSONResponse(status_code=400, content={"error": "action is required"})

        try:
            result = manager.guard.validate(
                action=action,
                params=params,
                context={"agent_id": agent_id, "framework": framework},
            )
            return {
                "allowed": True,
                "action": action,
                "score": result.score,
                "tier": result.tier,
            }
        except ActionBlockedError as e:
            return {
                "allowed": False,
                "action": action,
                "score": e.score,
                "tier": "block",
                "reason": e.reason,
                "rule_id": e.rule_id,
            }

    # ─── Control Plane Config Endpoints ───────────────────────────────────

    @app.post("/config")
    async def apply_full_config(request: Request) -> Any:
        """Apply full sidecar configuration (tools + policy)."""
        body = await request.json()
        config = SidecarConfig(**body)
        result = manager.apply_config(config)
        # Update trace reporter if control_plane_url changed
        if config.control_plane_url:
            trace_reporter.configure(config.control_plane_url, config.sidecar_id)
        return {"status": "applied", **result}

    @app.get("/config")
    async def get_config() -> Any:
        """Return current sidecar configuration."""
        return manager.config.model_dump(mode="json", by_alias=True)

    @app.get("/config/mode")
    async def get_mode() -> Any:
        """Return the current enforcement mode (enforce | shadow)."""
        return {"mode": manager.config.mode}

    @app.post("/config/mode")
    async def set_mode(request: Request) -> Any:
        """Set the enforcement mode. In 'shadow' mode the gateway evaluates
        policy but never blocks and never runs real tool side effects."""
        body = await request.json()
        mode = body.get("mode", "enforce")
        if mode not in ("enforce", "shadow"):
            return JSONResponse(status_code=400, content={"error": "mode must be 'enforce' or 'shadow'"})
        manager.config.mode = mode
        return {"status": "applied", "mode": mode}

    @app.post("/config/tools")
    async def apply_tools(request: Request) -> Any:
        """Hot-reload all tool definitions."""
        body = await request.json()
        tools = [ToolDefinition(**t) for t in body.get("tools", [])]
        result = manager.apply_tools(tools)
        return {"status": "applied", **result}

    @app.post("/config/tools/{name}")
    async def add_tool(name: str, request: Request) -> Any:
        """Add or update a single tool."""
        body = await request.json()
        body["name"] = name
        tool = ToolDefinition(**body)
        result = manager.add_tool(tool)
        return result

    @app.delete("/config/tools/{name}")
    async def remove_tool(name: str) -> Any:
        """Remove a tool."""
        result = manager.remove_tool(name)
        if not result["removed"]:
            return JSONResponse(status_code=404, content={"error": f"Tool not found: {name}"})
        return result

    @app.post("/config/agent-auth")
    async def apply_agent_auth(request: Request) -> Any:
        """Hot-reload per-agent tool authorization."""
        body = await request.json()
        agent_auth.configure(body)
        return {"status": "applied", **agent_auth.get_status()}

    @app.get("/config/agent-auth")
    async def get_agent_auth() -> Any:
        """Get current agent authorization config."""
        return {**agent_auth.get_status(), "agents": agent_auth.list_agents()}

    @app.post("/config/cross-agent")
    async def apply_cross_agent(request: Request) -> Any:
        """Hot-reload cross-agent (A2A) delegation policy."""
        body = await request.json()
        cross_agent.configure(body)
        return {"status": "applied", **cross_agent.get_status()}

    @app.get("/config/cross-agent")
    async def get_cross_agent() -> Any:
        """Get current cross-agent delegation policy."""
        return cross_agent.get_status()

    # ─── Payment (x402) Config Endpoints ───────────────────────────────────

    @app.post("/config/payments")
    async def apply_payments(request: Request) -> Any:
        """Hot-reload payment config (mode + pricing + wallets)."""
        body = await request.json()
        payment_gate.configure(body)
        return {"status": "applied", **payment_gate.status()}

    @app.get("/config/payments")
    async def get_payments() -> Any:
        """Get current payment config + wallet balances."""
        return payment_gate.status()

    @app.post("/config/policy")
    async def apply_policy(request: Request) -> Any:
        """Hot-reload policy."""
        body = await request.json()
        policy = PolicyConfig(**body)
        result = manager.apply_policy(policy)
        return {"status": "applied", **result}

    # ─── Quota Config Endpoints ────────────────────────────────────────────

    @app.post("/config/quota")
    async def apply_quota(request: Request) -> Any:
        """Hot-reload quota configuration."""
        body = await request.json()
        quota_enforcer.configure(body)
        return {"status": "applied", "quota": quota_enforcer.get_status()}

    @app.get("/config/quota")
    async def get_quota() -> Any:
        """Get current quota status."""
        return quota_enforcer.get_status()

    @app.post("/config/quota/reset-spend")
    async def reset_quota_spend() -> Any:
        """Reset the spend counter."""
        quota_enforcer.reset_spend()
        return {"status": "reset", "current_spend": 0}

    # ─── Routing / Budget / Classification Config Endpoints ─────────────────
    # In-memory config the dashboard reads and writes. Persists for the
    # process lifetime (same model as agent_auth / quota above).
    runtime_config: dict[str, Any] = {
        "budget_reset": {"schedule": "manual"},
        "task_classification": {"rules": [], "model_mapping": {}},
        "llm": {"routing_rules": []},
        "routing_overrides": {"overrides": []},
    }

    @app.get("/config/budget-reset")
    async def get_budget_reset() -> Any:
        """Return the current budget-reset schedule."""
        return runtime_config["budget_reset"]

    @app.post("/config/budget-reset")
    async def set_budget_reset(request: Request) -> Any:
        """Set the budget-reset schedule (manual | daily | weekly | monthly)."""
        body = await request.json()
        runtime_config["budget_reset"] = body
        return {"status": "applied", **body}

    @app.get("/config/task-classification")
    async def get_task_classification() -> Any:
        """Return task-classification rules and model mapping."""
        return runtime_config["task_classification"]

    @app.post("/config/task-classification")
    async def set_task_classification(request: Request) -> Any:
        """Set task-classification rules and per-category model mapping."""
        body = await request.json()
        runtime_config["task_classification"] = {
            "rules": body.get("rules", []),
            "model_mapping": body.get("model_mapping", {}),
        }
        return {"status": "applied", **runtime_config["task_classification"]}

    @app.get("/config/llm")
    async def get_llm_config() -> Any:
        """Return LLM routing policy (routing rules)."""
        return runtime_config["llm"]

    @app.post("/config/llm")
    async def set_llm_config(request: Request) -> Any:
        """Set LLM routing policy (routing rules)."""
        body = await request.json()
        runtime_config["llm"] = {"routing_rules": body.get("routing_rules", [])}
        return {"status": "applied", **runtime_config["llm"]}

    @app.get("/config/routing-overrides")
    async def get_routing_overrides() -> Any:
        """Return per-agent routing overrides."""
        return runtime_config["routing_overrides"]

    @app.post("/config/routing-overrides")
    async def set_routing_overrides(request: Request) -> Any:
        """Set per-agent routing overrides."""
        body = await request.json()
        runtime_config["routing_overrides"] = {"overrides": body.get("overrides", [])}
        return {"status": "applied", **runtime_config["routing_overrides"]}

    # ─── MCP Server Config Endpoints ────────────────────────────────────────

    @app.post("/config/mcp-servers")
    async def add_mcp_server(request: Request) -> Any:
        """Add an MCP server (embedded, remote, or stdio)."""
        from ostiari_gateway.mcp.models import MCPServerConfig

        body = await request.json()
        config = MCPServerConfig(**body)
        result = await mcp_manager.add_server(config)
        if result.get("status") == "error":
            return JSONResponse(status_code=502, content=result)
        return result

    @app.delete("/config/mcp-servers/{name}")
    async def remove_mcp_server(name: str) -> Any:
        """Remove an MCP server and all its tools."""
        removed = await mcp_manager.remove_server(name)
        if not removed:
            return JSONResponse(status_code=404, content={"error": f"MCP server not found: {name}"})
        return {"server": name, "status": "removed"}

    @app.get("/config/mcp-servers")
    async def list_mcp_servers() -> Any:
        """List connected MCP servers."""
        return {"servers": mcp_manager.list_servers()}

    @app.post("/config/mcp-servers/{name}/refresh")
    async def refresh_mcp_tools(name: str) -> Any:
        """Re-discover tools from an MCP server."""
        tools = await mcp_manager.refresh_tools(name)
        return {"server": name, "tools_discovered": len(tools), "tools": [t.qualified_name for t in tools]}

    # ─── A2A Agent Config Endpoints ─────────────────────────────────────────

    @app.post("/config/a2a-agents")
    async def add_a2a_agent(request: Request) -> Any:
        """Discover a remote A2A agent and expose its skills as a2a.<name> tools."""
        from ostiari_gateway.a2a.models import A2AAgentConfig

        body = await request.json()
        url = body.get("url", "").rstrip("/")
        if not url:
            return JSONResponse(status_code=400, content={"error": "url is required"})

        # Derive a stable agent key (lowercase, underscores) matching the UI's tool name
        provided_name = body.get("name", "")
        try:
            from ostiari_gateway.a2a.discovery import fetch_agent_card
            card = await fetch_agent_card(url, timeout=body.get("timeout_seconds", 10.0),
                                          auth_token=body.get("auth_token", ""))
            display_name = provided_name or card.name
        except Exception as e:
            return JSONResponse(status_code=502, content={"error": f"Discovery failed: {e}"})

        agent_key = display_name.lower().replace(" ", "_")
        result = await a2a_manager.add_agent(A2AAgentConfig(
            name=agent_key, url=url, auth_token=body.get("auth_token", ""),
        ))
        if result.get("status") == "error":
            return JSONResponse(status_code=502, content=result)
        return {
            "name": display_name,
            "agent_key": agent_key,
            "url": url,
            "skills": [s.id for s in card.skills],
            "tools": [f"a2a.{agent_key}"],
        }

    @app.get("/config/a2a-agents")
    async def list_a2a_agents() -> Any:
        """List connected A2A agents."""
        return {"agents": a2a_manager.list_agents()}

    @app.delete("/config/a2a-agents/{name}")
    async def remove_a2a_agent(name: str) -> Any:
        """Disconnect an A2A agent."""
        removed = await a2a_manager.remove_agent(name)
        if not removed:
            return JSONResponse(status_code=404, content={"error": f"A2A agent not found: {name}"})
        return {"agent": name, "status": "removed"}

    # ─── Health & Info ────────────────────────────────────────────────────

    @app.get("/tools")
    async def list_tools() -> Any:
        """List all registered tools (HTTP + MCP)."""
        http_tools = manager.tool_proxy.list_tools()
        mcp_tools = mcp_manager.list_tools()
        return {"tools": http_tools, "mcp_tools": mcp_tools}

    @app.get("/health")
    async def health() -> Any:
        http_tools = manager.tool_proxy.list_tools()
        mcp_tools = mcp_manager.list_tools()
        return {
            "status": "ok",
            "sidecar_id": manager.config.sidecar_id,
            "tools_registered": len(http_tools) + len(mcp_tools),
            "http_tools": len(http_tools),
            "mcp_tools": len(mcp_tools),
            "mcp_servers": len(mcp_manager.list_servers()),
            "policy_loaded": manager.guard is not None,
            "modules_active": module_registry.get_active(),
            "modules_available": module_registry.get_available(),
            "quota": quota_enforcer.get_status(),
            "agent_auth": agent_auth.get_status(),
        }

    @app.get("/modules")
    async def list_modules() -> Any:
        """List available and active modules."""
        return {
            "active": module_registry.get_active(),
            "available": module_registry.get_available(),
        }

    # Expose core components for tests and introspection.
    app.state.manager = manager
    app.state.a2a_manager = a2a_manager
    app.state.mcp_manager = mcp_manager
    app.state.cross_agent = cross_agent
    app.state.agent_auth = agent_auth

    return app
