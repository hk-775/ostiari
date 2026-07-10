"""Integration tests for Unit 5: Reliability."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ostiari.exceptions import BreakerTrippedError
from ostiari.guard import Guard
from ostiari.models import (
    BreakerConfig,
    BreakerState,
    MetricType,
    RetentionPolicy,
)


@pytest.fixture
def mock_storage():
    storage = MagicMock()
    storage.get_traces.return_value = []
    storage.save_traces_batch.return_value = None
    storage.get_breaker_state.return_value = None
    storage.save_breaker_state.return_value = None
    storage.save_checkpoint.return_value = None
    storage.delete_checkpoints.return_value = None
    return storage


class TestBreakerTrippingMidSession:
    def test_breaker_trips_after_accumulated_cost(self, mock_storage):
        configs = [
            BreakerConfig(metric=MetricType.TOKEN_COST, threshold=500, recovery_after_seconds=1)
        ]
        g = Guard(storage=mock_storage, breaker_configs=configs)

        for i in range(4):
            g.validate("tool", {}, context={"token_cost": 100})

        g.validate("tool", {}, context={"token_cost": 100})

        with pytest.raises(BreakerTrippedError):
            g.validate("tool", {}, context={"token_cost": 100})

    def test_breaker_recovery_after_trip(self, mock_storage):
        fake_time = [0.0]
        configs = [
            BreakerConfig(metric=MetricType.TOTAL_ACTIONS, threshold=3, recovery_after_seconds=1)
        ]
        g = Guard(storage=mock_storage, breaker_configs=configs)
        g._breaker._clock = lambda: fake_time[0]
        for b in g._breaker._breakers.values():
            b._clock = lambda: fake_time[0]

        g.validate("tool", {})
        g.validate("tool", {})
        g.validate("tool", {})

        with pytest.raises(BreakerTrippedError):
            g.validate("tool", {})

        fake_time[0] = 2.0
        result = g.validate("tool", {})
        assert result.tier == "allow"


class TestCheckpointRollbackEndToEnd:
    def test_create_and_rollback(self, mock_storage):
        g = Guard(storage=mock_storage)
        g.validate("step_1", {"data": "a"})
        cp_id = g.checkpoint("before_danger")
        g.validate("step_2", {"data": "b"})
        g.validate("step_3", {"data": "c"})

        state = g.rollback("before_danger")
        assert state.checkpoint.name == "before_danger"
        assert state.restored_at is not None

    def test_auto_checkpoints_accumulate(self, mock_storage):
        g = Guard(storage=mock_storage)
        for i in range(5):
            g.validate(f"tool_{i}", {})
        checkpoints = g._checkpoint_engine.list(limit=100)
        assert len(checkpoints) == 5

    def test_retention_limits_checkpoints(self, mock_storage):
        retention = RetentionPolicy(keep_last=3)
        g = Guard(storage=mock_storage, retention_policy=retention)
        for i in range(10):
            g.validate(f"tool_{i}", {})
        checkpoints = g._checkpoint_engine.list(limit=100)
        unnamed = [cp for cp in checkpoints if cp.name is None]
        assert len(unnamed) <= 3


class TestBreakerPersistence:
    def test_breaker_state_restored_on_start(self, mock_storage):
        mock_storage.get_breaker_state.return_value = BreakerState(
            breaker_id="token_cost",
            state="open",
            tripped_at=None,
            last_checked=MagicMock(),
            metrics={"counter": 200.0},
            recovery_mode="auto_retry",
            recovery_after_seconds=60,
        )
        configs = [
            BreakerConfig(metric=MetricType.TOKEN_COST, threshold=100, recovery_after_seconds=60)
        ]
        g = Guard(storage=mock_storage, breaker_configs=configs)
        g.start()

        with pytest.raises(BreakerTrippedError):
            g.validate("tool", {})

        g.shutdown()

    def test_persist_queue_drains_on_shutdown(self, mock_storage):
        configs = [
            BreakerConfig(metric=MetricType.TOKEN_COST, threshold=100, recovery_after_seconds=60)
        ]
        g = Guard(storage=mock_storage, breaker_configs=configs)
        g.start()
        g._breaker.record(MetricType.TOKEN_COST, 101)
        g.shutdown()

        assert mock_storage.save_breaker_state.called


class TestAdaptiveConvergence:
    def test_adaptive_with_trace_history(self, mock_storage):
        configs = [
            BreakerConfig(
                metric=MetricType.WALL_CLOCK_MS, threshold=10000, recovery_after_seconds=60
            )
        ]
        g = Guard(storage=mock_storage, breaker_configs=configs)
        g._breaker.enable_adaptive(sensitivity=2.0, min_samples=5)

        for i in range(10):
            g.validate(f"tool_{i}", {})

        metrics = g._breaker.get_metrics()
        wc_metric = next(m for m in metrics if m.metric == MetricType.WALL_CLOCK_MS)
        assert wc_metric.sample_count > 0
