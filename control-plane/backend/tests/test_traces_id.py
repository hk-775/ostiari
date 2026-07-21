"""Tests for stable trace_id: idempotent ingest + dedup."""

import pytest

pytestmark = pytest.mark.anyio


def _event(trace_id=None, **over):
    e = {
        "sidecar_id": "crm-agent", "action": "llm.messages", "tier": "allow",
        "score": 0, "agent_id": "claude-code", "framework": "claude-code",
        "model": "claude-sonnet-4-6", "timestamp": 1.0,
    }
    if trace_id is not None:
        e["trace_id"] = trace_id
    e.update(over)
    return e


class TestTraceId:
    async def test_ingest_assigns_trace_id_when_absent(self, client):
        r = await client.post("/api/traces/ingest", json=_event())
        assert r.status_code == 200
        body = r.json()
        assert body["trace_id"] and body["duplicate"] is False

    async def test_duplicate_trace_id_does_not_create_second_row(self, client):
        before = len((await client.get("/api/traces/recent?limit=500")).json()["traces"])
        ev = _event(trace_id="fixed-123")
        r1 = await client.post("/api/traces/ingest", json=ev)
        r2 = await client.post("/api/traces/ingest", json=ev)  # retry, same id
        assert r1.json()["duplicate"] is False
        assert r2.json()["duplicate"] is True
        after = (await client.get("/api/traces/recent?limit=500")).json()["traces"]
        matching = [t for t in after if t.get("trace_id") == "fixed-123"]
        assert len(matching) == 1                      # exactly one row, not two
        assert len(after) == before + 1                 # net +1 overall

    async def test_duplicate_updates_in_place(self, client):
        await client.post("/api/traces/ingest", json=_event(trace_id="upd-1", tier="allow"))
        await client.post("/api/traces/ingest", json=_event(trace_id="upd-1", tier="block"))
        traces = (await client.get("/api/traces/recent?limit=500")).json()["traces"]
        row = next(t for t in traces if t.get("trace_id") == "upd-1")
        assert row["tier"] == "block"                   # replaced in place

    async def test_recent_includes_trace_id(self, client):
        await client.post("/api/traces/ingest", json=_event(trace_id="rec-1"))
        traces = (await client.get("/api/traces/recent?limit=500")).json()["traces"]
        assert any(t.get("trace_id") == "rec-1" for t in traces)

    async def test_distinct_ids_are_distinct_rows(self, client):
        await client.post("/api/traces/ingest", json=_event(trace_id="a", timestamp=5.0))
        await client.post("/api/traces/ingest", json=_event(trace_id="b", timestamp=5.0))
        traces = (await client.get("/api/traces/recent?limit=500")).json()["traces"]
        ids = {t.get("trace_id") for t in traces}
        assert "a" in ids and "b" in ids               # same ts, different id -> both kept
