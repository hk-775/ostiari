"""The gateway stamps a stable, unique trace_id on every reported trace."""

from __future__ import annotations

import httpx
import pytest
from ostiari_gateway.trace_reporter import TraceReporter


@pytest.mark.anyio
async def test_report_stamps_unique_trace_id(monkeypatch):
    captured: list[dict] = []

    class _Client(httpx.AsyncClient):
        async def post(self, url, json=None, **kw):  # type: ignore[override]
            captured.append(json)
            return httpx.Response(200, json={"status": "ok"})

    tr = TraceReporter(control_plane_url="http://cp.local", sidecar_id="gw1")
    tr._client = _Client()

    await tr.report(action="llm.messages", tier="allow", score=0, duration_ms=1.0,
                    agent_id="claude-code")
    await tr.report(action="llm.messages", tier="allow", score=0, duration_ms=1.0,
                    agent_id="claude-code")

    assert len(captured) == 2
    ids = [e.get("trace_id") for e in captured]
    assert all(ids), "every event must carry a trace_id"
    assert ids[0] != ids[1], "trace_ids must be unique per call"
    assert all(len(i) >= 16 for i in ids), "trace_id should be a substantial unique token"


@pytest.mark.anyio
async def test_reporter_sends_ingest_and_service_credentials(monkeypatch):
    calls: list[tuple[str, dict[str, str]]] = []

    class _Client(httpx.AsyncClient):
        async def post(self, url, json=None, **kw):  # type: ignore[override]
            calls.append((url, kw.get("headers", {})))
            return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setenv("OSTIARI_INGEST_KEY", "ingest-secret")
    monkeypatch.setenv("OSTIARI_SERVICE_TOKEN", "service-secret")
    tr = TraceReporter(control_plane_url="http://cp.local", sidecar_id="gw1")
    tr._client = _Client()

    await tr.report(action="tool", tier="allow", score=0, duration_ms=1)
    await tr.report_payment(
        agent_id="a", action="tool", amount_usdc=0.1, settled=True
    )

    assert calls[0][1] == {"X-Ingest-Key": "ingest-secret"}
    assert calls[1][1] == {"X-Ostiari-Service-Key": "service-secret"}
