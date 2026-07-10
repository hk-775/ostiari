"""Property-based tests for ostiari.models."""

from datetime import datetime

from hypothesis import assume, given
from hypothesis import strategies as st
from pydantic import ValidationError

from ostiari.models import (
    AnomalySignal,
    RiskSignal,
    ThresholdConfig,
    TraceFilters,
    TraceStats,
)


@given(score=st.integers(min_value=0, max_value=100))
def test_risk_signal_valid_score_always_constructs(score):
    signal = RiskSignal(source="test", score_contribution=score, description="d")
    assert signal.score_contribution == score


@given(score=st.integers().filter(lambda x: x < -100 or x > 100))
def test_risk_signal_invalid_score_always_rejects(score):
    try:
        RiskSignal(source="test", score_contribution=score, description="d")
        assert False, "Should have raised"
    except ValidationError:
        pass


@given(score=st.integers(min_value=0, max_value=100))
def test_anomaly_signal_valid_score_always_constructs(score):
    signal = AnomalySignal(
        detector="test", severity="low", score_contribution=score, description="d"
    )
    assert signal.score_contribution == score


@given(
    allow_max=st.integers(min_value=0, max_value=99),
    intervene_max=st.integers(min_value=1, max_value=100),
)
def test_threshold_config_ordering_invariant(allow_max, intervene_max):
    if allow_max < intervene_max:
        tc = ThresholdConfig(allow_max=allow_max, intervene_max=intervene_max)
        assert tc.allow_max < tc.intervene_max
    else:
        try:
            ThresholdConfig(allow_max=allow_max, intervene_max=intervene_max)
            assert False, "Should have raised for allow_max >= intervene_max"
        except ValidationError:
            pass


@given(
    allowed=st.integers(min_value=0, max_value=100),
    intervened=st.integers(min_value=0, max_value=100),
    blocked=st.integers(min_value=0, max_value=100),
)
def test_trace_stats_count_invariant(allowed, intervened, blocked):
    total = allowed + intervened + blocked
    stats = TraceStats(
        total_actions=total,
        allowed=allowed,
        intervened=intervened,
        blocked=blocked,
        avg_risk_score=50.0,
        total_duration_ms=100.0,
        unique_tools=1,
        period_start=datetime(2024, 1, 1),
        period_end=datetime(2024, 1, 2),
    )
    assert stats.allowed + stats.intervened + stats.blocked == stats.total_actions


@given(
    total=st.integers(min_value=1, max_value=100),
    allowed=st.integers(min_value=0, max_value=100),
    intervened=st.integers(min_value=0, max_value=100),
    blocked=st.integers(min_value=0, max_value=100),
)
def test_trace_stats_mismatch_always_rejected(total, allowed, intervened, blocked):
    assume(allowed + intervened + blocked != total)
    try:
        TraceStats(
            total_actions=total,
            allowed=allowed,
            intervened=intervened,
            blocked=blocked,
            avg_risk_score=50.0,
            total_duration_ms=100.0,
            unique_tools=1,
            period_start=datetime(2024, 1, 1),
            period_end=datetime(2024, 1, 2),
        )
        assert False, "Should have raised"
    except ValidationError:
        pass


@given(
    limit=st.integers(min_value=1, max_value=1000),
    offset=st.integers(min_value=0, max_value=10000),
)
def test_trace_filters_valid_pagination(limit, offset):
    filters = TraceFilters(limit=limit, offset=offset)
    assert filters.limit == limit
    assert filters.offset == offset
