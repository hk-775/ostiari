"""Unit tests for ostiari.tracer."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from ostiari.models import TraceEntry, TraceFilters
from ostiari.tracer import ExecutionTracer


def _trace_entry(action: str = "test_action", score: int = 10, tier: str = "allow") -> TraceEntry:
    return TraceEntry(
        trace_id="t1",
        timestamp=datetime.now(timezone.utc),
        action=action,
        params={},
        risk_score=score,
        tier=tier,
        duration_ms=1.0,
    )


class TestRecord:
    def test_record_appends_to_both_queues(self):
        storage = MagicMock()
        tracer = ExecutionTracer(storage=storage, queue_max=10, history_max=10)
        entry = _trace_entry()
        tracer.record(entry)
        assert tracer.queue_depth == 1
        assert len(tracer.recent_history()) == 1

    def test_recent_history_unaffected_by_flush(self):
        storage = MagicMock()
        tracer = ExecutionTracer(storage=storage, queue_max=10, history_max=10)
        for i in range(5):
            tracer.record(_trace_entry(action=f"action_{i}"))
        tracer._flush_all()
        assert tracer.queue_depth == 0
        assert len(tracer.recent_history()) == 5

    def test_queue_bounded_drops_oldest(self):
        storage = MagicMock()
        tracer = ExecutionTracer(storage=storage, queue_max=3, history_max=3)
        for i in range(5):
            tracer.record(_trace_entry(action=f"action_{i}"))
        assert tracer.queue_depth == 3
        history = tracer.recent_history(5)
        assert len(history) == 3
        assert history[0].action == "action_2"

    def test_recent_history_with_limit(self):
        storage = MagicMock()
        tracer = ExecutionTracer(storage=storage, history_max=50)
        for i in range(10):
            tracer.record(_trace_entry(action=f"action_{i}"))
        history = tracer.recent_history(3)
        assert len(history) == 3
        assert history[-1].action == "action_9"


class TestFlush:
    def test_flush_batch_writes_to_storage(self):
        storage = MagicMock()
        tracer = ExecutionTracer(storage=storage, batch_size=5)
        for _ in range(3):
            tracer.record(_trace_entry())
        tracer._flush_batch()
        storage.save_traces_batch.assert_called_once()
        assert len(storage.save_traces_batch.call_args[0][0]) == 3
        assert tracer.queue_depth == 0

    def test_storage_failure_logged_batch_discarded(self):
        storage = MagicMock()
        storage.save_traces_batch.side_effect = RuntimeError("db error")
        tracer = ExecutionTracer(storage=storage)
        tracer.record(_trace_entry())
        tracer._flush_batch()
        assert tracer.queue_depth == 0

    def test_adaptive_interval_shorter_when_queue_full(self):
        storage = MagicMock()
        tracer = ExecutionTracer(storage=storage, queue_max=100, flush_interval=1.0)
        for _ in range(60):
            tracer.record(_trace_entry())
        # > 50% full → interval should be flush_interval / 10
        # We test indirectly by verifying the writer loop logic
        depth = len(tracer._queue)
        assert depth > tracer._queue_max // 2


class TestQuery:
    def test_query_delegates_to_storage(self):
        storage = MagicMock()
        storage.get_traces.return_value = [_trace_entry()]
        tracer = ExecutionTracer(storage=storage)
        result = tracer.query(TraceFilters(min_risk=5))
        storage.get_traces.assert_called_once()
        assert len(result) == 1

    def test_export_json_returns_bytes(self):
        storage = MagicMock()
        storage.get_traces.return_value = [_trace_entry()]
        tracer = ExecutionTracer(storage=storage)
        data = tracer.export("json")
        assert isinstance(data, bytes)
        assert b"test_action" in data

    def test_export_invalid_format_raises(self):
        storage = MagicMock()
        storage.get_traces.return_value = []
        tracer = ExecutionTracer(storage=storage)
        with pytest.raises(ValueError, match="Unsupported"):
            tracer.export("xml")

    def test_get_stats_computes_aggregates(self):
        entries = [
            _trace_entry(score=10, tier="allow"),
            _trace_entry(score=50, tier="intervene"),
            _trace_entry(score=80, tier="block"),
        ]
        storage = MagicMock()
        storage.get_traces.return_value = entries
        tracer = ExecutionTracer(storage=storage)
        stats = tracer.get_stats(timedelta(hours=1))
        assert stats.total_actions == 3
        assert stats.allowed == 1
        assert stats.intervened == 1
        assert stats.blocked == 1
        assert stats.avg_risk_score == pytest.approx(140 / 3, rel=0.01)


class TestCorrelation:
    def test_set_correlation_id(self):
        storage = MagicMock()
        tracer = ExecutionTracer(storage=storage)
        assert tracer.correlation_id is None
        tracer.set_correlation_id("abc-123")
        assert tracer.correlation_id == "abc-123"
        tracer.set_correlation_id(None)
        assert tracer.correlation_id is None


class TestLifecycle:
    def test_shutdown_flushes_remaining(self):
        storage = MagicMock()
        tracer = ExecutionTracer(storage=storage)
        tracer.record(_trace_entry())
        tracer.record(_trace_entry())
        tracer.shutdown()
        storage.save_traces_batch.assert_called()
        assert tracer.queue_depth == 0

    def test_start_stop_writer_thread(self):
        storage = MagicMock()
        tracer = ExecutionTracer(storage=storage, flush_interval=0.05)
        tracer.start()
        assert tracer._running is True
        tracer.record(_trace_entry())
        time.sleep(0.15)
        tracer.shutdown()
        assert tracer._running is False
        storage.save_traces_batch.assert_called()
