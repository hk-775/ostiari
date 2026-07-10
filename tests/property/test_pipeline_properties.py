"""Property-based tests for the action pipeline."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from ostiari.gateway import ActionGateway
from ostiari.models import (
    AnomalySignal,
    EvalContext,
    PolicyResult,
    RiskAdjustment,
    Rule,
    ThresholdConfig,
)


def _anomaly(score: int) -> AnomalySignal:
    return AnomalySignal(
        detector="test",
        severity="medium",
        score_contribution=score,
        description="test",
    )


def _policy_result(deltas: list[int]) -> PolicyResult:
    adjustments = [
        RiskAdjustment(
            delta=d,
            source_rule=Rule(type="risk_adjust", action="*", risk_adjust=d),
            reason="adj",
        )
        for d in deltas
        if d != 0
    ]
    return PolicyResult(decision="evaluate", risk_adjustments=adjustments)


@given(
    deltas=st.lists(st.integers(min_value=-100, max_value=100), min_size=0, max_size=5),
    anomaly_scores=st.lists(st.integers(min_value=0, max_value=100), min_size=0, max_size=5),
)
@settings(max_examples=200)
def test_score_bounded(deltas, anomaly_scores):
    gw = ActionGateway()
    anomalies = [_anomaly(s) for s in anomaly_scores]
    result = gw.evaluate("action", {}, EvalContext(), _policy_result(deltas), anomalies)
    assert 0 <= result.score <= 100


@given(
    score=st.integers(min_value=0, max_value=100),
    allow_max=st.integers(min_value=1, max_value=49),
)
@settings(max_examples=200)
def test_tier_monotonicity(score, allow_max):
    intervene_max = allow_max + 25
    if intervene_max > 99:
        intervene_max = 99
    if allow_max >= intervene_max:
        return

    gw = ActionGateway(thresholds=ThresholdConfig(allow_max=allow_max, intervene_max=intervene_max))
    tier = gw._classify(score, gw._thresholds)

    tier_rank = {"allow": 0, "intervene": 1, "block": 2}
    if score <= allow_max:
        assert tier_rank[tier] == 0
    elif score <= intervene_max:
        assert tier_rank[tier] == 1
    else:
        assert tier_rank[tier] == 2


@given(
    deltas=st.lists(st.integers(min_value=-100, max_value=100), min_size=0, max_size=5),
    anomaly_scores=st.lists(st.integers(min_value=0, max_value=100), min_size=0, max_size=5),
)
@settings(max_examples=200)
def test_score_deterministic(deltas, anomaly_scores):
    gw = ActionGateway()
    anomalies = [_anomaly(s) for s in anomaly_scores]
    pr = _policy_result(deltas)
    ctx = EvalContext()

    r1 = gw.evaluate("action", {}, ctx, pr, anomalies)
    r2 = gw.evaluate("action", {}, ctx, pr, anomalies)
    assert r1.score == r2.score
    assert r1.tier == r2.tier


@given(
    scores_a=st.lists(st.integers(min_value=0, max_value=50), min_size=1, max_size=4),
    scores_b=st.lists(st.integers(min_value=0, max_value=50), min_size=1, max_size=4),
)
@settings(max_examples=200)
def test_more_signals_higher_or_equal_score(scores_a, scores_b):
    gw = ActionGateway()
    ctx = EvalContext()
    pr = _policy_result([])

    anomalies_a = [_anomaly(s) for s in scores_a]
    anomalies_combined = [_anomaly(s) for s in scores_a + scores_b]

    r_a = gw.evaluate("action", {}, ctx, pr, anomalies_a)
    r_combined = gw.evaluate("action", {}, ctx, pr, anomalies_combined)
    assert r_combined.score >= r_a.score


@given(
    scores=st.lists(st.integers(min_value=0, max_value=50), min_size=2, max_size=5),
)
@settings(max_examples=200)
def test_signal_order_does_not_affect_score(scores):
    gw = ActionGateway()
    ctx = EvalContext()
    pr = _policy_result([])

    anomalies_forward = [_anomaly(s) for s in scores]
    anomalies_reversed = [_anomaly(s) for s in reversed(scores)]

    r1 = gw.evaluate("action", {}, ctx, pr, anomalies_forward)
    r2 = gw.evaluate("action", {}, ctx, pr, anomalies_reversed)
    assert r1.score == r2.score


@given(
    delta=st.integers(min_value=1, max_value=100),
)
@settings(max_examples=100)
def test_partial_eval_fail_open_produces_result(delta):
    gw = ActionGateway(fail_open=True)
    # Even with no policy result (simulating failure), should still work
    anomalies = [_anomaly(delta)]
    result = gw.evaluate("action", {}, EvalContext(), None, anomalies)
    assert 0 <= result.score <= 100
    assert result.tier in ("allow", "intervene", "block")
