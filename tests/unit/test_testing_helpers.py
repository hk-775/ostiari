"""Unit tests for the ostiari.testing helpers module."""

from __future__ import annotations

from datetime import datetime, timezone

from ostiari.adapters.protocol import validate_adapter
from ostiari.models import AnomalySignal, TraceEntry, TraceFilters
from ostiari.testing import MockAdapter, MockDetector, MockStorage


def _make_trace(action="test", tier="allow", risk_score=20):
    return TraceEntry(
        trace_id="t-1",
        correlation_id="agent-1",
        timestamp=datetime.now(timezone.utc),
        action=action,
        params={},
        result=None,
        risk_score=risk_score,
        tier=tier,
        duration_ms=1.0,
        signals=[],
        anomalies=[],
        breaker_state=None,
        metadata={},
    )


class TestMockStorage:
    def test_save_and_get_trace(self):
        storage = MockStorage()
        trace = _make_trace()
        storage.save_trace(trace)
        assert storage.get_trace("t-1") == trace

    def test_get_traces_with_filters(self):
        storage = MockStorage()
        storage.save_trace(_make_trace("a.read", "allow", 10))
        storage.save_trace(_make_trace("b.write", "block", 80))

        results = storage.get_traces(TraceFilters(tier="block", limit=10))
        assert len(results) == 1
        assert results[0].action == "b.write"

    def test_save_traces_batch(self):
        storage = MockStorage()
        traces = [_make_trace(f"action.{i}") for i in range(5)]
        storage.save_traces_batch(traces)
        assert len(storage.get_traces(TraceFilters(limit=100))) == 5

    def test_schema_version(self):
        storage = MockStorage()
        assert storage.schema_version() == 1

    def test_close_no_error(self):
        storage = MockStorage()
        storage.close()

    def test_breaker_state(self):
        from datetime import datetime, timezone

        from ostiari.models import BreakerState

        storage = MockStorage()
        state = BreakerState(
            breaker_id="cost",
            state="closed",
            last_checked=datetime.now(timezone.utc),
            recovery_mode="auto_retry",
        )
        storage.save_breaker_state(state)
        assert storage.get_breaker_state("cost") == state
        assert storage.get_breaker_state("nonexistent") is None


class TestMockDetector:
    def test_returns_configured_signals(self):
        signal = AnomalySignal(
            detector="test",
            severity="medium",
            description="test anomaly",
            score_contribution=20,
        )
        detector = MockDetector(signals=[signal])
        result = detector.detect("action", {}, [])
        assert result == signal
        assert detector.call_count == 1

    def test_returns_none_when_empty(self):
        detector = MockDetector()
        result = detector.detect("action", {}, [])
        assert result is None

    def test_name_property(self):
        detector = MockDetector()
        assert detector.name == "mock"


class TestMockAdapter:
    def test_passes_protocol_validation(self):
        adapter = MockAdapter()
        validate_adapter(adapter)

    def test_wrap_tool_call_records(self):
        adapter = MockAdapter(name="test-adapter")
        ctx = adapter.wrap_tool_call("search", {"q": "hello"})
        assert ctx.action == "search"
        assert ctx.params == {"q": "hello"}
        assert len(adapter.pre_hook_calls) == 1

    def test_on_result_records(self):
        adapter = MockAdapter()
        ctx = adapter.wrap_tool_call("test", {})
        adapter.on_result(ctx, {"result": "ok"})
        assert len(adapter.post_hook_calls) == 1

    def test_on_error_records(self):
        adapter = MockAdapter()
        ctx = adapter.wrap_tool_call("test", {})
        adapter.on_error(ctx, ValueError("fail"))
        assert len(adapter.error_hook_calls) == 1

    def test_name_property(self):
        adapter = MockAdapter(name="custom")
        assert adapter.name == "custom"
