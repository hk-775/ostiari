"""Unit tests for ostiari.gateway."""

from __future__ import annotations

import time

import pytest

from ostiari.exceptions import ActionInterventionTimeout
from ostiari.gateway import ActionGateway
from ostiari.models import (
    AnomalySignal,
    EvalContext,
    PolicyResult,
    RiskAdjustment,
    RiskSignal,
    Rule,
    ThresholdConfig,
)


def _eval_context() -> EvalContext:
    return EvalContext()


def _policy_result(deltas: list[int] | None = None) -> PolicyResult:
    adjustments = []
    if deltas:
        for d in deltas:
            adjustments.append(
                RiskAdjustment(
                    delta=d,
                    source_rule=Rule(type="risk_adjust", action="*", risk_adjust=d),
                    reason=f"adjustment {d}",
                )
            )
    return PolicyResult(
        decision="evaluate",
        risk_adjustments=adjustments,
    )


def _anomaly(score: int, detector: str = "test") -> AnomalySignal:
    return AnomalySignal(
        detector=detector,
        severity="medium",
        score_contribution=score,
        description=f"anomaly score={score}",
    )


class TestScoreComposition:
    def test_sum_of_signals_capped_at_100(self):
        gw = ActionGateway()
        result = gw.evaluate(
            "action",
            {},
            _eval_context(),
            _policy_result([50]),
            [_anomaly(40), _anomaly(30)],
        )
        assert result.score == 100

    def test_sum_below_cap(self):
        gw = ActionGateway()
        result = gw.evaluate(
            "action",
            {},
            _eval_context(),
            _policy_result([10]),
            [_anomaly(15)],
        )
        assert result.score == 25

    def test_negative_adjustment_floors_at_zero(self):
        gw = ActionGateway()
        result = gw.evaluate(
            "action",
            {},
            _eval_context(),
            _policy_result([-50]),
            [],
        )
        assert result.score == 0

    def test_no_signals_score_zero(self):
        gw = ActionGateway()
        result = gw.evaluate(
            "action",
            {},
            _eval_context(),
            _policy_result(),
            [],
        )
        assert result.score == 0


class TestTierClassification:
    def test_allow_tier(self):
        gw = ActionGateway(thresholds=ThresholdConfig(allow_max=30, intervene_max=70))
        result = gw.evaluate(
            "action",
            {},
            _eval_context(),
            _policy_result([20]),
            [],
        )
        assert result.tier == "allow"

    def test_intervene_tier(self):
        gw = ActionGateway(thresholds=ThresholdConfig(allow_max=30, intervene_max=70))
        result = gw.evaluate(
            "action",
            {},
            _eval_context(),
            _policy_result([50]),
            [],
        )
        assert result.tier == "intervene"

    def test_block_tier(self):
        gw = ActionGateway(thresholds=ThresholdConfig(allow_max=30, intervene_max=70))
        result = gw.evaluate(
            "action",
            {},
            _eval_context(),
            _policy_result([80]),
            [],
        )
        assert result.tier == "block"

    def test_per_tool_threshold_override(self):
        gw = ActionGateway(thresholds=ThresholdConfig(allow_max=30, intervene_max=70))
        pr = PolicyResult(
            decision="evaluate",
            effective_thresholds=ThresholdConfig(allow_max=10, intervene_max=20),
        )
        result = gw.evaluate("action", {}, _eval_context(), pr, [_anomaly(15)])
        assert result.tier == "intervene"


class TestIntervention:
    def test_callback_returns_true_allows(self):
        gw = ActionGateway()
        gw.set_intervention_callback(lambda a, p, s: True)
        tier = gw.handle_intervention_sync("action", {}, 50)
        assert tier == "allow"

    def test_callback_returns_false_blocks(self):
        gw = ActionGateway()
        gw.set_intervention_callback(lambda a, p, s: False)
        tier = gw.handle_intervention_sync("action", {}, 50)
        assert tier == "block"

    def test_callback_timeout_raises(self):
        def slow_callback(a, p, s):
            time.sleep(2)
            return True

        gw = ActionGateway(intervention_timeout=0.1)
        gw.set_intervention_callback(slow_callback)
        with pytest.raises(ActionInterventionTimeout):
            gw.handle_intervention_sync("action", {}, 50)

    def test_callback_exception_uses_fail_open(self):
        def bad_callback(a, p, s):
            raise RuntimeError("callback error")

        gw = ActionGateway(fail_open=True)
        gw.set_intervention_callback(bad_callback)
        tier = gw.handle_intervention_sync("action", {}, 50)
        assert tier == "allow"

    def test_no_callback_fail_open_allows(self):
        gw = ActionGateway(fail_open=True)
        tier = gw.handle_intervention_sync("action", {}, 50)
        assert tier == "allow"

    def test_no_callback_fail_closed_blocks(self):
        gw = ActionGateway(fail_open=False)
        tier = gw.handle_intervention_sync("action", {}, 50)
        assert tier == "block"


class TestInterventionAsync:
    def test_async_callback_approve(self):
        import asyncio

        async def async_cb(action, params, score):
            return True

        gw = ActionGateway()
        gw.set_intervention_callback(async_cb)
        tier = asyncio.run(gw.handle_intervention_async("action", {}, 50))
        assert tier == "allow"

    def test_async_callback_deny(self):
        import asyncio

        async def async_cb(action, params, score):
            return False

        gw = ActionGateway()
        gw.set_intervention_callback(async_cb)
        tier = asyncio.run(gw.handle_intervention_async("action", {}, 50))
        assert tier == "block"

    def test_async_callback_timeout(self):
        import asyncio

        async def slow_cb(action, params, score):
            await asyncio.sleep(5)
            return True

        gw = ActionGateway(intervention_timeout=0.1)
        gw.set_intervention_callback(slow_cb)
        with pytest.raises(ActionInterventionTimeout):
            asyncio.run(gw.handle_intervention_async("action", {}, 50))

    def test_async_with_sync_callback(self):
        import asyncio

        def sync_cb(action, params, score):
            return True

        gw = ActionGateway()
        gw.set_intervention_callback(sync_cb)
        tier = asyncio.run(gw.handle_intervention_async("action", {}, 50))
        assert tier == "allow"

    def test_async_no_callback_fail_open(self):
        import asyncio

        gw = ActionGateway(fail_open=True)
        tier = asyncio.run(gw.handle_intervention_async("action", {}, 50))
        assert tier == "allow"

    def test_async_callback_exception_fail_open(self):
        import asyncio

        async def bad_cb(action, params, score):
            raise RuntimeError("fail")

        gw = ActionGateway(fail_open=True)
        gw.set_intervention_callback(bad_cb)
        tier = asyncio.run(gw.handle_intervention_async("action", {}, 50))
        assert tier == "allow"

    def test_sync_path_with_async_callback(self):

        async def async_cb(action, params, score):
            return True

        gw = ActionGateway()
        gw.set_intervention_callback(async_cb)
        tier = gw.handle_intervention_sync("action", {}, 50)
        assert tier == "allow"


class TestSignalProviders:
    def test_custom_provider_contributes_score(self):
        class MyProvider:
            @property
            def name(self) -> str:
                return "custom"

            def evaluate(self, action, params, context) -> RiskSignal:
                return RiskSignal(
                    source="custom", score_contribution=25, description="custom signal"
                )

        gw = ActionGateway()
        gw.add_signal_provider(MyProvider())
        result = gw.evaluate("action", {}, _eval_context(), _policy_result(), [])
        assert result.score == 25

    def test_provider_exception_omits_signal(self):
        class BadProvider:
            @property
            def name(self) -> str:
                return "bad"

            def evaluate(self, action, params, context):
                raise RuntimeError("provider crash")

        gw = ActionGateway()
        gw.add_signal_provider(BadProvider())
        result = gw.evaluate(
            "action",
            {},
            _eval_context(),
            _policy_result([10]),
            [],
        )
        assert result.score == 10

    def test_add_signal_provider_copy_on_write(self):
        class P:
            @property
            def name(self) -> str:
                return "p"

            def evaluate(self, action, params, context):
                return None

        gw = ActionGateway()
        old_list = gw._signal_providers
        gw.add_signal_provider(P())
        assert gw._signal_providers is not old_list
        assert len(gw._signal_providers) == 1

    def test_set_thresholds_atomic(self):
        gw = ActionGateway()
        old_thresh = gw._thresholds
        gw.set_thresholds(10, 50)
        assert gw._thresholds is not old_thresh
        assert gw._thresholds.allow_max == 10
        assert gw._thresholds.intervene_max == 50
