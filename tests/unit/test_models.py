"""Unit tests for ostiari.models."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from ostiari.models import (
    AnomalySignal,
    BreakerConfig,
    BreakerState,
    MetricType,
    OstiariConfig,
    RetentionPolicy,
    RiskSignal,
    ThresholdConfig,
    TraceFilters,
    TraceStats,
    ValidationResult,
)


class TestRiskSignal:
    def test_valid_construction(self):
        signal = RiskSignal(source="policy", score_contribution=25, description="High risk tool")
        assert signal.source == "policy"
        assert signal.score_contribution == 25

    def test_score_contribution_bounds(self):
        RiskSignal(source="x", score_contribution=-100, description="min")
        RiskSignal(source="x", score_contribution=100, description="max")
        with pytest.raises(ValidationError):
            RiskSignal(source="x", score_contribution=-101, description="too low")
        with pytest.raises(ValidationError):
            RiskSignal(source="x", score_contribution=101, description="too high")

    def test_empty_source_rejected(self):
        with pytest.raises(ValidationError):
            RiskSignal(source="", score_contribution=0, description="test")

    def test_frozen(self):
        signal = RiskSignal(source="x", score_contribution=0, description="test")
        with pytest.raises(ValidationError):
            signal.source = "y"  # type: ignore[misc]


class TestAnomalySignal:
    def test_valid_construction(self):
        signal = AnomalySignal(
            detector="loop", severity="high", score_contribution=50, description="Loop detected"
        )
        assert signal.detector == "loop"
        assert signal.severity == "high"

    def test_score_contribution_bounds(self):
        AnomalySignal(detector="x", severity="low", score_contribution=0, description="min")
        AnomalySignal(detector="x", severity="low", score_contribution=100, description="max")
        with pytest.raises(ValidationError):
            AnomalySignal(detector="x", severity="low", score_contribution=-1, description="neg")
        with pytest.raises(ValidationError):
            AnomalySignal(detector="x", severity="low", score_contribution=101, description="hi")

    def test_invalid_severity(self):
        with pytest.raises(ValidationError):
            AnomalySignal(detector="x", severity="extreme", score_contribution=0, description="t")


class TestThresholdConfig:
    def test_defaults(self):
        tc = ThresholdConfig()
        assert tc.allow_max == 30
        assert tc.intervene_max == 70

    def test_valid_custom(self):
        tc = ThresholdConfig(allow_max=10, intervene_max=50)
        assert tc.allow_max == 10

    def test_ordering_violated(self):
        with pytest.raises(ValidationError, match="allow_max.*must be less than.*intervene_max"):
            ThresholdConfig(allow_max=70, intervene_max=30)

    def test_equal_values_rejected(self):
        with pytest.raises(ValidationError):
            ThresholdConfig(allow_max=50, intervene_max=50)


class TestValidationResult:
    def test_valid_construction(self):
        result = ValidationResult(
            tier="allow",
            score=15,
            trace_id="abc123",
            action="send_email",
            duration_ms=5.0,
        )
        assert result.tier == "allow"
        assert result.score == 15

    def test_score_range(self):
        with pytest.raises(ValidationError):
            ValidationResult(tier="allow", score=-1, trace_id="x", action="y", duration_ms=0)
        with pytest.raises(ValidationError):
            ValidationResult(tier="allow", score=101, trace_id="x", action="y", duration_ms=0)


class TestTraceFilters:
    def test_defaults(self):
        filters = TraceFilters()
        assert filters.limit == 100
        assert filters.offset == 0

    def test_time_range_valid(self):
        TraceFilters(
            start_time=datetime(2024, 1, 1),
            end_time=datetime(2024, 1, 2),
        )

    def test_time_range_invalid(self):
        with pytest.raises(ValidationError, match="start_time must be"):
            TraceFilters(
                start_time=datetime(2024, 1, 2),
                end_time=datetime(2024, 1, 1),
            )

    def test_limit_bounds(self):
        with pytest.raises(ValidationError):
            TraceFilters(limit=0)
        with pytest.raises(ValidationError):
            TraceFilters(limit=1001)


class TestTraceStats:
    def test_valid_counts(self):
        stats = TraceStats(
            total_actions=10,
            allowed=5,
            intervened=3,
            blocked=2,
            avg_risk_score=45.0,
            total_duration_ms=100.0,
            unique_tools=3,
            period_start=datetime(2024, 1, 1),
            period_end=datetime(2024, 1, 2),
        )
        assert stats.total_actions == 10

    def test_counts_mismatch(self):
        with pytest.raises(ValidationError, match="allowed.*intervened.*blocked.*total"):
            TraceStats(
                total_actions=10,
                allowed=5,
                intervened=3,
                blocked=3,
                avg_risk_score=45.0,
                total_duration_ms=100.0,
                unique_tools=3,
                period_start=datetime(2024, 1, 1),
                period_end=datetime(2024, 1, 2),
            )


class TestBreakerState:
    def test_valid(self):
        state = BreakerState(
            breaker_id="cost",
            state="closed",
            last_checked=datetime(2024, 1, 1),
            recovery_mode="auto_retry",
        )
        assert state.state == "closed"

    def test_recovery_after_seconds_bounds(self):
        with pytest.raises(ValidationError):
            BreakerState(
                breaker_id="x",
                state="closed",
                last_checked=datetime(2024, 1, 1),
                recovery_mode="auto_retry",
                recovery_after_seconds=0,
            )


class TestBreakerConfig:
    def test_valid(self):
        config = BreakerConfig(metric=MetricType.TOKEN_COST, threshold=1000.0)
        assert config.threshold == 1000.0

    def test_threshold_must_be_positive(self):
        with pytest.raises(ValidationError):
            BreakerConfig(metric=MetricType.TOKEN_COST, threshold=0)
        with pytest.raises(ValidationError):
            BreakerConfig(metric=MetricType.TOKEN_COST, threshold=-1)


class TestRetentionPolicy:
    def test_defaults(self):
        rp = RetentionPolicy()
        assert rp.keep_last == 100
        assert rp.keep_named is True

    def test_keep_last_minimum(self):
        with pytest.raises(ValidationError):
            RetentionPolicy(keep_last=0)


class TestOstiariConfig:
    def test_defaults(self):
        config = OstiariConfig()
        assert config.fail_open is True
        assert config.storage_path == "ostiari.db"
        assert config.log_level == "INFO"

    def test_not_frozen(self):
        config = OstiariConfig()
        config.fail_open = False
        assert config.fail_open is False


class TestMetricType:
    def test_values(self):
        assert MetricType.TOKEN_COST.value == "token_cost"
        assert MetricType.WALL_CLOCK_MS.value == "wall_clock_ms"
        assert MetricType.ERROR_COUNT.value == "error_count"
        assert MetricType.CONSECUTIVE_FAILURES.value == "consecutive_failures"
        assert MetricType.TOTAL_ACTIONS.value == "total_actions"
