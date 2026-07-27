"""Unit tests for ostiari.guard."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from ostiari.exceptions import ActionBlockedError, BreakerTrippedError, OstiariError
from ostiari.guard import Guard
from ostiari.models import (
    BreakerConfig,
    BreakerState,
    MetricType,
    OstiariConfig,
    PolicyResult,
    RiskAdjustment,
    Rule,
    ThresholdConfig,
    ValidationResult,
)


@pytest.fixture
def mock_storage():
    storage = MagicMock()
    storage.get_traces.return_value = []
    storage.save_traces_batch.return_value = None
    return storage


@pytest.fixture
def guard(mock_storage):
    g = Guard(storage=mock_storage)
    return g


class TestValidateAllow:
    def test_returns_validation_result(self, guard):
        result = guard.validate("read_file", {"path": "/tmp/x"})
        assert isinstance(result, ValidationResult)
        assert result.tier == "allow"
        assert result.action == "read_file"

    def test_score_zero_with_no_policies(self, guard):
        result = guard.validate("action", {})
        assert result.score == 0

    def test_trace_recorded(self, guard):
        guard.validate("action", {})
        assert guard.tracer.queue_depth == 1


class TestValidateBlock:
    def test_policy_block_raises_action_blocked(self, mock_storage):
        from ostiari.policy import PolicyEngine

        engine = MagicMock(spec=PolicyEngine)
        block_rule = Rule(type="block", action="dangerous")
        engine.evaluate.return_value = PolicyResult(
            decision="block",
            blocked_by=block_rule,
        )
        g = Guard(policy_engine=engine, storage=mock_storage)
        with pytest.raises(ActionBlockedError) as exc_info:
            g.validate("dangerous", {})
        assert exc_info.value.action == "dangerous"
        assert exc_info.value.score == 100

    def test_score_exceeds_threshold_blocks(self, mock_storage):
        from ostiari.policy import PolicyEngine

        engine = MagicMock(spec=PolicyEngine)
        engine.evaluate.return_value = PolicyResult(
            decision="evaluate",
            risk_adjustments=[
                RiskAdjustment(
                    delta=80,
                    source_rule=Rule(type="risk_adjust", action="*", risk_adjust=80),
                    reason="high risk",
                )
            ],
        )
        config = OstiariConfig(thresholds=ThresholdConfig(allow_max=30, intervene_max=70))
        g = Guard(config=config, policy_engine=engine, storage=mock_storage)
        with pytest.raises(ActionBlockedError):
            g.validate("action", {})


class TestPartialEvaluation:
    def test_policy_failure_still_evaluates(self, mock_storage):
        from ostiari.policy import PolicyEngine

        engine = MagicMock(spec=PolicyEngine)
        engine.evaluate.side_effect = RuntimeError("policy crash")
        g = Guard(policy_engine=engine, storage=mock_storage)
        result = g.validate("action", {})
        assert result.tier == "allow"
        assert result.score == 0

    def test_anomaly_failure_still_evaluates(self, mock_storage):
        from ostiari.anomaly import AnomalyDetector

        detector = MagicMock(spec=AnomalyDetector)
        detector.analyze.side_effect = RuntimeError("anomaly crash")
        g = Guard(anomaly_detector=detector, storage=mock_storage)
        result = g.validate("action", {})
        assert result.tier == "allow"

    def test_fail_closed_raises_on_gateway_failure(self, mock_storage):
        config = OstiariConfig(fail_open=False)
        g = Guard(config=config, storage=mock_storage)
        with patch.object(g._gateway, "evaluate", side_effect=RuntimeError("gw crash")):
            with pytest.raises(OstiariError):
                g.validate("action", {})

    def test_fail_open_allows_on_gateway_failure(self, mock_storage):
        g = Guard(storage=mock_storage)
        with patch.object(g._gateway, "evaluate", side_effect=RuntimeError("gw crash")):
            result = g.validate("action", {})
        assert result.tier == "allow"
        assert result.score == 0


class TestIntervention:
    def test_intervention_invoked_when_intervene(self, mock_storage):
        from ostiari.policy import PolicyEngine

        engine = MagicMock(spec=PolicyEngine)
        engine.evaluate.return_value = PolicyResult(
            decision="evaluate",
            risk_adjustments=[
                RiskAdjustment(
                    delta=50,
                    source_rule=Rule(type="risk_adjust", action="*", risk_adjust=50),
                    reason="medium risk",
                )
            ],
        )
        config = OstiariConfig(thresholds=ThresholdConfig(allow_max=30, intervene_max=70))
        g = Guard(config=config, policy_engine=engine, storage=mock_storage)
        g.gateway.set_intervention_callback(lambda a, p, s: True)
        result = g.validate("action", {})
        assert result.tier == "allow"
        assert result.score == 50


class TestAsync:
    def test_avalidate_returns_same_result(self, mock_storage):
        g = Guard(storage=mock_storage)
        result = asyncio.run(g.avalidate("action", {}))
        assert isinstance(result, ValidationResult)
        assert result.tier == "allow"


class TestLifecycle:
    def test_validate_after_shutdown_raises(self, mock_storage):
        g = Guard(storage=mock_storage)
        g.shutdown()
        with pytest.raises(OstiariError, match="shut down"):
            g.validate("action", {})

    def test_validate_in_created_state_works(self, mock_storage):
        g = Guard(storage=mock_storage)
        assert g.state == "created"
        result = g.validate("action", {})
        assert result.tier == "allow"

    def test_context_manager(self, mock_storage):
        with Guard(storage=mock_storage) as g:
            assert g.state == "started"
            g.validate("action", {})
        assert g.state == "shutdown"

    def test_start_idempotent(self, mock_storage):
        g = Guard(storage=mock_storage)
        g.start()
        g.start()
        assert g.state == "started"

    def test_shutdown_idempotent(self, mock_storage):
        g = Guard(storage=mock_storage)
        g.start()
        g.shutdown()
        g.shutdown()
        assert g.state == "shutdown"

    def test_start_after_shutdown_raises(self, mock_storage):
        g = Guard(storage=mock_storage)
        g.start()
        g.shutdown()
        with pytest.raises(OstiariError, match="Cannot restart"):
            g.start()


class TestDelegation:
    def test_register_detector_delegates(self, mock_storage):
        g = Guard(storage=mock_storage)
        initial_count = g._anomaly_detector.detector_count

        class FakeDetector:
            @property
            def name(self) -> str:
                return "fake"

            def detect(self, action, params, history):
                return None

        g.register_detector(FakeDetector())
        assert g._anomaly_detector.detector_count == initial_count + 1

    def test_checkpoint_creates_named(self, mock_storage):
        g = Guard(storage=mock_storage)
        cp_id = g.checkpoint("test_point")
        assert isinstance(cp_id, str)

    def test_rollback_not_found_raises(self, mock_storage):
        g = Guard(storage=mock_storage)
        from ostiari.exceptions import CheckpointNotFoundError

        with pytest.raises(CheckpointNotFoundError):
            g.rollback("nonexistent")

    def test_configure_with_dict(self, mock_storage):
        g = Guard(storage=mock_storage)
        g.configure({"log_level": "DEBUG"})
        assert g._config.log_level == "DEBUG"


class TestBreakerIntegration:
    def test_breaker_trips_blocks_validate(self, mock_storage):
        from ostiari.exceptions import BreakerTrippedError

        configs = [
            BreakerConfig(metric=MetricType.TOKEN_COST, threshold=100, recovery_after_seconds=60)
        ]
        g = Guard(storage=mock_storage, breaker_configs=configs)
        g._breaker.record(MetricType.TOKEN_COST, 101)
        with pytest.raises(BreakerTrippedError):
            g.validate("action", {})

    def test_validate_records_breaker_metrics(self, mock_storage):

        configs = [
            BreakerConfig(
                metric=MetricType.TOTAL_ACTIONS, threshold=1000, recovery_after_seconds=60
            )
        ]
        g = Guard(storage=mock_storage, breaker_configs=configs)
        g.validate("action", {})
        metrics = g._breaker.get_metrics()
        actions_metric = next(m for m in metrics if m.metric == MetricType.TOTAL_ACTIONS)
        assert actions_metric.current_value == 1.0

    def test_validate_records_token_cost_from_context(self, mock_storage):

        configs = [
            BreakerConfig(metric=MetricType.TOKEN_COST, threshold=10000, recovery_after_seconds=60)
        ]
        g = Guard(storage=mock_storage, breaker_configs=configs)
        g.validate("action", {}, context={"token_cost": 500})
        metrics = g._breaker.get_metrics()
        cost_metric = next(m for m in metrics if m.metric == MetricType.TOKEN_COST)
        assert cost_metric.current_value == 500.0

    def test_probe_result_reported_on_success(self, mock_storage):

        fake_time = [0.0]
        configs = [
            BreakerConfig(metric=MetricType.TOKEN_COST, threshold=100, recovery_after_seconds=1)
        ]
        g = Guard(storage=mock_storage, breaker_configs=configs)
        g._breaker._clock = lambda: fake_time[0]
        for b in g._breaker._breakers.values():
            b._clock = lambda: fake_time[0]

        g._breaker.record(MetricType.TOKEN_COST, 101)
        fake_time[0] = 2.0
        result = g.validate("action", {})
        assert result.tier == "allow"
        assert g._breaker.check() is None

    def test_probe_result_reported_on_block(self, mock_storage):
        from unittest.mock import MagicMock as MM

        from ostiari.exceptions import ActionBlockedError
        from ostiari.policy import PolicyEngine

        fake_time = [0.0]
        breaker_configs = [
            BreakerConfig(metric=MetricType.TOKEN_COST, threshold=100, recovery_after_seconds=1)
        ]

        engine = MM(spec=PolicyEngine)
        engine.evaluate.return_value = PolicyResult(
            decision="block", blocked_by=Rule(type="block", action="danger")
        )

        g = Guard(storage=mock_storage, breaker_configs=breaker_configs, policy_engine=engine)
        g._breaker._clock = lambda: fake_time[0]
        for b in g._breaker._breakers.values():
            b._clock = lambda: fake_time[0]

        g._breaker.record(MetricType.TOKEN_COST, 101)
        fake_time[0] = 2.0

        with pytest.raises(ActionBlockedError):
            g.validate("danger", {})

        with pytest.raises(BreakerTrippedError):
            g._breaker.check()

    def test_report_outcome_delegates(self, mock_storage):
        from ostiari.exceptions import BreakerTrippedError

        configs = [
            BreakerConfig(
                metric=MetricType.CONSECUTIVE_FAILURES, threshold=2, recovery_after_seconds=60
            )
        ]
        g = Guard(storage=mock_storage, breaker_configs=configs)
        g.report_outcome("tool", success=False)
        g.report_outcome("tool", success=False)
        with pytest.raises(BreakerTrippedError):
            g.validate("action", {})

    def test_configure_breakers_creates_breaker(self, mock_storage):

        g = Guard(storage=mock_storage)
        assert g._breaker is None
        configs = [
            BreakerConfig(metric=MetricType.TOKEN_COST, threshold=100, recovery_after_seconds=60)
        ]
        g.configure_breakers(configs)
        assert g._breaker is not None
        assert g._breaker.breaker_count == 1


class TestAutoCheckpoint:
    def test_auto_checkpoint_on_allow(self, mock_storage):
        g = Guard(storage=mock_storage)
        g.validate("action", {"key": "val"})
        checkpoints = g._checkpoint_engine.list()
        assert len(checkpoints) == 1
        assert checkpoints[0].action == "action"

    def test_no_checkpoint_on_block(self, mock_storage):
        from ostiari.exceptions import ActionBlockedError
        from ostiari.policy import PolicyEngine

        engine = MagicMock(spec=PolicyEngine)
        engine.evaluate.return_value = PolicyResult(
            decision="block", blocked_by=Rule(type="block", action="bad")
        )
        g = Guard(storage=mock_storage, policy_engine=engine)
        with pytest.raises(ActionBlockedError):
            g.validate("bad", {})
        checkpoints = g._checkpoint_engine.list()
        assert len(checkpoints) == 0

    def test_auto_checkpoint_disabled(self, mock_storage):
        g = Guard(storage=mock_storage)
        g._checkpoint_engine.auto_enabled = False
        g.validate("action", {})
        checkpoints = g._checkpoint_engine.list()
        assert len(checkpoints) == 0

    def test_start_restores_breaker_state(self, mock_storage):
        from ostiari.exceptions import BreakerTrippedError

        mock_storage.get_breaker_state.return_value = BreakerState(
            breaker_id="token_cost",
            state="open",
            tripped_at=None,
            last_checked=MagicMock(),
            metrics={"counter": 50.0},
            recovery_mode="auto_retry",
            recovery_after_seconds=60,
        )
        configs = [
            BreakerConfig(metric=MetricType.TOKEN_COST, threshold=100, recovery_after_seconds=60)
        ]
        g = Guard(storage=mock_storage, breaker_configs=configs)
        g.start()
        with pytest.raises(BreakerTrippedError):
            g.validate("action", {})
