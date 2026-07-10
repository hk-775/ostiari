"""Property-based tests for ostiari.storage round-trip properties."""

from datetime import datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from ostiari.models import (
    BreakerState,
    Checkpoint,
    TraceEntry,
    TraceFilters,
)
from ostiari.storage.sqlite import SQLiteBackend


def make_backend(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "test.db"
    return SQLiteBackend(path=path)


safe_text = st.text(min_size=1, max_size=50, alphabet="abcdefghijklmnopqrstuvwxyz_")
safe_dict = st.dictionaries(
    keys=safe_text,
    values=st.one_of(st.integers(min_value=0, max_value=1000), safe_text),
    max_size=3,
)


@st.composite
def trace_entries(draw):
    return TraceEntry(
        trace_id=draw(st.text(min_size=8, max_size=32, alphabet="abcdef0123456789")),
        timestamp=datetime(2024, 1, 1) + timedelta(minutes=draw(st.integers(0, 10000))),
        action=draw(safe_text),
        params=draw(safe_dict),
        risk_score=draw(st.integers(min_value=0, max_value=100)),
        tier=draw(st.sampled_from(["allow", "intervene", "block"])),
        duration_ms=draw(st.floats(min_value=0, max_value=1000)),
        signals=[],
        anomalies=[],
    )


@st.composite
def checkpoints(draw):
    return Checkpoint(
        checkpoint_id=draw(st.text(min_size=8, max_size=32, alphabet="abcdef0123456789")),
        sequence_number=draw(st.integers(min_value=0, max_value=10000)),
        timestamp=datetime(2024, 1, 1) + timedelta(minutes=draw(st.integers(0, 10000))),
        state=draw(safe_dict),
        action=draw(safe_text),
        params=draw(safe_dict),
    )


@given(entry=trace_entries())
@settings(max_examples=20)
def test_trace_round_trip(entry, tmp_path_factory):
    backend = SQLiteBackend(path=tmp_path_factory.mktemp("db") / "test.db")
    try:
        backend.save_trace(entry)
        traces = backend.get_traces(TraceFilters(limit=1))
        assert len(traces) == 1
        retrieved = traces[0]
        assert retrieved.trace_id == entry.trace_id
        assert retrieved.action == entry.action
        assert retrieved.risk_score == entry.risk_score
        assert retrieved.tier == entry.tier
    finally:
        backend.close()


@given(cp=checkpoints())
@settings(max_examples=20)
def test_checkpoint_round_trip(cp, tmp_path_factory):
    backend = SQLiteBackend(path=tmp_path_factory.mktemp("db") / "test.db")
    try:
        backend.save_checkpoint(cp)
        retrieved = backend.get_checkpoint(cp.checkpoint_id)
        assert retrieved.checkpoint_id == cp.checkpoint_id
        assert retrieved.sequence_number == cp.sequence_number
        assert retrieved.action == cp.action
    finally:
        backend.close()


@given(entries=st.lists(trace_entries(), min_size=1, max_size=10))
@settings(max_examples=10)
def test_batch_save_count(entries, tmp_path_factory):
    # Ensure unique trace_ids
    seen = set()
    unique_entries = []
    for e in entries:
        if e.trace_id not in seen:
            seen.add(e.trace_id)
            unique_entries.append(e)

    backend = SQLiteBackend(path=tmp_path_factory.mktemp("db") / "test.db")
    try:
        backend.save_traces_batch(unique_entries)
        traces = backend.get_traces(TraceFilters(limit=1000))
        assert len(traces) == len(unique_entries)
    finally:
        backend.close()


@given(
    state=st.sampled_from(["closed", "open", "half_open"]),
    mode=st.sampled_from(["auto_retry", "notify", "terminate"]),
)
@settings(max_examples=10)
def test_breaker_state_round_trip(state, mode, tmp_path_factory):
    breaker = BreakerState(
        breaker_id="test_breaker",
        state=state,
        last_checked=datetime(2024, 6, 15),
        metrics={"error_count": 5.0},
        recovery_mode=mode,
    )
    backend = SQLiteBackend(path=tmp_path_factory.mktemp("db") / "test.db")
    try:
        backend.save_breaker_state(breaker)
        retrieved = backend.get_breaker_state("test_breaker")
        assert retrieved is not None
        assert retrieved.state == state
        assert retrieved.recovery_mode == mode
    finally:
        backend.close()
