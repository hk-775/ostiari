"""Live trace viewer — receives traces from gateways and broadcasts via WebSocket."""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from datetime import datetime, timezone
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
    status,
)
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org, principal_from_token
from control_plane.auth.workload import authorize_reported_gateway
from control_plane.database import get_db
from control_plane.env import control_plane_replicas, is_production
from control_plane.models.database import DEFAULT_ORG, TraceRecord
from control_plane.redis_client import get_redis

log = logging.getLogger("control_plane.traces")

router = APIRouter(tags=["traces"])

# All trace state is keyed by org (tenant) so one org's traces are never
# stored in, listed from, or broadcast to another org's buffer/sockets.
# Single-org dev/demo uses only the "default" org, so behavior is unchanged.
_TRACE_CACHE_SIZE = 200

# org -> buffer of recent traces (for new WebSocket clients to catch up)
_recent_traces: dict[str, deque[dict[str, Any]]] = defaultdict(
    lambda: deque(maxlen=_TRACE_CACHE_SIZE)
)
# org -> set of connected WebSocket clients (fan-out is per-org — no cross-tenant leak)
_ws_clients: dict[str, set[WebSocket]] = defaultdict(set)

# org -> (session_id -> parent_trace_id). The first trace seen for a session
# becomes the parent span; later traces in that session reference it, so a
# prompt's sub-calls nest under one span. LRU-bounded per org: touching a
# session moves it to the end; at the cap only the single least-recently-used
# entry is evicted (not a full clear, which would fragment an active session).
_session_parents: dict[str, OrderedDict[str, str]] = defaultdict(OrderedDict)
_SESSION_PARENTS_MAX = 2000
_SAFE_PARAM_FIELDS = frozenset({"input_tokens", "output_tokens", "total_tokens"})
_TRACE_CHANNEL = "ostiari:control-plane:traces"
_INSTANCE_ID = uuid.uuid4().hex
_trace_bus_task: asyncio.Task[None] | None = None
_trace_bus_errors: dict[str, str] = {
    "parent": "",
    "publish": "",
    "subscribe": "",
}


def _capture_raw_params() -> bool:
    raw = os.environ.get("OSTIARI_TRACE_CAPTURE_PARAMS", "").strip().lower()
    if is_production():
        return False
    if not raw:
        return True
    return raw in {"1", "true", "yes", "on"}


def _sanitize_event(event: dict[str, Any]) -> dict[str, Any]:
    """Remove raw tool arguments before persistence, fan-out, and export."""
    sanitized = dict(event)
    params = sanitized.get("params")
    if isinstance(params, dict) and not _capture_raw_params():
        sanitized["params"] = {
            str(key): (
                value
                if key in _SAFE_PARAM_FIELDS
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                else "[REDACTED]"
            )
            for key, value in params.items()
        }
    elif not isinstance(params, dict):
        sanitized["params"] = {}
    return sanitized


async def _persist_trace(db: AsyncSession, org: str, event: dict[str, Any]) -> None:
    """Tenant-scoped idempotent trace upsert."""
    values = {
        "org_id": org,
        "trace_id": event["trace_id"],
        "gateway_id": event.get("gateway_id", ""),
        "event": event,
        "updated_at": datetime.now(timezone.utc),
    }
    dialect = db.bind.dialect.name if db.bind is not None else ""
    insert_fn = postgresql_insert if dialect == "postgresql" else sqlite_insert
    statement = insert_fn(TraceRecord).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=["org_id", "trace_id"],
        set_={
            "gateway_id": statement.excluded.gateway_id,
            "event": statement.excluded.event,
            "updated_at": statement.excluded.updated_at,
        },
    )
    await db.execute(statement)


async def _recent_from_db(
    db: AsyncSession,
    org: str,
    *,
    limit: int = _TRACE_CACHE_SIZE,
) -> list[dict[str, Any]]:
    result = await db.execute(
        select(TraceRecord)
        .where(TraceRecord.org_id == org)
        .order_by(TraceRecord.updated_at.desc())
        .limit(limit)
    )
    return [record.event for record in reversed(result.scalars().all())]


async def load_recent_trace_cache(db: AsyncSession) -> None:
    """Rebuild each tenant's bounded live cache from durable trace rows."""
    _recent_traces.clear()
    org_result = await db.execute(select(TraceRecord.org_id).distinct())
    for org in org_result.scalars():
        _recent_traces[org].extend(
            await _recent_from_db(db, org, limit=_TRACE_CACHE_SIZE)
        )


def recent_traces_for(org: str = DEFAULT_ORG) -> list[dict[str, Any]]:
    """The recent-trace buffer for one org, as a list. The canonical accessor
    for other routers (roi/compliance/trust/discovery) that analyze traces —
    they must NOT iterate the org-keyed dict directly."""
    return list(_recent_traces[org])


def _assign_parent(event: dict[str, Any], org: str = DEFAULT_ORG) -> None:
    """Stamp parent_trace_id on an event based on its session (within its org).

    First trace in a session is the parent (parent_trace_id == its own trace_id);
    subsequent traces in the same session point at that parent. Traces with no
    session are left as standalone roots (parent_trace_id == trace_id).
    """
    tid = event.get("trace_id", "")
    sid = event.get("session_id") or ""
    if not sid:
        event["parent_trace_id"] = tid          # standalone root
        return
    parents = _session_parents[org]
    parent = parents.get(sid)
    if parent is None:
        # first call in this session → it is the parent
        if len(parents) >= _SESSION_PARENTS_MAX:
            parents.popitem(last=False)   # evict only the LRU session
        parents[sid] = tid
        parent = tid
    else:
        # touch: mark this session most-recently-used so an active session isn't
        # evicted out from under its own later calls.
        parents.move_to_end(sid)
    event["parent_trace_id"] = parent
    event["is_span_root"] = (parent == tid)


async def _assign_parent_distributed(
    event: dict[str, Any],
    org: str = DEFAULT_ORG,
) -> None:
    """Assign one stable session root across every control-plane replica."""
    tid = str(event.get("trace_id") or "")
    sid = str(event.get("session_id") or "")
    if not sid:
        _assign_parent(event, org)
        return

    redis = await get_redis()
    if redis is None:
        if is_production() or control_plane_replicas() > 1:
            _trace_bus_errors["parent"] = "Redis unavailable"
            raise HTTPException(
                status_code=503,
                detail="Trace coordination is unavailable",
            )
        _assign_parent(event, org)
        return

    digest = hashlib.sha256(f"{org}\0{sid}".encode()).hexdigest()
    key = f"ostiari:trace-session-parent:{digest}"
    try:
        created = await redis.set(key, tid, nx=True, ex=86_400)
        parent = tid if created else await redis.get(key)
        parent = str(parent or tid)
        event["parent_trace_id"] = parent
        event["is_span_root"] = parent == tid
        _trace_bus_errors["parent"] = ""
    except Exception as exc:  # noqa: BLE001 - SQL persistence remains available
        _trace_bus_errors["parent"] = str(exc)
        if is_production() or control_plane_replicas() > 1:
            raise HTTPException(
                status_code=503,
                detail="Trace coordination is unavailable",
            ) from exc
        _assign_parent(event, org)


async def _broadcast_local(org: str, event: dict[str, Any]) -> int:
    """Update this replica's cache and connected viewers."""
    buf = _recent_traces[org]
    tid = event.get("trace_id")
    replaced = False
    for index, existing in enumerate(buf):
        if existing.get("trace_id") == tid:
            buf[index] = event
            replaced = True
            break
    if not replaced:
        buf.append(event)

    clients = _ws_clients[org]
    disconnected = set()
    for websocket in clients:
        try:
            await websocket.send_json(event)
        except Exception:  # noqa: BLE001 - one viewer must not block fan-out
            disconnected.add(websocket)
    clients.difference_update(disconnected)
    return len(clients)


async def _publish_trace(org: str, event: dict[str, Any]) -> int:
    """Broadcast locally and publish to sibling replicas when Redis is present."""
    clients = await _broadcast_local(org, event)
    redis = await get_redis()
    if redis is None:
        return clients
    try:
        await redis.publish(
            _TRACE_CHANNEL,
            json.dumps(
                {"source": _INSTANCE_ID, "org": org, "event": event},
                separators=(",", ":"),
            ),
        )
        _trace_bus_errors["publish"] = ""
    except Exception as exc:  # noqa: BLE001 - event is already durable in SQL
        _trace_bus_errors["publish"] = str(exc)
        log.exception("Trace fan-out publication failed")
    return clients


async def _handle_trace_message(raw: str) -> None:
    payload = json.loads(raw)
    if payload.get("source") == _INSTANCE_ID:
        return
    org = str(payload.get("org") or DEFAULT_ORG)
    event = payload.get("event")
    if isinstance(event, dict):
        await _broadcast_local(org, event)


async def _trace_bus_loop() -> None:
    while True:
        try:
            redis = await get_redis()
            if redis is None:
                await asyncio.sleep(1)
                continue
            async with redis.pubsub() as pubsub:
                await pubsub.subscribe(_TRACE_CHANNEL)
                _trace_bus_errors["subscribe"] = ""
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    await _handle_trace_message(message["data"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reconnect until Redis recovers
            _trace_bus_errors["subscribe"] = str(exc)
            log.exception("Trace fan-out subscriber failed")
            await asyncio.sleep(1)


def start_trace_bus() -> None:
    global _trace_bus_task
    if _trace_bus_task is None or _trace_bus_task.done():
        _trace_bus_task = asyncio.create_task(_trace_bus_loop())


async def stop_trace_bus() -> None:
    global _trace_bus_task
    if _trace_bus_task is None:
        return
    _trace_bus_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _trace_bus_task
    _trace_bus_task = None


def trace_bus_error() -> str:
    return "; ".join(
        f"{operation}: {error}"
        for operation, error in _trace_bus_errors.items()
        if error
    )


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
        "demo_seed": True,
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

    Seeds once per process even when durable live traffic already exists. The
    marker lets startup distinguish restored demo history from real traces.
    Covers all seeded gateways/agents with a mix of allow/intervene/block/error
    tiers, HTTP and MCP tools, and one multi-step planner session.
    """
    buf = _recent_traces[DEFAULT_ORG]
    if any(event.get("demo_seed") for event in buf):
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
        buf.append(e)

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
            buf.append({
                "trace_id": uuid.uuid4().hex,
                "demo_seed": True,
                "sidecar_id": "crm-agent", "gateway_id": "crm-agent",
                "action": f"a2a.{callee}", "tier": "block", "score": 0,
                "agent_id": caller, "framework": "gateway-invoke", "is_mcp": False,
                "blocked_reason": reason, "limit_type": "cross_agent_delegation",
                "would_block": True, "shadow": shadow, "delegation_chain": [caller],
                "endpoint": f"a2a://{callee}", "params": {}, "model": "",
                "timestamp": now - _rnd.uniform(10, 1800),
            })

    log.info("Seeded %d demo traces", len(buf))


async def persist_demo_traces(db: AsyncSession) -> None:
    """Persist seeded history so REST and WebSocket clients see the same data."""
    seed_traces()
    demo_events = [
        _sanitize_event(event)
        for event in _recent_traces[DEFAULT_ORG]
        if event.get("demo_seed")
    ]
    for event in demo_events:
        await _persist_trace(db, DEFAULT_ORG, event)
    await db.commit()
    log.info("Persisted %d demo traces", len(demo_events))


@router.post("/api/traces/ingest")
async def ingest_trace(request: Request, db: AsyncSession = Depends(get_db)) -> Any:
    """Receive a trace event from a gateway and broadcast to WebSocket clients.

    Idempotent on ``trace_id``: a re-POSTed trace (gateway retry) updates the
    existing entry instead of creating a duplicate. Legacy events without a
    trace_id get one synthesized so downstream consumers always have a stable
    handle.
    """
    event = await request.json()
    if not event.get("trace_id"):
        event["trace_id"] = uuid.uuid4().hex

    gateway_id = event.get("sidecar_id") or event.get("gateway_id") or ""
    gateway = await authorize_reported_gateway(request, db, gateway_id)

    # The org this event belongs to, derived from the reporting gateway — never
    # from the payload. All storage + fan-out below is confined to this org.
    org = gateway.org_id if gateway is not None else DEFAULT_ORG
    # An ingest caller cannot choose its own tenant: drop any org_id it sent so
    # the forged value can't survive into the stored event and mislead readers.
    event["org_id"] = org

    # The reporter sends `sidecar_id`; consumers (LiveTraces.tsx, delegation
    # reports) read `gateway_id`. Without this the column showed empty for every
    # live trace while the demo-seeded ones — which set both — looked fine.
    if not event.get("gateway_id") and event.get("sidecar_id"):
        event["gateway_id"] = event["sidecar_id"]

    event = _sanitize_event(event)

    # Assign the session parent span (parent_trace_id) so a prompt's sub-calls nest.
    await _assign_parent_distributed(event, org)

    tid = event["trace_id"]
    duplicate = any(
        existing.get("trace_id") == tid
        for existing in _recent_traces[org]
    )

    await _persist_trace(db, org, event)
    await db.commit()

    # Export the governance span over OTLP (no-op unless OTEL endpoint configured).
    # New events only — a duplicate (retry) is the same span.
    if not duplicate:
        try:
            from control_plane.services.otlp_exporter import exporter as _otlp
            _otlp.export_event(event)
        except Exception:  # noqa: BLE001 — export must never break ingest
            pass

    clients = await _publish_trace(org, event)
    return {
        "status": "ok",
        "trace_id": tid,
        "duplicate": duplicate,
        "clients": clients,
    }


@router.get("/api/traces/recent")
async def get_recent_traces(
    limit: int = 50,
    org: str = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Get recent traces (for initial page load before WebSocket connects)."""
    traces = await _recent_from_db(db, org, limit=min(max(limit, 1), _TRACE_CACHE_SIZE))
    if not traces:
        traces = list(_recent_traces[org])[-limit:]
    return {"traces": traces, "total": len(traces)}


@router.get("/api/traces/spans")
async def get_spans(limit: int = 200, org: str = Depends(get_current_org)) -> Any:
    """Return traces grouped into parent spans (one prompt = one span tree).

    Groups by parent_trace_id: each span carries the child traces plus a rollup
    (call count, total tokens, total duration, worst tier). Standalone traces
    (no session) appear as single-child spans. This is what powers a nested
    span view — a prompt's many sub-calls under one parent.
    """
    traces = list(_recent_traces[org])[-limit:]
    spans: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    tier_rank = {"allow": 0, "intervene": 1, "error": 2, "block": 3}

    for t in traces:
        parent = str(t.get("parent_trace_id") or t.get("trace_id") or "")
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
async def shadow_report(org: str = Depends(get_current_org)) -> Any:
    """Summarize shadow-mode activity: what enforce mode WOULD have blocked.

    Aggregates the trace buffer's shadow events into a report suitable for the
    'try before you enforce' workflow — total shadow calls, how many would have
    been blocked, and the offending actions grouped by reason.
    """
    shadow_traces = [t for t in _recent_traces[org] if t.get("shadow")]
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
async def delegation_report(org: str = Depends(get_current_org)) -> Any:
    """Summarize blocked (and would-be-blocked) cross-agent delegations.

    Surfaces the A2A edges that governance stopped: which caller tried to
    delegate to which callee, how often, why, and the delegation chain — for
    the Protocol Governance "would-block" feed.
    """
    blocked = [
        t for t in _recent_traces[org]
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


def _ws_org(websocket: WebSocket) -> str:
    """Resolve the org a trace-viewer socket belongs to.

    When auth is enforced, derive it from the token (query `?token=` or the
    Authorization header) so a viewer can only subscribe to its own org.
    Otherwise (demo/single-org) accept an explicit `?org=` and default to
    "default". A socket only ever receives events for the org resolved here.
    """
    require_auth = os.environ.get("OSTIARI_REQUIRE_AUTH", "").lower() in ("1", "true", "yes", "on")
    if require_auth:
        token = websocket.query_params.get("token", "")
        if not token:
            auth = websocket.headers.get("authorization", "")
            if auth.startswith("Bearer "):
                token = auth.removeprefix("Bearer ")
        if not token:
            raise WebSocketException(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="Authentication required",
            )
        try:
            principal = principal_from_token(token)
        except Exception:  # noqa: BLE001 — authentication must fail closed
            raise WebSocketException(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="Invalid or expired token",
            ) from None
        return principal.tenant_id or DEFAULT_ORG
    return websocket.query_params.get("org", DEFAULT_ORG) or DEFAULT_ORG


@router.websocket("/ws/traces")
async def websocket_traces(websocket: WebSocket) -> None:
    """WebSocket endpoint for live trace streaming.

    Clients connect here to receive real-time tool call events for THEIR org.
    On connect, sends that org's recent history so the UI isn't empty.
    """
    org = _ws_org(websocket)
    await websocket.accept()
    clients = _ws_clients[org]
    clients.add(websocket)
    log.info("Trace viewer connected to org=%s (total: %d)", org, len(clients))

    # Send recent history (this org only) on connect
    try:
        for trace in list(_recent_traces[org])[-50:]:
            await websocket.send_json(trace)
    except Exception:
        clients.discard(websocket)
        return

    # Keep alive until disconnect
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(websocket)
        log.info("Trace viewer disconnected from org=%s (total: %d)", org, len(clients))
