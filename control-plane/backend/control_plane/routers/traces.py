"""Live trace viewer — receives traces from gateways and broadcasts via WebSocket."""

import hmac
import logging
import os
import time
import uuid
from collections import deque
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect, status

log = logging.getLogger("control_plane.traces")

router = APIRouter(tags=["traces"])

# Shared secret that gateways/sidecars must present to push traces. Mirrors the
# OSTIARI_JWT_SECRET pattern in auth/service.py. Machine callers aren't users, so
# trace ingest uses a shared key (X-Ingest-Key header) rather than a user JWT.
#
# Fail-open when unset: the demo and local dev run without it, matching the
# control plane's dev-friendly defaults. Set OSTIARI_INGEST_KEY in any shared or
# production deployment to require authenticated ingest.
_INGEST_KEY_ENV = "OSTIARI_INGEST_KEY"


def _require_ingest_auth(request: Request) -> None:
    """Enforce the ingest shared secret when OSTIARI_INGEST_KEY is configured.

    No-op (with a one-time warning elsewhere) when the key is unset, so existing
    demo/dev setups keep working. When set, a request must present a matching
    ``X-Ingest-Key`` header or it is rejected with 401.
    """
    expected = os.environ.get(_INGEST_KEY_ENV, "").strip()
    if not expected:
        return  # fail-open in dev; see module docstring
    presented = request.headers.get("X-Ingest-Key", "")
    # Constant-time compare to avoid leaking the key via timing.
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Ingest-Key",
        )

# In-memory buffer of recent traces (for new WebSocket clients to catch up)
_recent_traces: deque[dict[str, Any]] = deque(maxlen=200)
_ws_clients: set[WebSocket] = set()

# session_id -> parent_trace_id. The first trace seen for a session becomes the
# parent span; every later trace in that session references it as parent, so a
# prompt's many sub-calls nest under one span. Bounded to avoid unbounded growth.
_session_parents: dict[str, str] = {}
_SESSION_PARENTS_MAX = 2000


def _assign_parent(event: dict[str, Any]) -> None:
    """Stamp parent_trace_id on an event based on its session.

    First trace in a session is the parent (parent_trace_id == its own trace_id);
    subsequent traces in the same session point at that parent. Traces with no
    session are left as standalone roots (parent_trace_id == trace_id).
    """
    tid = event.get("trace_id", "")
    sid = event.get("session_id") or ""
    if not sid:
        event["parent_trace_id"] = tid          # standalone root
        return
    parent = _session_parents.get(sid)
    if parent is None:
        # first call in this session → it is the parent
        if len(_session_parents) >= _SESSION_PARENTS_MAX:
            _session_parents.clear()            # simple bound; drops old sessions
        _session_parents[sid] = tid
        parent = tid
    event["parent_trace_id"] = parent
    event["is_span_root"] = (parent == tid)


def _trace(
    *,
    gateway_id: str,
    action: str,
    tier: str,
    score: int,
    duration_ms: float,
    agent_id: str,
    framework: str,
    age_seconds: float,
    is_mcp: bool = False,
    blocked_reason: str | None = None,
    endpoint: str = "",
    session_id: str = "",
    plan: str = "",
    step: str = "",
    params: dict[str, Any] | None = None,
    model: str = "",
    now: float,
) -> dict[str, Any]:
    """Build one trace event dict in the shape the gateway emits."""
    return {
        "trace_id": uuid.uuid4().hex,
        "sidecar_id": gateway_id,
        "gateway_id": gateway_id,
        "action": action,
        "tier": tier,
        "score": score,
        "duration_ms": duration_ms,
        "agent_id": agent_id,
        "framework": framework,
        "is_mcp": is_mcp,
        "blocked_reason": blocked_reason,
        "endpoint": endpoint,
        "session_id": session_id,
        "plan": plan,
        "step": step,
        "params": params or {},
        "model": model,
        "timestamp": now - age_seconds,
    }


def seed_traces() -> None:
    """Populate the trace buffer with demo data so Live Traces isn't empty.

    Only seeds when the buffer is empty (real gateway traffic takes precedence).
    Covers all seeded gateways/agents with a mix of allow/intervene/block/error
    tiers, HTTP and MCP tools, and one multi-step planner session.
    """
    if _recent_traces:
        return

    now = time.time()
    session = "sess-planner-7c3f9a2b"

    events = [
        # --- research-agent (OpenAI) on CRM gateway ---
        _trace(gateway_id="crm-agent", action="web_search", tier="allow", score=10,
               duration_ms=142.3, agent_id="research-agent", framework="openai",
               endpoint="http://search-service:8080/query", model="gpt-4o",
               params={"query": "enterprise LLM gateway benchmarks"}, age_seconds=312, now=now),
        _trace(gateway_id="crm-agent", action="file_write", tier="intervene", score=48,
               duration_ms=23.1, agent_id="research-agent", framework="openai",
               endpoint="http://fs-service:8080/write", model="gpt-4o",
               params={"path": "/reports/summary.md", "bytes": 4096}, age_seconds=298, now=now),

        # --- ops-agent (Strands) on Ops gateway — a blocked destructive call ---
        _trace(gateway_id="ops-agent", action="db_query", tier="allow", score=15,
               duration_ms=58.7, agent_id="ops-agent", framework="strands",
               endpoint="http://db-service:8080/query", model="claude-sonnet-4-6",
               params={"sql": "SELECT count(*) FROM orders"}, age_seconds=270, now=now),
        _trace(gateway_id="ops-agent", action="db_delete", tier="block", score=95,
               duration_ms=2.4, agent_id="ops-agent", framework="strands",
               blocked_reason="Matched blocklist pattern '*.delete'", model="claude-sonnet-4-6",
               params={"table": "users", "where": "1=1"}, age_seconds=255, now=now),
        _trace(gateway_id="ops-agent", action="send_email", tier="allow", score=25,
               duration_ms=189.2, agent_id="ops-agent", framework="strands",
               endpoint="http://email-service:8080/send", model="claude-sonnet-4-6",
               params={"to": "oncall@example.com", "subject": "Nightly report"}, age_seconds=240, now=now),

        # --- claude-agent (Anthropic) — MCP tool call ---
        _trace(gateway_id="crm-agent", action="github.create_issue", tier="allow", score=20,
               duration_ms=331.5, agent_id="claude-agent", framework="anthropic", is_mcp=True,
               model="claude-sonnet-4-6",
               params={"repo": "acme/api", "title": "Flaky integration test"}, age_seconds=210, now=now),

        # --- bedrock-agent — high risk, intervened ---
        _trace(gateway_id="crm-agent", action="execute_code", tier="intervene", score=62,
               duration_ms=402.8, agent_id="bedrock-agent", framework="bedrock",
               endpoint="http://sandbox:8080/exec", model="bedrock/anthropic.claude-3-5-sonnet",
               params={"lang": "python", "lines": 34}, age_seconds=180, now=now),

        # --- langgraph-agent — MCP + error (unreachable endpoint) ---
        _trace(gateway_id="crm-agent", action="drawio.create_diagram", tier="error", score=18,
               duration_ms=104.0, agent_id="langgraph-agent", framework="langgraph", is_mcp=True,
               endpoint="http://drawio-mcp:3000", model="gpt-4o",
               blocked_reason="Cannot reach tool endpoint (connection refused)",
               params={"name": "system-architecture"}, age_seconds=150, now=now),

        # --- analytics-agent on Analytics gateway ---
        _trace(gateway_id="analytics-agent", action="db_query", tier="allow", score=12,
               duration_ms=76.4, agent_id="crewai-agent", framework="crewai",
               endpoint="http://warehouse:8080/query", model="gpt-4o",
               params={"sql": "SELECT day, revenue FROM daily_rollup LIMIT 30"}, age_seconds=120, now=now),

        # --- planner-bot multi-step session (grouped in the UI) ---
        _trace(gateway_id="crm-agent", action="web_search", tier="allow", score=10,
               duration_ms=131.7, agent_id="planner-bot", framework="gateway-invoke",
               endpoint="http://search-service:8080/query", model="claude-sonnet-4-6",
               session_id=session, plan="Research competitors, draft brief, notify team",
               step="1. Gather market data", params={"query": "AI agent governance vendors"},
               age_seconds=90, now=now),
        _trace(gateway_id="crm-agent", action="file_write", tier="allow", score=22,
               duration_ms=41.9, agent_id="planner-bot", framework="gateway-invoke",
               endpoint="http://fs-service:8080/write", model="claude-sonnet-4-6",
               session_id=session, plan="Research competitors, draft brief, notify team",
               step="2. Draft brief", params={"path": "/briefs/competitors.md"},
               age_seconds=78, now=now),
        _trace(gateway_id="crm-agent", action="github.search_code", tier="allow", score=15,
               duration_ms=205.3, agent_id="planner-bot", framework="gateway-invoke", is_mcp=True,
               model="claude-sonnet-4-6",
               session_id=session, plan="Research competitors, draft brief, notify team",
               step="3. Reference prior work", params={"q": "policy engine", "org": "acme"},
               age_seconds=64, now=now),
        _trace(gateway_id="crm-agent", action="send_email", tier="intervene", score=44,
               duration_ms=167.0, agent_id="planner-bot", framework="gateway-invoke",
               endpoint="http://email-service:8080/send", model="claude-sonnet-4-6",
               session_id=session, plan="Research competitors, draft brief, notify team",
               step="4. Notify team", params={"to": "team@example.com", "subject": "Competitor brief ready"},
               age_seconds=50, now=now),

        # --- smart-router-bot — routed model ---
        _trace(gateway_id="crm-agent", action="db_query", tier="allow", score=14,
               duration_ms=63.2, agent_id="smart-router-bot", framework="gateway-invoke",
               endpoint="http://db-service:8080/query", model="claude-haiku-4-5 (routed)",
               params={"sql": "SELECT * FROM accounts LIMIT 10"}, age_seconds=30, now=now),
        _trace(gateway_id="devops-agent", action="deploy", tier="intervene", score=55,
               duration_ms=88.6, agent_id="ops-agent", framework="strands",
               endpoint="http://localhost:9200/a2a", model="claude-sonnet-4-6",
               params={"service": "auth-service", "environment": "production"}, age_seconds=12, now=now),
    ]

    # --- Volume: a broader spread of activity so Live Traces / Metering /
    #     Compliance / Trust look like a real fleet, not a handful of rows. ---
    import random as _rnd
    _rnd.seed(42)  # deterministic demo
    agents_fw = [
        ("research-agent", "openai"), ("coder-agent", "anthropic"),
        ("db-agent", "langgraph"), ("payments-agent", "crewai"),
        ("ops-agent", "strands"), ("analytics-agent", "autogen"),
        ("support-agent", "openai"), ("planner-agent", "langgraph"),
    ]
    safe_tools = ["web_search", "db_query", "file_read", "github.search_code", "calendar.create"]
    risky_tools = ["db_delete", "file_write", "github.create_pr", "send_email", "slack.post"]
    for _i in range(90):
        agent, fw = _rnd.choice(agents_fw)
        risky = _rnd.random() < 0.4
        tool = _rnd.choice(risky_tools if risky else safe_tools)
        if risky:
            tier = _rnd.choices(["allow", "intervene", "block"], weights=[45, 30, 25])[0]
        else:
            tier = _rnd.choices(["allow", "intervene", "block"], weights=[86, 11, 3])[0]
        score = {"allow": _rnd.randint(2, 28), "intervene": _rnd.randint(38, 68),
                 "block": _rnd.randint(74, 100)}[tier]
        events.append(_trace(
            gateway_id=_rnd.choice(["crm-agent", "ops-agent", "devops-agent", "analytics-agent"]),
            action=tool, tier=tier, score=score, duration_ms=round(_rnd.uniform(15, 420), 1),
            agent_id=agent, framework=fw,
            blocked_reason=(f"policy: {tool} restricted" if tier == "block" else None),
            model=_rnd.choice(["claude-haiku", "gpt-4o", "claude-sonnet", "gpt-4o-mini"]),
            age_seconds=_rnd.uniform(5, 3600), now=now,
        ))

    for e in events:
        _recent_traces.append(e)

    # --- Blocked cross-agent delegations (Protocol Governance + Shadow feeds).
    #     These carry limit_type/would_block/delegation_chain, which _trace()
    #     doesn't model, so append them directly. ---
    delegations = [
        ("research-agent", "payments-agent", "research-agent -> payments-agent not permitted", False, 3),
        ("coder-agent", "payments-agent", "coder-agent -> payments-agent not permitted", True, 2),
        ("support-agent", "db-agent", "callee db-agent trust 55 below minimum 60", True, 2),
        ("analytics-agent", "payments-agent", "analytics-agent -> payments-agent not permitted", False, 2),
        ("planner-agent", "ops-agent", "delegation chain depth 5 exceeds max 4", True, 1),
    ]
    for caller, callee, reason, shadow, count in delegations:
        for _ in range(count):
            _recent_traces.append({
                "trace_id": uuid.uuid4().hex,
                "sidecar_id": "crm-agent", "gateway_id": "crm-agent",
                "action": f"a2a.{callee}", "tier": "block", "score": 0,
                "agent_id": caller, "framework": "gateway-invoke", "is_mcp": False,
                "blocked_reason": reason, "limit_type": "cross_agent_delegation",
                "would_block": True, "shadow": shadow, "delegation_chain": [caller],
                "endpoint": f"a2a://{callee}", "params": {}, "model": "",
                "timestamp": now - _rnd.uniform(10, 1800),
            })

    log.info("Seeded %d demo traces", len(_recent_traces))


@router.post("/api/traces/ingest")
async def ingest_trace(request: Request) -> Any:
    """Receive a trace event from a gateway and broadcast to WebSocket clients.

    Idempotent on ``trace_id``: a re-POSTed trace (gateway retry) updates the
    existing entry instead of creating a duplicate. Legacy events without a
    trace_id get one synthesized so downstream consumers always have a stable
    handle.
    """
    _require_ingest_auth(request)
    event = await request.json()
    if not event.get("trace_id"):
        event["trace_id"] = uuid.uuid4().hex

    # Assign the session parent span (parent_trace_id) so a prompt's sub-calls nest.
    _assign_parent(event)

    # Dedup: if we've already seen this trace_id, replace it in place (retry /
    # update) rather than appending a second copy.
    tid = event["trace_id"]
    duplicate = False
    for i, existing in enumerate(_recent_traces):
        if existing.get("trace_id") == tid:
            _recent_traces[i] = event
            duplicate = True
            break
    if not duplicate:
        _recent_traces.append(event)

    # Broadcast to all connected WebSocket clients (clients dedup on trace_id).
    disconnected = set()
    for ws in _ws_clients:
        try:
            await ws.send_json(event)
        except Exception:
            disconnected.add(ws)

    _ws_clients.difference_update(disconnected)
    return {"status": "ok", "trace_id": tid, "duplicate": duplicate, "clients": len(_ws_clients)}


@router.get("/api/traces/recent")
async def get_recent_traces(limit: int = 50) -> Any:
    """Get recent traces (for initial page load before WebSocket connects)."""
    traces = list(_recent_traces)[-limit:]
    return {"traces": traces, "total": len(traces)}


@router.get("/api/traces/spans")
async def get_spans(limit: int = 200) -> Any:
    """Return traces grouped into parent spans (one prompt = one span tree).

    Groups by parent_trace_id: each span carries the child traces plus a rollup
    (call count, total tokens, total duration, worst tier). Standalone traces
    (no session) appear as single-child spans. This is what powers a nested
    span view — a prompt's many sub-calls under one parent.
    """
    traces = list(_recent_traces)[-limit:]
    spans: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    tier_rank = {"allow": 0, "intervene": 1, "error": 2, "block": 3}

    for t in traces:
        parent = t.get("parent_trace_id") or t.get("trace_id")
        if parent not in spans:
            spans[parent] = {
                "span_id": parent, "session_id": t.get("session_id", ""),
                "agent_id": t.get("agent_id", ""), "children": [],
                "call_count": 0, "total_input_tokens": 0, "total_output_tokens": 0,
                "total_duration_ms": 0.0, "worst_tier": "allow", "start_ts": None, "end_ts": None,
            }
            order.append(parent)
        s = spans[parent]
        s["children"].append(t)
        s["call_count"] += 1
        p = t.get("params") or {}
        s["total_input_tokens"] += int(p.get("input_tokens", 0) or 0)
        s["total_output_tokens"] += int(p.get("output_tokens", 0) or 0)
        s["total_duration_ms"] += float(t.get("duration_ms", 0) or 0)
        if tier_rank.get(t.get("tier", "allow"), 0) > tier_rank.get(s["worst_tier"], 0):
            s["worst_tier"] = t.get("tier", "allow")
        ts = t.get("timestamp")
        if ts is not None:
            s["start_ts"] = ts if s["start_ts"] is None else min(s["start_ts"], ts)
            s["end_ts"] = ts if s["end_ts"] is None else max(s["end_ts"], ts)

    return {"spans": [spans[p] for p in order], "total": len(order)}


@router.get("/api/traces/shadow-report")
async def shadow_report() -> Any:
    """Summarize shadow-mode activity: what enforce mode WOULD have blocked.

    Aggregates the trace buffer's shadow events into a report suitable for the
    'try before you enforce' workflow — total shadow calls, how many would have
    been blocked, and the offending actions grouped by reason.
    """
    shadow_traces = [t for t in _recent_traces if t.get("shadow")]
    would_block = [t for t in shadow_traces if t.get("would_block")]

    by_action: dict[str, dict[str, Any]] = {}
    for t in would_block:
        action = t.get("action", "unknown")
        entry = by_action.setdefault(action, {
            "action": action, "count": 0, "max_score": 0, "reasons": set(),
        })
        entry["count"] += 1
        entry["max_score"] = max(entry["max_score"], t.get("score") or 0)
        if t.get("blocked_reason"):
            entry["reasons"].add(t["blocked_reason"])

    # Serialize reason sets to sorted lists
    offenders = sorted(
        (
            {**e, "reasons": sorted(e["reasons"])}
            for e in by_action.values()
        ),
        key=lambda e: e["count"],
        reverse=True,
    )

    total_shadow = len(shadow_traces)
    return {
        "total_shadow_calls": total_shadow,
        "would_block_count": len(would_block),
        "would_allow_count": total_shadow - len(would_block),
        "block_rate": round(len(would_block) / total_shadow, 4) if total_shadow else 0.0,
        "offending_actions": offenders,
    }


@router.get("/api/traces/delegation-report")
async def delegation_report() -> Any:
    """Summarize blocked (and would-be-blocked) cross-agent delegations.

    Surfaces the A2A edges that governance stopped: which caller tried to
    delegate to which callee, how often, why, and the delegation chain — for
    the Protocol Governance "would-block" feed.
    """
    blocked = [
        t for t in _recent_traces
        if t.get("limit_type") == "cross_agent_delegation" and t.get("would_block")
    ]

    by_edge: dict[str, dict[str, Any]] = {}
    for t in blocked:
        chain = t.get("delegation_chain") or []
        caller = chain[-1] if chain else t.get("agent_id", "unknown")
        # action is "a2a.<callee>"
        action = t.get("action", "")
        callee = action[len("a2a."):] if action.startswith("a2a.") else action
        key = f"{caller}->{callee}"
        entry = by_edge.setdefault(key, {
            "caller": caller, "callee": callee, "count": 0,
            "reasons": set(), "example_chain": chain, "shadow": bool(t.get("shadow")),
        })
        entry["count"] += 1
        if t.get("blocked_reason"):
            entry["reasons"].add(t["blocked_reason"])

    edges = sorted(
        ({**e, "reasons": sorted(e["reasons"])} for e in by_edge.values()),
        key=lambda e: e["count"],
        reverse=True,
    )
    return {
        "blocked_delegation_count": len(blocked),
        "distinct_edges": len(edges),
        "edges": edges,
    }


@router.websocket("/ws/traces")
async def websocket_traces(websocket: WebSocket) -> None:
    """WebSocket endpoint for live trace streaming.

    Clients connect here to receive real-time tool call events from all gateways.
    On connect, sends recent history so the UI isn't empty.
    """
    await websocket.accept()
    _ws_clients.add(websocket)
    log.info("Trace viewer connected (total: %d)", len(_ws_clients))

    # Send recent history on connect
    try:
        for trace in list(_recent_traces)[-50:]:
            await websocket.send_json(trace)
    except Exception:
        _ws_clients.discard(websocket)
        return

    # Keep alive until disconnect
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(websocket)
        log.info("Trace viewer disconnected (total: %d)", len(_ws_clients))
