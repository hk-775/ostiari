"""Tests for parent/child span grouping (one prompt = one span tree)."""

import pytest

pytestmark = pytest.mark.anyio


def _ev(trace_id, session_id="", **over):
    e = {
        "trace_id": trace_id, "session_id": session_id, "action": "llm.messages",
        "tier": "allow", "agent_id": "claude-code", "model": "claude-sonnet-4-6",
        "duration_ms": 10.0, "params": {"input_tokens": 5, "output_tokens": 3},
        "timestamp": 1.0,
    }
    e.update(over)
    return e


class TestParentSpan:
    async def test_first_call_in_session_is_the_parent(self, client):
        r = await client.post("/api/traces/ingest", json=_ev("t1", "sess-A"))
        assert r.status_code == 200
        traces = (await client.get("/api/traces/recent?limit=50")).json()["traces"]
        row = next(t for t in traces if t["trace_id"] == "t1")
        assert row["parent_trace_id"] == "t1"       # parent references itself
        assert row["is_span_root"] is True

    async def test_later_calls_reference_the_parent(self, client):
        await client.post("/api/traces/ingest", json=_ev("t1", "sess-B"))
        await client.post("/api/traces/ingest", json=_ev("t2", "sess-B"))
        await client.post("/api/traces/ingest", json=_ev("t3", "sess-B"))
        traces = {t["trace_id"]: t for t in (await client.get("/api/traces/recent?limit=50")).json()["traces"]}
        assert traces["t2"]["parent_trace_id"] == "t1"
        assert traces["t3"]["parent_trace_id"] == "t1"
        assert traces["t2"]["is_span_root"] is False

    async def test_different_sessions_get_different_parents(self, client):
        await client.post("/api/traces/ingest", json=_ev("a1", "s1"))
        await client.post("/api/traces/ingest", json=_ev("b1", "s2"))
        traces = {t["trace_id"]: t for t in (await client.get("/api/traces/recent?limit=50")).json()["traces"]}
        assert traces["a1"]["parent_trace_id"] == "a1"
        assert traces["b1"]["parent_trace_id"] == "b1"
        assert traces["a1"]["parent_trace_id"] != traces["b1"]["parent_trace_id"]

    async def test_no_session_is_standalone_root(self, client):
        await client.post("/api/traces/ingest", json=_ev("solo", ""))
        traces = {t["trace_id"]: t for t in (await client.get("/api/traces/recent?limit=50")).json()["traces"]}
        assert traces["solo"]["parent_trace_id"] == "solo"

    async def test_spans_endpoint_groups_and_rolls_up(self, client):
        # 3 calls in one session → one span with a token/duration rollup
        await client.post("/api/traces/ingest", json=_ev("c1", "sess-C", tier="allow"))
        await client.post("/api/traces/ingest", json=_ev("c2", "sess-C", tier="block"))
        await client.post("/api/traces/ingest", json=_ev("c3", "sess-C", tier="allow"))
        spans = (await client.get("/api/traces/spans")).json()["spans"]
        span_c = next(s for s in spans if s["span_id"] == "c1")
        assert span_c["call_count"] == 3
        assert span_c["total_input_tokens"] == 15      # 3 × 5
        assert span_c["total_output_tokens"] == 9       # 3 × 3
        assert span_c["total_duration_ms"] == 30.0
        assert span_c["worst_tier"] == "block"          # escalates to worst child
        assert len(span_c["children"]) == 3


class TestSessionParentsEviction:
    """The session→parent map is LRU-bounded. Regression for the old bug where
    hitting the cap did a full .clear(), fragmenting a still-active session."""

    def _assign(self, tid, sid):
        from control_plane.routers import traces
        ev = _ev(tid, sid)
        traces._assign_parent(ev)
        return ev

    def test_active_session_survives_cap_via_lru(self, monkeypatch):
        from control_plane.routers import traces
        traces._session_parents.clear()   # unit test — no app fixture reset
        monkeypatch.setattr(traces, "_SESSION_PARENTS_MAX", 3)

        # Open the session we care about, then keep it "active" by touching it
        # while other sessions arrive and push past the cap.
        assert self._assign("hot-1", "hot")["parent_trace_id"] == "hot-1"
        for i in range(10):
            self._assign(f"o{i}", f"other-{i}")   # each new session may evict
            self._assign("hot-2", "hot")          # touch: keep 'hot' most-recent

        # 'hot' must still map to its original parent, not fragment into a new root.
        final = self._assign("hot-3", "hot")
        assert final["parent_trace_id"] == "hot-1"
        assert final["is_span_root"] is False

    def test_only_lru_entry_is_evicted_not_all(self, monkeypatch):
        from control_plane.routers import traces
        traces._session_parents.clear()   # unit test — no app fixture reset
        monkeypatch.setattr(traces, "_SESSION_PARENTS_MAX", 2)

        self._assign("a1", "A")
        self._assign("b1", "B")
        # C arrives at cap → evicts the LRU (A), but B must remain.
        self._assign("c1", "C")
        assert len(traces._session_parents) == 2
        # B still known (not cleared) → its next call references b1.
        assert self._assign("b2", "B")["parent_trace_id"] == "b1"
        # A was evicted → its next call starts a fresh parent.
        assert self._assign("a2", "A")["parent_trace_id"] == "a2"
