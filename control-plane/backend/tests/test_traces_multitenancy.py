"""Trace multi-tenancy: the ingest→buffer→broadcast path is per-org.

This is the fix for the cross-tenant leak where every org's traces were
broadcast to every connected viewer. We verify:
- an ingested event lands only in its own org's buffer,
- an event with no org_id defaults to "default" (back-compat),
- the WebSocket fan-out only targets the event's org (verified with fake
  sockets, since the ASGI test client doesn't do real WebSockets),
- the /recent read endpoint is org-scoped.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio


class _FakeWS:
    """Minimal stand-in for a connected WebSocket that records what it's sent."""
    def __init__(self):
        self.received: list[dict] = []

    async def send_json(self, data):
        self.received.append(data)


class TestIngestScoping:
    async def test_event_lands_in_its_own_org_buffer(self, client):
        from control_plane.routers import traces
        traces._recent_traces.clear()
        await client.post("/api/traces/ingest", json={
            "trace_id": "t-a", "org_id": "org-a", "action": "x", "tier": "allow",
        })
        await client.post("/api/traces/ingest", json={
            "trace_id": "t-b", "org_id": "org-b", "action": "y", "tier": "allow",
        })
        a_ids = [t["trace_id"] for t in traces._recent_traces["org-a"]]
        b_ids = [t["trace_id"] for t in traces._recent_traces["org-b"]]
        assert a_ids == ["t-a"]
        assert b_ids == ["t-b"]

    async def test_event_without_org_defaults_to_default(self, client):
        from control_plane.routers import traces
        traces._recent_traces.clear()
        await client.post("/api/traces/ingest", json={
            "trace_id": "t-def", "action": "x", "tier": "allow",
        })
        assert [t["trace_id"] for t in traces._recent_traces["default"]] == ["t-def"]

    async def test_broadcast_only_reaches_same_org_sockets(self, client):
        from control_plane.routers import traces
        traces._recent_traces.clear()
        traces._ws_clients.clear()
        sock_a, sock_b = _FakeWS(), _FakeWS()
        traces._ws_clients["org-a"].add(sock_a)
        traces._ws_clients["org-b"].add(sock_b)

        await client.post("/api/traces/ingest", json={
            "trace_id": "leak-check", "org_id": "org-a", "action": "x", "tier": "allow",
        })
        # Only org-a's socket should have received the event — no cross-tenant leak.
        assert [e["trace_id"] for e in sock_a.received] == ["leak-check"]
        assert sock_b.received == []


class TestRecentReadScoping:
    async def test_recent_is_org_scoped(self, client):
        from control_plane.auth.service import create_access_token
        from control_plane.routers import traces
        traces._recent_traces.clear()
        for tid, org in [("r-a", "org-a"), ("r-b", "org-b")]:
            await client.post("/api/traces/ingest", json={
                "trace_id": tid, "org_id": org, "action": "x", "tier": "allow",
            })

        def hdr(org):
            t = create_access_token(user_id=1, email=f"{org}@t.io", role="admin", org=org)
            return {"Authorization": f"Bearer {t}"}

        a = (await client.get("/api/traces/recent", headers=hdr("org-a"))).json()
        b = (await client.get("/api/traces/recent", headers=hdr("org-b"))).json()
        assert [t["trace_id"] for t in a["traces"]] == ["r-a"]
        assert [t["trace_id"] for t in b["traces"]] == ["r-b"]
