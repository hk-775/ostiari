"""Live trace viewer — receives traces from gateways and broadcasts via WebSocket."""

import asyncio
import logging
from collections import deque
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

log = logging.getLogger("control_plane.traces")

router = APIRouter(tags=["traces"])

# In-memory buffer of recent traces (for new WebSocket clients to catch up)
_recent_traces: deque[dict[str, Any]] = deque(maxlen=200)
_ws_clients: set[WebSocket] = set()


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
