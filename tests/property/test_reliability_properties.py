"""Property-based tests for Unit 5: Reliability."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from ostiari.breaker import CircuitBreaker
from ostiari.checkpoint import CheckpointEngine
from ostiari.models import BreakerConfig, MetricType, RetentionPolicy


def _make_clock(start: float = 0.0):
    time_ref = [start]

    def clock():
        return time_ref[0]

    def advance(seconds: float):
        time_ref[0] += seconds

    return clock, advance


class TestBreakerStateMachine:
    @given(values=st.lists(st.floats(min_value=0.1, max_value=50.0), min_size=1, max_size=20))
    @settings(max_examples=50)
    def test_state_only_valid_values(self, values):
        clock, _ = _make_clock()
        cfg = BreakerConfig(
            metric=MetricType.TOKEN_COST,
            threshold=100,
            recovery_mode="auto_retry",
            recovery_after_seconds=60,
        )
        cb = CircuitBreaker(configs=[cfg], clock=clock)

        for v in values:
            cb.record(MetricType.TOKEN_COST, v)

        breaker = cb._breakers["token_cost"]
        assert breaker.state in ("closed", "open", "half_open")

    @given(threshold=st.floats(min_value=1.0, max_value=1000.0))
    @settings(max_examples=30)
    def test_trip_only_when_threshold_exceeded(self, threshold):
        clock, _ = _make_clock()
        cfg = BreakerConfig(
            metric=MetricType.TOKEN_COST,
            threshold=threshold,
            recovery_mode="auto_retry",
            recovery_after_seconds=60,
        )
        cb = CircuitBreaker(configs=[cfg], clock=clock)
        cb.record(MetricType.TOKEN_COST, threshold - 0.1)
        assert cb.check() is None


class TestCheckpointSequence:
    @given(count=st.integers(min_value=1, max_value=50))
    @settings(max_examples=20)
    def test_sequence_strictly_monotonic(self, count):
        engine = CheckpointEngine()
        for i in range(count):
            engine.create(action=f"tool_{i}", params={})
        checkpoints = engine.list(limit=count)
        sequences = [cp.sequence_number for cp in checkpoints]
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences)


class TestAdaptiveThreshold:
    @given(
        durations=st.lists(
            st.floats(min_value=0.1, max_value=100.0),
            min_size=15,
            max_size=50,
        ),
        sensitivity=st.floats(min_value=0.5, max_value=5.0),
    )
    @settings(max_examples=30)
    def test_adaptive_never_below_floor(self, durations, sensitivity):
        from unittest.mock import MagicMock

        tracer = MagicMock()
        entries = [MagicMock(duration_ms=d, tier="allow", metadata={}) for d in durations]
        tracer.recent_history.return_value = entries

        clock, _ = _make_clock()
        static_threshold = 1000.0
        cfg = BreakerConfig(
            metric=MetricType.WALL_CLOCK_MS,
            threshold=static_threshold,
            recovery_mode="auto_retry",
            recovery_after_seconds=60,
        )
        cb = CircuitBreaker(configs=[cfg], clock=clock, tracer=tracer)
        cb.enable_adaptive(sensitivity=sensitivity, min_samples=10)

        effective = cb._effective_threshold(cb._breakers["wall_clock_ms"])
        floor = static_threshold * 0.5
        assert effective >= floor


class TestRetentionInvariant:
    @given(
        keep_last=st.integers(min_value=1, max_value=10),
        total_creates=st.integers(min_value=1, max_value=30),
    )
    @settings(max_examples=30)
    def test_never_exceeds_keep_last_unnamed(self, keep_last, total_creates):
        engine = CheckpointEngine(retention=RetentionPolicy(keep_last=keep_last))
        for i in range(total_creates):
            engine.create(action=f"tool_{i}", params={})
        checkpoints = engine.list(limit=1000)
        unnamed = [cp for cp in checkpoints if cp.name is None]
        assert len(unnamed) <= keep_last


class TestBreakerIndependence:
    @given(
        cost_values=st.lists(st.floats(min_value=1.0, max_value=200.0), min_size=1, max_size=10),
        action_values=st.lists(st.floats(min_value=1.0, max_value=100.0), min_size=1, max_size=10),
    )
    @settings(max_examples=30)
    def test_breakers_independent_counters(self, cost_values, action_values):
        clock, _ = _make_clock()
        cb = CircuitBreaker(
            configs=[
                BreakerConfig(
                    metric=MetricType.TOKEN_COST, threshold=10000, recovery_after_seconds=60
                ),
                BreakerConfig(
                    metric=MetricType.TOTAL_ACTIONS, threshold=10000, recovery_after_seconds=60
                ),
            ],
            clock=clock,
        )

        for v in cost_values:
            cb.record(MetricType.TOKEN_COST, v)
        for v in action_values:
            cb.record(MetricType.TOTAL_ACTIONS, v)

        metrics = cb.get_metrics()
        cost_m = next(m for m in metrics if m.metric == MetricType.TOKEN_COST)
        action_m = next(m for m in metrics if m.metric == MetricType.TOTAL_ACTIONS)

        expected_cost = sum(cost_values)
        expected_actions = sum(action_values)
        assert abs(cost_m.current_value - expected_cost) < 0.01
        assert abs(action_m.current_value - expected_actions) < 0.01
