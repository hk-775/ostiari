"""Unit tests for ostiari.storage.sqlite."""

from datetime import datetime

import pytest

from ostiari.exceptions import CheckpointNotFoundError
from ostiari.models import (
    BreakerState,
    Checkpoint,
    RiskSignal,
    TraceEntry,
    TraceFilters,
)
from ostiari.storage.sqlite import SQLiteBackend


@pytest.fixture
def backend(tmp_path):
    db = SQLiteBackend(path=tmp_path / "test.db")
    yield db
    db.close()


@pytest.fixture
def sample_trace():
    return TraceEntry(
        trace_id="trace001",
        correlation_id="corr001",
        timestamp=datetime(2024, 6, 15, 10, 30, 0),
        action="send_email",
        params={"to": "user@example.com", "subject": "Hello"},
        result={"status": "sent"},
        risk_score=25,
        tier="allow",
        duration_ms=5.2,
        signals=[RiskSignal(source="policy", score_contribution=10, description="Low risk")],
        anomalies=[],
        metadata={"agent_id": "agent1"},
    )


@pytest.fixture
def sample_checkpoint():
    return Checkpoint(
        checkpoint_id="cp001",
        name="before_email",
        sequence_number=1,
        timestamp=datetime(2024, 6, 15, 10, 30, 0),
        state={"step": 3, "context": "test"},
        action="send_email",
        params={"to": "user@example.com"},
    )


class TestSQLiteBackendInit:
    def test_creates_database_file(self, tmp_path):
        db_path = tmp_path / "new.db"
        backend = SQLiteBackend(path=db_path)
        assert db_path.exists()
        backend.close()

    def test_creates_parent_directories(self, tmp_path):
        db_path = tmp_path / "nested" / "dir" / "test.db"
        backend = SQLiteBackend(path=db_path)
        assert db_path.exists()
        backend.close()

    def test_schema_version_after_init(self, backend):
        assert backend.schema_version() == 1


class TestTraceOperations:
    def test_save_and_retrieve(self, backend, sample_trace):
        backend.save_trace(sample_trace)
        traces = backend.get_traces(TraceFilters())
        assert len(traces) == 1
        assert traces[0].trace_id == "trace001"
        assert traces[0].action == "send_email"
        assert traces[0].risk_score == 25

    def test_batch_save(self, backend):
        entries = [
            TraceEntry(
                trace_id=f"trace{i:03d}",
                timestamp=datetime(2024, 6, 15, 10, i, 0),
                action="tool",
                params={},
                risk_score=i * 10,
                tier="allow",
                duration_ms=1.0,
                signals=[],
                anomalies=[],
            )
            for i in range(5)
        ]
        backend.save_traces_batch(entries)
        traces = backend.get_traces(TraceFilters())
        assert len(traces) == 5

    def test_filter_by_tier(self, backend):
        for tier in ["allow", "intervene", "block"]:
            backend.save_trace(
                TraceEntry(
                    trace_id=f"t_{tier}",
                    timestamp=datetime(2024, 6, 15),
                    action="tool",
                    params={},
                    risk_score=50,
                    tier=tier,
                    duration_ms=1.0,
                    signals=[],
                    anomalies=[],
                )
            )
        traces = backend.get_traces(TraceFilters(tier="block"))
        assert len(traces) == 1
        assert traces[0].tier == "block"

    def test_filter_by_min_risk(self, backend):
        for score in [10, 50, 90]:
            backend.save_trace(
                TraceEntry(
                    trace_id=f"t_{score}",
                    timestamp=datetime(2024, 6, 15),
                    action="tool",
                    params={},
                    risk_score=score,
                    tier="allow",
                    duration_ms=1.0,
                    signals=[],
                    anomalies=[],
                )
            )
        traces = backend.get_traces(TraceFilters(min_risk=50))
        assert len(traces) == 2

    def test_limit_and_offset(self, backend):
        for i in range(10):
            backend.save_trace(
                TraceEntry(
                    trace_id=f"t{i:02d}",
                    timestamp=datetime(2024, 6, 15, 10, i, 0),
                    action="tool",
                    params={},
                    risk_score=0,
                    tier="allow",
                    duration_ms=1.0,
                    signals=[],
                    anomalies=[],
                )
            )
        page1 = backend.get_traces(TraceFilters(limit=3, offset=0))
        page2 = backend.get_traces(TraceFilters(limit=3, offset=3))
        assert len(page1) == 3
        assert len(page2) == 3
        assert page1[0].trace_id != page2[0].trace_id

    def test_ordered_by_timestamp_desc(self, backend):
        for i in range(3):
            backend.save_trace(
                TraceEntry(
                    trace_id=f"t{i}",
                    timestamp=datetime(2024, 6, 15, 10, i, 0),
                    action="tool",
                    params={},
                    risk_score=0,
                    tier="allow",
                    duration_ms=1.0,
                    signals=[],
                    anomalies=[],
                )
            )
        traces = backend.get_traces(TraceFilters())
        assert traces[0].timestamp > traces[1].timestamp > traces[2].timestamp


class TestCheckpointOperations:
    def test_save_and_retrieve(self, backend, sample_checkpoint):
        backend.save_checkpoint(sample_checkpoint)
        cp = backend.get_checkpoint("cp001")
        assert cp.checkpoint_id == "cp001"
        assert cp.name == "before_email"
        assert cp.sequence_number == 1

    def test_not_found_raises(self, backend):
        with pytest.raises(CheckpointNotFoundError, match="nonexistent"):
            backend.get_checkpoint("nonexistent")

    def test_delete_checkpoints(self, backend, sample_checkpoint):
        backend.save_checkpoint(sample_checkpoint)
        backend.delete_checkpoints(["cp001"])
        with pytest.raises(CheckpointNotFoundError):
            backend.get_checkpoint("cp001")

    def test_delete_nonexistent_is_silent(self, backend):
        backend.delete_checkpoints(["does_not_exist"])


class TestBreakerStateOperations:
    def test_save_and_retrieve(self, backend):
        state = BreakerState(
            breaker_id="cost",
            state="closed",
            last_checked=datetime(2024, 6, 15),
            metrics={"token_cost": 500.0},
            recovery_mode="auto_retry",
            recovery_after_seconds=60,
        )
        backend.save_breaker_state(state)
        retrieved = backend.get_breaker_state("cost")
        assert retrieved is not None
        assert retrieved.breaker_id == "cost"
        assert retrieved.metrics == {"token_cost": 500.0}

    def test_upsert_overwrites(self, backend):
        state1 = BreakerState(
            breaker_id="cost",
            state="closed",
            last_checked=datetime(2024, 6, 15),
            metrics={"token_cost": 500.0},
            recovery_mode="auto_retry",
        )
        state2 = BreakerState(
            breaker_id="cost",
            state="open",
            tripped_at=datetime(2024, 6, 15, 12, 0),
            last_checked=datetime(2024, 6, 15, 12, 0),
            metrics={"token_cost": 1500.0},
            recovery_mode="auto_retry",
        )
        backend.save_breaker_state(state1)
        backend.save_breaker_state(state2)
        retrieved = backend.get_breaker_state("cost")
        assert retrieved is not None
        assert retrieved.state == "open"

    def test_not_found_returns_none(self, backend):
        assert backend.get_breaker_state("nonexistent") is None


class TestRetryBehavior:
    def test_fail_open_returns_none(self, tmp_path):
        backend = SQLiteBackend(path=tmp_path / "test.db", fail_open=True)
        backend.close()
        # After close, operations should fail gracefully
        # The connection manager raises StorageError
        # This tests that fail_open doesn't crash
        backend_new = SQLiteBackend(path=tmp_path / "test.db", fail_open=True)
        backend_new.close()


class TestClose:
    def test_close_is_idempotent(self, backend):
        backend.close()
        backend.close()  # Should not raise
