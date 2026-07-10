"""Unit tests for ostiari.policy.rules."""

from datetime import datetime, timedelta, timezone

from ostiari.models import (
    ContextCondition,
    EvalContext,
    Rule,
    ThresholdConfig,
    ThresholdOverrides,
    TraceEntry,
)
from ostiari.policy.rules import (
    compute_risk_adjustments,
    evaluate_context_condition,
    match_rules,
    resolve_thresholds,
)


def _rule(
    type="risk_adjust",
    action="*",
    priority=0,
    enabled=True,
    risk_adjust=10,
    context=None,
    threshold_override=None,
):
    kwargs = {"type": type, "action": action, "priority": priority, "enabled": enabled}
    if type == "risk_adjust":
        kwargs["risk_adjust"] = risk_adjust
    if type == "context_rule":
        kwargs["context"] = context
    if type == "threshold_override":
        kwargs["threshold_override"] = threshold_override
    return Rule(**kwargs)


def _trace(action="tool_a", timestamp=None):
    return TraceEntry(
        trace_id="t1",
        timestamp=timestamp or datetime.now(timezone.utc),
        action=action,
        risk_score=0,
        tier="allow",
        duration_ms=1.0,
    )


class TestMatchRules:
    def test_exact_match(self):
        rules = [_rule(action="send_email")]
        assert len(match_rules("send_email", rules)) == 1

    def test_glob_star(self):
        rules = [_rule(action="send_*")]
        assert len(match_rules("send_email", rules)) == 1
        assert len(match_rules("read_file", rules)) == 0

    def test_glob_question(self):
        rules = [_rule(action="tool_?")]
        assert len(match_rules("tool_a", rules)) == 1
        assert len(match_rules("tool_ab", rules)) == 0

    def test_wildcard_all(self):
        rules = [_rule(action="*")]
        assert len(match_rules("anything", rules)) == 1

    def test_disabled_rule_skipped(self):
        rules = [_rule(action="*", enabled=False)]
        assert len(match_rules("anything", rules)) == 0

    def test_multiple_matches(self):
        rules = [_rule(action="send_*"), _rule(action="*")]
        assert len(match_rules("send_email", rules)) == 2

    def test_no_match(self):
        rules = [_rule(action="send_*")]
        assert len(match_rules("read_file", rules)) == 0


class TestContextRepetition:
    def test_condition_met(self):
        now = datetime.now(timezone.utc)
        condition = ContextCondition(type="repetition", count=3, window_seconds=60, risk_adjust=20)
        context = EvalContext(
            history=[
                _trace(action="send_email", timestamp=now - timedelta(seconds=i)) for i in range(3)
            ],
            current_time=now,
        )
        assert evaluate_context_condition(condition, "send_email", {}, context) is True

    def test_condition_not_met(self):
        now = datetime.now(timezone.utc)
        condition = ContextCondition(type="repetition", count=5, window_seconds=60, risk_adjust=20)
        context = EvalContext(
            history=[_trace(action="send_email", timestamp=now - timedelta(seconds=10))],
            current_time=now,
        )
        assert evaluate_context_condition(condition, "send_email", {}, context) is False

    def test_outside_window_not_counted(self):
        now = datetime.now(timezone.utc)
        condition = ContextCondition(type="repetition", count=2, window_seconds=30, risk_adjust=20)
        context = EvalContext(
            history=[
                _trace(action="send_email", timestamp=now - timedelta(seconds=60)),
                _trace(action="send_email", timestamp=now - timedelta(seconds=50)),
            ],
            current_time=now,
        )
        assert evaluate_context_condition(condition, "send_email", {}, context) is False


class TestContextEscalation:
    def test_preceding_action_found(self):
        now = datetime.now(timezone.utc)
        condition = ContextCondition(type="escalation", preceding_action="read_*", risk_adjust=20)
        context = EvalContext(
            history=[_trace(action="read_file", timestamp=now - timedelta(seconds=5))],
            current_time=now,
        )
        assert (
            evaluate_context_condition(condition, "delete_file", {"path": "/data"}, context) is True
        )

    def test_preceding_action_not_found(self):
        now = datetime.now(timezone.utc)
        condition = ContextCondition(type="escalation", preceding_action="read_*", risk_adjust=20)
        context = EvalContext(
            history=[_trace(action="list_files", timestamp=now - timedelta(seconds=5))],
            current_time=now,
        )
        assert evaluate_context_condition(condition, "delete_file", {}, context) is False

    def test_with_resource_match(self):
        now = datetime.now(timezone.utc)
        condition = ContextCondition(
            type="escalation",
            preceding_action="read_*",
            preceding_resource="/data/*",
            risk_adjust=20,
        )
        history_entry = TraceEntry(
            trace_id="t1",
            timestamp=now - timedelta(seconds=5),
            action="read_file",
            params={"path": "/data/users.db"},
            risk_score=0,
            tier="allow",
            duration_ms=1.0,
        )
        context = EvalContext(history=[history_entry], current_time=now)
        assert (
            evaluate_context_condition(
                condition, "delete_file", {"path": "/data/users.db"}, context
            )
            is True
        )


class TestContextTimeOfDay:
    def test_outside_business_hours(self):
        condition = ContextCondition(type="time_of_day", outside_hours=(9, 17), risk_adjust=30)
        at_2am = datetime(2026, 5, 9, 2, 0, 0, tzinfo=timezone.utc)
        context = EvalContext(current_time=at_2am)
        assert evaluate_context_condition(condition, "write_file", {}, context) is True

    def test_inside_business_hours(self):
        condition = ContextCondition(type="time_of_day", outside_hours=(9, 17), risk_adjust=30)
        at_noon = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
        context = EvalContext(current_time=at_noon)
        assert evaluate_context_condition(condition, "write_file", {}, context) is False

    def test_wrapping_midnight(self):
        condition = ContextCondition(type="time_of_day", outside_hours=(22, 6), risk_adjust=30)
        at_3am = datetime(2026, 5, 9, 3, 0, 0, tzinfo=timezone.utc)
        context = EvalContext(current_time=at_3am)
        assert evaluate_context_condition(condition, "write_file", {}, context) is False

    def test_wrapping_midnight_outside(self):
        condition = ContextCondition(type="time_of_day", outside_hours=(22, 6), risk_adjust=30)
        at_10am = datetime(2026, 5, 9, 10, 0, 0, tzinfo=timezone.utc)
        context = EvalContext(current_time=at_10am)
        assert evaluate_context_condition(condition, "write_file", {}, context) is True


class TestComputeRiskAdjustments:
    def test_static_rule(self):
        rules = [_rule(type="risk_adjust", action="*", risk_adjust=15)]
        adjustments = compute_risk_adjustments(rules, "anything", {}, EvalContext())
        assert len(adjustments) == 1
        assert adjustments[0].delta == 15

    def test_multiple_additive(self):
        rules = [
            _rule(type="risk_adjust", action="*", risk_adjust=10),
            _rule(type="risk_adjust", action="send_*", risk_adjust=5),
        ]
        adjustments = compute_risk_adjustments(rules, "send_email", {}, EvalContext())
        total = sum(a.delta for a in adjustments)
        assert total == 15

    def test_context_rule_met(self):
        now = datetime.now(timezone.utc)
        ctx = ContextCondition(type="repetition", count=2, window_seconds=60, risk_adjust=40)
        rules = [_rule(type="context_rule", action="*", context=ctx)]
        context = EvalContext(
            history=[
                _trace(action="send_email", timestamp=now - timedelta(seconds=5)) for _ in range(2)
            ],
            current_time=now,
        )
        adjustments = compute_risk_adjustments(rules, "send_email", {}, context)
        assert len(adjustments) == 1
        assert adjustments[0].delta == 40

    def test_context_rule_not_met(self):
        ctx = ContextCondition(type="repetition", count=10, window_seconds=60, risk_adjust=40)
        rules = [_rule(type="context_rule", action="*", context=ctx)]
        adjustments = compute_risk_adjustments(rules, "send_email", {}, EvalContext())
        assert len(adjustments) == 0


class TestResolveThresholds:
    def test_per_tool_override(self):
        overrides = ThresholdOverrides(
            global_thresholds=ThresholdConfig(allow_max=30, intervene_max=70),
            per_tool={"send_*": ThresholdConfig(allow_max=10, intervene_max=25)},
        )
        result = resolve_thresholds("send_email", overrides, ThresholdConfig())
        assert result.allow_max == 10
        assert result.intervene_max == 25

    def test_fallback_to_global(self):
        overrides = ThresholdOverrides(
            global_thresholds=ThresholdConfig(allow_max=20, intervene_max=60),
            per_tool={"send_*": ThresholdConfig(allow_max=10, intervene_max=25)},
        )
        result = resolve_thresholds("read_file", overrides, ThresholdConfig())
        assert result.allow_max == 20

    def test_most_specific_match(self):
        overrides = ThresholdOverrides(
            per_tool={
                "*": ThresholdConfig(allow_max=40, intervene_max=80),
                "send_*": ThresholdConfig(allow_max=10, intervene_max=30),
            },
        )
        result = resolve_thresholds("send_email", overrides, ThresholdConfig())
        assert result.allow_max == 10
