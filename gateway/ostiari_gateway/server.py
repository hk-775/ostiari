"""Generic sidecar server — validates and proxies tool calls to remote endpoints."""

import logging
import os as _os
from contextlib import asynccontextmanager
from typing import Any

import httpx as _httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace

from ostiari.exceptions import ActionBlockedError
from ostiari.explain import explain as _explain
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

_NON_SECRET_CRED_FIELDS = {
    "azure_endpoint", "azure_api_version", "bedrock_region",
    "vertex_project", "vertex_location",
}


def _redact_credentials(cfg: dict[str, Any]) -> None:
    """Redact provider API keys from a config dict in place (for GET /config).

    Only secret-bearing fields are redacted; non-sensitive config (region,
    endpoint, api-version, project) is preserved so the response stays useful.
    """
    llm = cfg.get("llm")
    if isinstance(llm, dict):
        creds = llm.get("credentials")
        if isinstance(creds, dict):
            for k, v in list(creds.items()):
                if v and k not in _NON_SECRET_CRED_FIELDS:
                    creds[k] = "***REDACTED***"


def _fail_closed_on_cp_loss() -> bool:
    """Whether a control-plane-unreachable gateway should fail closed.

    Opt-in: OSTIARI_FAIL_CLOSED_ON_CP_LOSS=true, or implied by
    OSTIARI_ENV=production. Default off preserves the demo/standalone flow.
    """
    v = _os.environ.get("OSTIARI_FAIL_CLOSED_ON_CP_LOSS", "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return _os.environ.get("OSTIARI_ENV", "").strip().lower() in ("production", "prod")


def _is_production() -> bool:
    return _os.environ.get("OSTIARI_ENV", "").strip().lower() in ("production", "prod")


def _check_production_posture() -> None:
    """Warn (or refuse) when a production gateway is left fail-open.

    Every gateway control is off-by-default for the demo/standalone flow. In
    production that means anyone reaching the port can flip enforcement mode,
    rewrite tools/policy, or impersonate any agent via the X-Agent-Id header.
    We surface that loudly at startup, and hard-refuse when the operator opts
    into strict mode (OSTIARI_STRICT=1) — mirroring the control plane's
    production JWT-secret guard.
    """
    if not _is_production():
        return

    open_controls: list[str] = []
    if not _os.environ.get("OSTIARI_CONFIG_ADMIN_KEY", "").strip():
        open_controls.append(
            "OSTIARI_CONFIG_ADMIN_KEY unset — /config/* (mode, tools, policy, "
            "quota, payments) is unauthenticated"
        )
    if _os.environ.get("OSTIARI_GATEWAY_AUTH", "off").strip().lower() not in (
        "required", "1", "true", "yes", "on"
    ):
        open_controls.append(
            "OSTIARI_GATEWAY_AUTH not required — the X-Agent-Id header is trusted "
            "with no token, so any caller can impersonate any agent"
        )

    if not open_controls:
        return

    strict = _os.environ.get("OSTIARI_STRICT", "").strip().lower() in (
        "1", "true", "yes", "on"
    )
    banner = "; ".join(open_controls)
    if strict:
        raise RuntimeError(
            f"OSTIARI_ENV=production with OSTIARI_STRICT set, but fail-open "
            f"controls remain: {banner}. Set the listed variables or unset "
            f"OSTIARI_STRICT to start anyway."
        )
    log.warning(
        "PRODUCTION SECURITY WARNING — fail-open controls detected: %s. "
        "Set OSTIARI_STRICT=1 to make this fatal.", banner,
    )


def _config_admin_key() -> str:
    """Shared admin secret required to mutate/read gateway /config/* when set.

    When OSTIARI_CONFIG_ADMIN_KEY is set, /config* calls must present it via the
    X-Config-Admin-Key header (or Bearer). Unset (default) = demo/dev, open —
    matching the existing agent-auth "off by default" posture. This closes the
    assessment finding that anyone reaching the sidecar port could flip mode,
    rewrite tools/policy, or read config.
    """
    return _os.environ.get("OSTIARI_CONFIG_ADMIN_KEY", "")


def _authorize_config(request: Request) -> JSONResponse | None:
    """Gate a /config* request. Returns a 401 response to short-circuit, or None.

    No-op when OSTIARI_CONFIG_ADMIN_KEY is unset (dev/demo). When set, requires a
    matching X-Config-Admin-Key header or Authorization: Bearer <key>.
    """
    expected = _config_admin_key()
    if not expected:
        return None
    presented = request.headers.get("X-Config-Admin-Key", "")
    if not presented:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            presented = auth.removeprefix("Bearer ")
    # constant-time compare
    import hmac as _hmac
    if presented and _hmac.compare_digest(presented, expected):
        return None
    return JSONResponse(status_code=401, content={
        "error": "config administration requires a valid admin key",
    })


def _authenticate_agent(request: Request, agent_id: str) -> JSONResponse | None:
    """Enforce the agent's JWT when gateway auth is required.

    When OSTIARI_GATEWAY_AUTH=required, the caller must present a valid OIDC
    Bearer token whose asserted agent identity matches the X-Agent-Id header.
    Returns a 401/403 JSONResponse to short-circuit on failure, or None to
    proceed. No-op (returns None) when gateway auth is off — preserving the
    current header-trust behavior for the demo.
    """
    from ostiari_gateway import oidc

    validator = oidc.get_validator()
    if validator is None:
        return None  # auth off or unconfigured — trust the header as before

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return JSONResponse(status_code=401, content={
            "error": "authentication required", "detail": "missing Bearer token",
        })
    try:
        claims = validator.validate(header.removeprefix("Bearer "))
    except oidc.OIDCError as exc:
        return JSONResponse(status_code=401, content={
            "error": "invalid token", "detail": str(exc),
        })
    token_agent = oidc.agent_id_from_claims(claims)
    if token_agent != agent_id:
        return JSONResponse(status_code=403, content={
            "error": "identity mismatch",
            "detail": f"token identity '{token_agent}' does not match X-Agent-Id '{agent_id}'",
        })
    return None


def _hitl_enabled() -> bool:
    """Human-in-the-loop enforcement for the intervene tier (off by default)."""
    return _os.environ.get("OSTIARI_HITL", "off").lower() in ("1", "true", "yes", "on")


async def _check_approval(control_plane_url: str, approval_id: str) -> str | None:
    """Return an approval's status from the control plane (or None if unreachable)."""
    if not (control_plane_url and approval_id):
        return None
    try:
        async with _httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{control_plane_url.rstrip('/')}/api/approvals/{approval_id}")
            if r.status_code == 200:
                return r.json().get("status")
    except Exception:  # noqa: BLE001
        pass
    return None


async def _create_approval(control_plane_url: str, payload: dict) -> dict | None:
    """Create a pending approval in the control plane; return it (or None)."""
    if not control_plane_url:
        return None
    try:
        async with _httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(f"{control_plane_url.rstrip('/')}/api/approvals", json=payload)
            if r.status_code == 200:
                return r.json()
    except Exception:  # noqa: BLE001
        pass
    return None


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
    _check_production_posture()

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
            callback_url=initial_config.callback_url,
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

    async def _connect_mcp_servers(mcp_cfgs: list) -> None:
        """Connect a list of MCP server configs (dicts or models), tolerating errors."""
        from ostiari_gateway.mcp.models import MCPServerConfig
        for mcp_cfg in mcp_cfgs or []:
            try:
                cfg = MCPServerConfig(**mcp_cfg) if isinstance(mcp_cfg, dict) else mcp_cfg
                if not mcp_manager.list_servers() or not any(
                    s["name"] == cfg.name for s in mcp_manager.list_servers()
                ):
                    await mcp_manager.add_server(cfg)
            except Exception as e:  # noqa: BLE001 — one bad server shouldn't block the rest
                log.warning("Failed to connect MCP server from config: %s", e)

    async def _connect_a2a_agents(a2a_cfgs: list) -> None:
        """(Re)connect A2A agents from config (each {url, name, auth_token}).

        Discovers the agent card and registers its skills — the same work the
        POST /config/a2a-agents endpoint does — so agents survive a restart.
        """
        from ostiari_gateway.a2a.discovery import fetch_agent_card
        from ostiari_gateway.a2a.models import A2AAgentConfig
        for cfg in a2a_cfgs or []:
            try:
                url = (cfg.get("url") or "").rstrip("/")
                if not url:
                    continue
                card = await fetch_agent_card(url, auth_token=cfg.get("auth_token", ""))
                agent_key = (cfg.get("name") or card.name).lower().replace(" ", "_")
                if not a2a_manager.has_agent(agent_key):
                    await a2a_manager.add_agent(A2AAgentConfig(
                        name=agent_key, url=url, auth_token=cfg.get("auth_token", ""),
                    ))
            except Exception as e:  # noqa: BLE001 — one bad agent shouldn't block the rest
                log.warning("Failed to connect A2A agent from config: %s", e)

    @asynccontextmanager
    async def lifespan(app: Any) -> Any:
        # Initialize MCP servers from a local config file, if any.
        if initial_config and hasattr(initial_config, "mcp_servers"):
            await _connect_mcp_servers(initial_config.mcp_servers)

        # Register with control plane and start heartbeat. The registration
        # bundle carries the MCP servers the control plane has on record, so we
        # (re)connect them here — this is what makes MCP survive a bare gateway
        # restart without re-running a manual register script.
        if lifecycle:
            try:
                data = await lifecycle.register()
                bundle = (data or {}).get("config", {})
                if bundle.get("mcp_servers"):
                    await _connect_mcp_servers(bundle["mcp_servers"])
                if bundle.get("a2a_agents"):
                    await _connect_a2a_agents(bundle["a2a_agents"])
                await lifecycle.start_heartbeat(interval=30)
            except Exception as e:
                # If the control plane is unreachable, the gateway never received
                # its pushed gates (quota, agent-auth, cross-agent) — which all
                # default to allow-all. In production that silently disables
                # governance, so fail CLOSED: enable least-privilege agent auth
                # so unconfigured agents are denied rather than waved through.
                if _fail_closed_on_cp_loss():
                    agent_auth.configure({"enabled": True, "default_grants": [],
                                          "default_models": [], "default_providers": []})
                    log.error(
                        "Control plane registration failed: %s — FAILING CLOSED "
                        "(agent auth deny-by-default until CP reachable)", e)
                else:
                    log.warning(f"Control plane registration failed: {e} — running standalone (fail-open)")

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

    # Optional Redis-backed shared state so rate limit / budget / wallet limits
    # hold across a horizontally-scaled fleet. None (default) = per-process.
    from ostiari_gateway.shared_store import get_shared_store
    shared_store = get_shared_store()
    quota_enforcer.attach_shared_store(shared_store)
    payment_gate.attach_shared_store(shared_store)

    # DoS guards: reject oversized bodies, and per-caller rate limiting
    # (off unless OSTIARI_GATEWAY_RATE_LIMIT_RPM is set).
    from ostiari.http_limits import BodySizeLimitMiddleware, RateLimitMiddleware
    app.add_middleware(RateLimitMiddleware, store=shared_store)
    app.add_middleware(BodySizeLimitMiddleware)

    @app.middleware("http")
    async def _guard_config(request: Request, call_next: Any) -> Any:
        """Require the config-admin key for /config* mutations/reads when set.

        GET /config/mode and GET /tools stay readable; everything under /config
        (including GET /config which can expose config shape) is gated when
        OSTIARI_CONFIG_ADMIN_KEY is configured. No-op otherwise (dev/demo).
        """
        if request.url.path.startswith("/config"):
            denied = _authorize_config(request)
            if denied is not None:
                return denied
        return await call_next(request)

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
        # A bad body is the caller's error. Unguarded, the decode failure escaped
        # as an unhandled exception — a 500 plus a stack trace on the gateway's
        # hottest path, where the agent needs an actionable 400 instead.
        try:
            params: dict[str, Any] = await request.json()
        except Exception:  # noqa: BLE001 — any decode failure is a bad request
            return JSONResponse(status_code=400, content={"error": "Malformed JSON body"})
        if not isinstance(params, dict):
            return JSONResponse(status_code=400,
                                content={"error": "Tool parameters must be a JSON object"})

        agent_id = request.headers.get("X-Agent-Id", "unknown")
        framework = request.headers.get("X-Framework", "unknown")
        session_id = request.headers.get("X-Session-Id", "")
        plan = request.headers.get("X-Plan", "")
        step = request.headers.get("X-Step", "")

        # Authenticate the agent (no-op unless OSTIARI_GATEWAY_AUTH=required):
        # requires a valid OIDC token whose identity matches X-Agent-Id.
        auth_err = _authenticate_agent(request, agent_id)
        if auth_err is not None:
            return auth_err

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
            # Dynamic trust: feed the outcome to cross-agent behavior tracking
            # (intervene/block = risky). Degrading behavior lowers the agent's
            # effective trust for future delegation decisions.
            _raw = getattr(result, "original_tier", result.tier)
            cross_agent.record_outcome(agent_id, risky=_raw in ("intervene", "block"))
        except ActionBlockedError as e:
            record_validate_result(validate_span, "block", e.score, blocked=True)
            cross_agent.record_outcome(agent_id, risky=True)
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

        # Human-in-the-loop: the intervene tier means "ask a human before doing
        # this." When HITL is enabled and this call scored intervene, we do NOT
        # execute. If the caller carries an approved X-Approval-Id we proceed;
        # otherwise we create a pending approval and return 202 (the agent
        # re-submits with the id once a human approves in the dashboard).
        # Use the gateway's RAW tier: the Guard may collapse an intervene to
        # allow/block internally (fail_open / callback), but original_tier still
        # reports it so the sidecar's HITL gate can act on it.
        raw_tier = getattr(result, "original_tier", result.tier)
        if _hitl_enabled() and raw_tier == "intervene":
            # trace_reporter holds the live CP URL (set at startup); manager.config
            # only carries it when a config file was loaded, so prefer the reporter.
            cp_url = trace_reporter._url or (manager.config.control_plane_url if manager.config else "")
            approval_id = request.headers.get("X-Approval-Id", "")
            status_ = await _check_approval(cp_url, approval_id) if approval_id else None
            if status_ == "approved":
                pass  # human said yes — fall through to execute
            elif status_ == "denied":
                await trace_reporter.report(
                    action=action, tier="block", score=result.score, duration_ms=0,
                    agent_id=agent_id, framework=framework, is_mcp=is_mcp_tool,
                    blocked_reason="human denied the intervention",
                    session_id=session_id, plan=plan, step=step, params=params,
                    limit_type="intervention",
                )
                return JSONResponse(status_code=403, content={
                    "blocked": True, "action": action, "reason": "human denied approval",
                    "limit_type": "intervention", "approval_id": approval_id,
                })
            else:
                # No decision yet — create/await one.
                explanation = _explain(result)
                appr = await _create_approval(cp_url, {
                    "agent_id": agent_id, "gateway_id": trace_reporter._sidecar_id or "",
                    "action": action, "params": params, "score": result.score,
                    "reason": explanation.summary or (
                        f"intervene tier (score {result.score}) — human approval required"
                    ),
                })
                await trace_reporter.report(
                    action=action, tier="intervene", score=result.score, duration_ms=0,
                    agent_id=agent_id, framework=framework, is_mcp=is_mcp_tool,
                    blocked_reason="awaiting human approval",
                    session_id=session_id, plan=plan, step=step, params=params,
                    limit_type="intervention",
                )
                return JSONResponse(status_code=202, content={
                    "pending_approval": True, "action": action, "score": result.score,
                    "approval_id": (appr or {}).get("id", ""),
                    "reason": "This action requires human approval. Re-submit with "
                              "X-Approval-Id once approved.",
                    "decision": explanation.to_dict(),
                })

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
            "decision": _explain(result).to_dict(),
        }

    @app.post("/validate")
    async def validate_only(request: Request) -> Any:
        """Validate a tool call without executing it."""
        body: dict[str, Any] = await request.json()
        action = body.get("action", "")
        params = body.get("params", {})
        agent_id = request.headers.get("X-Agent-Id", "unknown")
        framework = request.headers.get("X-Framework", "unknown")

        auth_err = _authenticate_agent(request, agent_id)
        if auth_err is not None:
            return auth_err

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
        """Return current sidecar configuration with credentials redacted.

        The raw config carries resolved provider API keys under llm.credentials;
        never return them in cleartext (assessment finding #2).
        """
        cfg = manager.config.model_dump(mode="json", by_alias=True)
        _redact_credentials(cfg)
        return cfg

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

    @app.post("/config/tools/import-openapi")
    async def import_openapi_tools(request: Request) -> Any:
        """Generate tools from an OpenAPI spec and register them.

        Body: {"source": <url|json|yaml> or "spec": <dict>, "server_url"?,
               "name_prefix"?, "replace"?: bool, "preview"?: bool}
        With preview=true, tools are generated and returned but NOT registered.
        With replace=true, the imported set replaces all existing tools;
        otherwise they are merged in (add_tool per tool).

        Defined before /config/tools/{name} so the literal path wins over the
        parameterized one.
        """
        from ostiari_gateway.openapi_import import OpenAPIError, generate_tools

        body = await request.json()
        source = body.get("spec") if body.get("spec") is not None else body.get("source")
        if source is None:
            return JSONResponse(status_code=400,
                                content={"error": "provide 'source' (url/json/yaml) or 'spec' (object)"})

        # Fetch a URL source; otherwise parse inline. SSRF-guarded + redirects off.
        if isinstance(source, str) and source.strip().lower().startswith(("http://", "https://")):
            from ostiari.net_guard import SSRFError, validate_public_url
            try:
                validate_public_url(source)
            except SSRFError as e:
                return JSONResponse(status_code=400, content={"error": f"blocked URL: {e}"})
            try:
                import httpx as _hx
                async with _hx.AsyncClient(timeout=15.0, follow_redirects=False) as c:
                    r = await c.get(source.strip())
                    r.raise_for_status()
                    source = r.text
            except Exception as e:  # noqa: BLE001
                return JSONResponse(status_code=502,
                                    content={"error": f"could not fetch spec: {e}"})

        try:
            generated = generate_tools(
                source,
                server_url=body.get("server_url"),
                name_prefix=body.get("name_prefix", ""),
            )
        except OpenAPIError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})

        summary = [
            {"name": g.tool.name, "method": g.method, "path": g.path,
             "endpoint": g.tool.endpoint, "summary": g.summary,
             "path_params": g.tool.path_params, "query_params": g.tool.query_params}
            for g in generated
        ]

        if body.get("preview"):
            return {"status": "preview", "count": len(summary), "tools": summary}

        tools = [g.tool for g in generated]
        if body.get("replace"):
            manager.apply_tools(tools)
        else:
            for t in tools:
                manager.add_tool(t)
        return {"status": "imported", "count": len(tools), "tools": summary}

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
