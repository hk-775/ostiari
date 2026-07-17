"""Live trace viewer — receives traces from gateways and broadcasts via WebSocket."""

import logging
import time
from collections import deque
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

log = logging.getLogger("control_plane.traces")

router = APIRouter(tags=["traces"])

# In-memory buffer of recent traces (for new WebSocket clients to catch up)
_recent_traces: deque[dict[str, Any]] = deque(maxlen=200)
_ws_clients: set[WebSocket] = set()


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

    for e in events:
        _recent_traces.append(e)

    log.info("Seeded %d demo traces", len(events))


@router.post("/api/traces/ingest")
async def ingest_trace(request: Request) -> Any:
    """Receive a trace event from a gateway and broadcast to WebSocket clients."""
    event = await request.json()
    _recent_traces.append(event)

    # Broadcast to all connected WebSocket clients
    disconnected = set()
    for ws in _ws_clients:
        try:
            await ws.send_json(event)
        except Exception:
            disconnected.add(ws)

    _ws_clients.difference_update(disconnected)
    return {"status": "ok", "clients": len(_ws_clients)}


@router.get("/api/traces/recent")
async def get_recent_traces(limit: int = 50) -> Any:
    """Get recent traces (for initial page load before WebSocket connects)."""
    traces = list(_recent_traces)[-limit:]
    return {"traces": traces, "total": len(traces)}


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
