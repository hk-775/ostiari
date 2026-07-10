"""Unit tests for ostiari.breaker."""

from __future__ import annotations

from collections import deque
from unittest.mock import MagicMock

import pytest

from ostiari.breaker import CircuitBreaker
from ostiari.exceptions import AgentTerminatedError, BreakerTrippedError
from ostiari.models import BreakerConfig, BreakerState, MetricType


def _make_clock(start: float = 0.0):
    time_ref = [start]

    def clock():
        return time_ref[0]

    def advance(seconds: float):
        time_ref[0] += seconds

    return clock, advance


def _cost_config(threshold: float = 100.0, recovery: int = 60) -> BreakerConfig:
    return BreakerConfig(
        metric=MetricType.TOKEN_COST,
        threshold=threshold,
        recovery_mode="auto_retry",
        recovery_after_seconds=recovery,
    )


def _error_config(threshold: float = 3.0, recovery: int = 60) -> BreakerConfig:
    return BreakerConfig(
        metric=MetricType.CONSECUTIVE_FAILURES,
        threshold=threshold,
        recovery_mode="auto_retry",
        recovery_after_seconds=recovery,
    )


def _actions_config(threshold: float = 50.0) -> BreakerConfig:
    return BreakerConfig(
        metric=MetricType.TOTAL_ACTIONS,
        threshold=threshold,
        recovery_mode="auto_retry",
        recovery_after_seconds=60,
    )


class TestStateMachine:
    def test_initial_state_closed(self):
        clock, _ = _make_clock()
        cb = CircuitBreaker(configs=[_cost_config()], clock=clock)
        assert cb.check() is None

    def test_trip_on_threshold_exceeded(self):
        clock, _ = _make_clock()
        cb = CircuitBreaker(configs=[_cost_config(threshold=100)], clock=clock)
        cb.record(MetricType.TOKEN_COST, 101)
        with pytest.raises(BreakerTrippedError) as exc_info:
            cb.check()
        assert exc_info.value.breaker_id == "token_cost"

    def test_half_open_after_recovery(self):
        clock, advance = _make_clock()
        cb = CircuitBreaker(configs=[_cost_config(threshold=100, recovery=60)], clock=clock)
        cb.record(MetricType.TOKEN_COST, 101)
        advance(61)
        probing = cb.check()
        assert probing == "token_cost"

    def test_close_after_successful_probe(self):
        clock, advance = _make_clock()
        cb = CircuitBreaker(configs=[_cost_config(threshold=100, recovery=60)], clock=clock)
        cb.record(MetricType.TOKEN_COST, 101)
        advance(61)
        probing = cb.check()
        cb.report_probe_result(probing, success=True)
        assert cb.check() is None

    def test_reopen_after_failed_probe(self):
        clock, advance = _make_clock()
        cb = CircuitBreaker(configs=[_cost_config(threshold=100, recovery=60)], clock=clock)
        cb.record(MetricType.TOKEN_COST, 101)
        advance(61)
        probing = cb.check()
        cb.report_probe_result(probing, success=False)
        with pytest.raises(BreakerTrippedError):
            cb.check()

    def test_half_open_blocks_concurrent(self):
        clock, advance = _make_clock()
        cb = CircuitBreaker(configs=[_cost_config(threshold=100, recovery=60)], clock=clock)
        cb.record(MetricType.TOKEN_COST, 101)
        advance(61)
        probing = cb.check()
        assert probing is not None
        with pytest.raises(BreakerTrippedError):
            cb.check()


class TestMetricRecording:
    def test_accumulates_token_cost(self):
        clock, _ = _make_clock()
        cb = CircuitBreaker(configs=[_cost_config(threshold=100)], clock=clock)
        cb.record(MetricType.TOKEN_COST, 40)
        cb.record(MetricType.TOKEN_COST, 30)
        assert cb.check() is None
        cb.record(MetricType.TOKEN_COST, 31)
        with pytest.raises(BreakerTrippedError):
            cb.check()

    def test_total_actions_trips(self):
        clock, _ = _make_clock()
        cb = CircuitBreaker(configs=[_actions_config(threshold=3)], clock=clock)
        cb.record(MetricType.TOTAL_ACTIONS, 1)
        cb.record(MetricType.TOTAL_ACTIONS, 1)
        assert cb.check() is None
        cb.record(MetricType.TOTAL_ACTIONS, 1)
        with pytest.raises(BreakerTrippedError):
            cb.check()

    def test_unknown_metric_ignored(self):
        clock, _ = _make_clock()
        cb = CircuitBreaker(configs=[_cost_config()], clock=clock)
        cb.record(MetricType.WALL_CLOCK_MS, 9999)
        assert cb.check() is None

    def test_record_ignored_when_open(self):
        clock, _ = _make_clock()
        cb = CircuitBreaker(configs=[_cost_config(threshold=100)], clock=clock)
        cb.record(MetricType.TOKEN_COST, 101)
        cb.record(MetricType.TOKEN_COST, 500)
        metrics = cb.get_metrics()
        assert metrics[0].current_value == 101


class TestReportOutcome:
    def test_consecutive_failures_trips(self):
        clock, _ = _make_clock()
        cb = CircuitBreaker(configs=[_error_config(threshold=3)], clock=clock)
        cb.report_outcome("tool_a", success=False)
        cb.report_outcome("tool_a", success=False)
        assert cb.check() is None
        cb.report_outcome("tool_a", success=False)
        with pytest.raises(BreakerTrippedError):
            cb.check()

    def test_success_resets_counter(self):
        clock, _ = _make_clock()
        cb = CircuitBreaker(configs=[_error_config(threshold=3)], clock=clock)
        cb.report_outcome("tool_a", success=False)
        cb.report_outcome("tool_a", success=False)
        cb.report_outcome("tool_a", success=True)
        cb.report_outcome("tool_a", success=False)
        assert cb.check() is None

    def test_no_breaker_configured_logs_warning(self, caplog):
        clock, _ = _make_clock()
        cb = CircuitBreaker(configs=[_cost_config()], clock=clock)
        import logging

        with caplog.at_level(logging.WARNING, logger="ostiari"):
            cb.report_outcome("tool_a", success=False)
        assert "no consecutive_failures breaker configured" in caplog.text


class TestRecoveryModes:
    def test_terminate_raises_agent_terminated(self):
        clock, _ = _make_clock()
        cfg = BreakerConfig(
            metric=MetricType.TOKEN_COST,
            threshold=100,
            recovery_mode="terminate",
            recovery_after_seconds=60,
        )
        cb = CircuitBreaker(configs=[cfg], clock=clock)
        cb.record(MetricType.TOKEN_COST, 101)
        with pytest.raises(AgentTerminatedError) as exc_info:
            cb.check()
        assert exc_info.value.breaker_id == "token_cost"

    def test_notify_raises_breaker_tripped(self):
        clock, _ = _make_clock()
        cfg = BreakerConfig(
            metric=MetricType.TOKEN_COST,
            threshold=100,
            recovery_mode="notify",
            recovery_after_seconds=60,
        )
        cb = CircuitBreaker(configs=[cfg], clock=clock)
        cb.record(MetricType.TOKEN_COST, 101)
        with pytest.raises(BreakerTrippedError):
            cb.check()


class TestReset:
    def test_manual_reset_closes_breaker(self):
        clock, _ = _make_clock()
        cb = CircuitBreaker(configs=[_cost_config(threshold=100)], clock=clock)
        cb.record(MetricType.TOKEN_COST, 101)
        with pytest.raises(BreakerTrippedError):
            cb.check()
        cb.reset("token_cost")
        assert cb.check() is None

    def test_reset_clears_counter(self):
        clock, _ = _make_clock()
        cb = CircuitBreaker(configs=[_cost_config(threshold=100)], clock=clock)
        cb.record(MetricType.TOKEN_COST, 101)
        cb.reset("token_cost")
        metrics = cb.get_metrics()
        assert metrics[0].current_value == 0.0

    def test_reset_unknown_breaker_no_error(self):
        clock, _ = _make_clock()
        cb = CircuitBreaker(configs=[_cost_config()], clock=clock)
        cb.reset("nonexistent")


class TestAdaptiveThreshold:
    def _make_tracer_with_history(self, durations: list[float]):
        tracer = MagicMock()
        entries = [MagicMock(duration_ms=d, tier="allow", metadata={}) for d in durations]
        tracer.recent_history.return_value = entries
        return tracer

    def test_adaptive_uses_mean_plus_stddev(self):
        durations = [10.0] * 20
        tracer = self._make_tracer_with_history(durations)
        clock, _ = _make_clock()
        cfg = BreakerConfig(
            metric=MetricType.WALL_CLOCK_MS,
            threshold=1000,
            recovery_mode="auto_retry",
            recovery_after_seconds=60,
        )
        cb = CircuitBreaker(configs=[cfg], clock=clock, tracer=tracer)
        cb.enable_adaptive(sensitivity=2.0, min_samples=10)
        cb.record(MetricType.WALL_CLOCK_MS, 11)
        assert cb.check() is None

    def test_adaptive_trips_on_spike(self):
        durations = [10.0] * 20
        tracer = self._make_tracer_with_history(durations)
        clock, _ = _make_clock()
        cfg = BreakerConfig(
            metric=MetricType.WALL_CLOCK_MS,
            threshold=1000,
            recovery_mode="auto_retry",
            recovery_after_seconds=60,
        )
        cb = CircuitBreaker(configs=[cfg], clock=clock, tracer=tracer)
        cb.enable_adaptive(sensitivity=2.0, min_samples=10)
        cb.record(MetricType.WALL_CLOCK_MS, 500)
        with pytest.raises(BreakerTrippedError):
            cb.check()

    def test_insufficient_data_uses_static(self):
        tracer = self._make_tracer_with_history([10.0] * 5)
        clock, _ = _make_clock()
        cfg = BreakerConfig(
            metric=MetricType.WALL_CLOCK_MS,
            threshold=1000,
            recovery_mode="auto_retry",
            recovery_after_seconds=60,
        )
        cb = CircuitBreaker(configs=[cfg], clock=clock, tracer=tracer)
        cb.enable_adaptive(sensitivity=2.0, min_samples=10)
        cb.record(MetricType.WALL_CLOCK_MS, 999)
        assert cb.check() is None

    def test_safety_floor_50_percent(self):
        durations = [1.0] * 20
        tracer = self._make_tracer_with_history(durations)
        clock, _ = _make_clock()
        cfg = BreakerConfig(
            metric=MetricType.WALL_CLOCK_MS,
            threshold=100,
            recovery_mode="auto_retry",
            recovery_after_seconds=60,
        )
        cb = CircuitBreaker(configs=[cfg], clock=clock, tracer=tracer)
        cb.enable_adaptive(sensitivity=2.0, min_samples=10)
        cb.record(MetricType.WALL_CLOCK_MS, 49)
        assert cb.check() is None
        cb.record(MetricType.WALL_CLOCK_MS, 2)
        with pytest.raises(BreakerTrippedError):
            cb.check()


class TestMultipleBreakers:
    def test_independent_breakers(self):
        clock, _ = _make_clock()
        cb = CircuitBreaker(
            configs=[_cost_config(threshold=100), _actions_config(threshold=50)],
            clock=clock,
        )
        cb.record(MetricType.TOKEN_COST, 101)
        with pytest.raises(BreakerTrippedError) as exc_info:
            cb.check()
        assert exc_info.value.breaker_id == "token_cost"

    def test_breaker_count(self):
        clock, _ = _make_clock()
        cb = CircuitBreaker(
            configs=[_cost_config(), _error_config(), _actions_config()],
            clock=clock,
        )
        assert cb.breaker_count == 3


class TestStateChangeCallback:
    def test_callback_invoked_on_trip(self):
        clock, _ = _make_clock()
        cb = CircuitBreaker(configs=[_cost_config(threshold=100)], clock=clock)
        calls = []
        cb.on_state_change(lambda bid, old, new, val: calls.append((bid, old, new)))
        cb.record(MetricType.TOKEN_COST, 101)
        assert calls == [("token_cost", "closed", "open")]

    def test_callback_exception_caught(self):
        clock, _ = _make_clock()
        cb = CircuitBreaker(configs=[_cost_config(threshold=100)], clock=clock)
        cb.on_state_change(lambda *a: 1 / 0)
        cb.record(MetricType.TOKEN_COST, 101)


class TestPersistence:
    def test_persist_enqueued_on_trip(self):
        clock, _ = _make_clock()
        pq = deque()
        cb = CircuitBreaker(configs=[_cost_config(threshold=100)], clock=clock, persist_queue=pq)
        cb.record(MetricType.TOKEN_COST, 101)
        assert len(pq) == 1
        assert pq[0][0] == "breaker"

    def test_restore_state_from_storage(self):
        clock, _ = _make_clock()
        storage = MagicMock()
        storage.get_breaker_state.return_value = BreakerState(
            breaker_id="token_cost",
            state="open",
            tripped_at=None,
            last_checked=MagicMock(),
            metrics={"counter": 50.0},
            recovery_mode="auto_retry",
            recovery_after_seconds=60,
        )
        cb = CircuitBreaker(configs=[_cost_config()], clock=clock, storage=storage)
        cb.restore_state()
        with pytest.raises(BreakerTrippedError):
            cb.check()

    def test_restore_state_storage_failure_starts_closed(self):
        clock, _ = _make_clock()
        storage = MagicMock()
        storage.get_breaker_state.side_effect = RuntimeError("db error")
        cb = CircuitBreaker(configs=[_cost_config()], clock=clock, storage=storage)
        cb.restore_state()
        assert cb.check() is None


class TestGetMetrics:
    def test_returns_all_breakers(self):
        clock, _ = _make_clock()
        cb = CircuitBreaker(
            configs=[_cost_config(), _error_config()],
            clock=clock,
        )
        cb.record(MetricType.TOKEN_COST, 50)
        metrics = cb.get_metrics()
        assert len(metrics) == 2
        cost_metric = next(m for m in metrics if m.metric == MetricType.TOKEN_COST)
        assert cost_metric.current_value == 50
        assert cost_metric.threshold == 100

    def test_adaptive_metrics_included(self):
        durations = [10.0] * 20
        tracer = MagicMock()
        tracer.recent_history.return_value = [
            MagicMock(duration_ms=d, tier="allow", metadata={}) for d in durations
        ]
        clock, _ = _make_clock()
        cfg = BreakerConfig(
            metric=MetricType.WALL_CLOCK_MS,
            threshold=1000,
            recovery_mode="auto_retry",
            recovery_after_seconds=60,
        )
        cb = CircuitBreaker(configs=[cfg], clock=clock, tracer=tracer)
        cb.enable_adaptive(sensitivity=2.0, min_samples=10)
        metrics = cb.get_metrics()
        assert metrics[0].adaptive_threshold is not None
        assert metrics[0].sample_count == 20


class TestSetRecoveryMode:
    def test_changes_recovery_mode(self):
        clock, _ = _make_clock()
        cb = CircuitBreaker(configs=[_cost_config()], clock=clock)
        cb.set_recovery_mode("token_cost", "terminate")
        cb.record(MetricType.TOKEN_COST, 101)
        with pytest.raises(AgentTerminatedError):
            cb.check()
