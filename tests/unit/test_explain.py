"""Unit tests for decision explainability."""

from __future__ import annotations

from ostiari import Guard, explain
from ostiari.explain import DecisionExplanation
from ostiari.models import RiskSignal, ValidationResult


def _result(tier="allow", score=0, signals=None):
    return ValidationResult(
        tier=tier, original_tier=tier, score=score, signals=signals or [],
        trace_id="t", action="db_delete", params={}, duration_ms=1.0,
    )


class TestExplain:
    def test_no_signals_baseline(self):
        e = explain(_result(score=0))
        assert isinstance(e, DecisionExplanation)
        assert e.factors == []
        assert "baseline" in e.summary.lower()

    def test_factors_from_signals(self):
        r = _result(tier="intervene", score=60, signals=[
            RiskSignal(source="parameter-risk", score_contribution=45,
                       description="unbounded operation"),
            RiskSignal(source="policy", score_contribution=15, description="send_email +15"),
        ])
        e = explain(r)
        assert len(e.factors) == 2
        # ordered biggest-driver first
        assert e.factors[0].source == "parameter-risk"
        assert e.factors[0].points == 45

    def test_summary_leads_with_top_driver(self):
        r = _result(tier="block", score=95, signals=[
            RiskSignal(source="parameter-risk", score_contribution=80,
                       description="unbounded delete on production"),
        ])
        e = explain(r)
        assert "blocked" in e.summary.lower()
        assert "unbounded delete on production" in e.summary

    def test_intervene_summary_wording(self):
        r = _result(tier="intervene", score=50, signals=[
            RiskSignal(source="parameter-risk", score_contribution=50, description="external recipient"),
        ])
        e = explain(r)
        assert "human approval" in e.summary.lower()

    def test_to_dict_shape(self):
        r = _result(tier="allow", score=20, signals=[
            RiskSignal(source="parameter-risk", score_contribution=20,
                       description="wildcard", metadata={"reasons": ["wildcard"]}),
        ])
        d = explain(r).to_dict()
        assert d["tier"] == "allow" and d["score"] == 20
        assert d["factors"][0]["source"] == "parameter-risk"
        assert d["factors"][0]["detail"]["reasons"] == ["wildcard"]

    def test_uses_original_tier_when_present(self):
        # tier collapsed to allow (fail_open) but original_tier says intervene
        r = ValidationResult(
            tier="allow", original_tier="intervene", score=50, signals=[],
            trace_id="t", action="x", params={}, duration_ms=1.0,
        )
        assert explain(r).tier == "intervene"


class TestExplainRealGuard:
    def test_real_decision_is_explainable(self):
        g = Guard(); g.start()
        r = g.validate(action="db_delete",
                       params={"sql": "DELETE FROM users WHERE 1=1"},
                       context={"agent_id": "a"})
        e = explain(r)
        # the parameter-risk signal should be a named factor with a reason
        assert any(f.source == "parameter-risk" for f in e.factors)
        assert e.summary
