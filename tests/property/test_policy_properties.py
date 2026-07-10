"""Property-based tests for ostiari.policy."""

from datetime import datetime, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from ostiari.models import (
    PolicySet,
    Rule,
    ThresholdConfig,
    ThresholdOverrides,
)
from ostiari.policy.engine import PolicyEngine
from ostiari.policy.rules import match_rules, resolve_thresholds

action_names = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz_"),
    min_size=1,
    max_size=20,
)

risk_values = st.integers(min_value=-100, max_value=100).filter(lambda x: x != 0)


def _rule(type="risk_adjust", action="*", risk_adjust=10, priority=0):
    kwargs = {"type": type, "action": action, "priority": priority}
    if type == "risk_adjust":
        kwargs["risk_adjust"] = risk_adjust
    return Rule(**kwargs)


class TestMatchRulesProperties:
    @given(action=action_names)
    def test_wildcard_matches_everything(self, action):
        rules = [_rule(action="*")]
        assert len(match_rules(action, rules)) == 1

    @given(action=action_names)
    def test_match_is_idempotent(self, action):
        rules = [_rule(action="*"), _rule(action="send_*")]
        result1 = match_rules(action, rules)
        result2 = match_rules(action, rules)
        assert result1 == result2


class TestBlockDominance:
    @given(action=action_names)
    @settings(max_examples=50)
    def test_block_always_blocks(self, action):
        engine = PolicyEngine()
        block_rule = Rule(type="block", action="*")
        allow_rule = Rule(type="allow", action="*")
        risk_rule = _rule(action="*", risk_adjust=50)
        engine._active_policy = PolicySet(
            rules=[block_rule, allow_rule, risk_rule],
            loaded_at=datetime.now(timezone.utc),
        )
        result = engine.evaluate(action, {})
        assert result.decision == "block"


class TestAllowBypass:
    @given(action=action_names)
    @settings(max_examples=50)
    def test_allow_bypasses_scoring(self, action):
        engine = PolicyEngine()
        allow_rule = Rule(type="allow", action="*")
        risk_rule = _rule(action="*", risk_adjust=50)
        engine._active_policy = PolicySet(
            rules=[allow_rule, risk_rule],
            loaded_at=datetime.now(timezone.utc),
        )
        result = engine.evaluate(action, {})
        assert result.decision == "allow"
        assert result.risk_adjustments == []


class TestRiskAdjustmentAdditivity:
    @given(
        adj1=risk_values,
        adj2=risk_values,
    )
    @settings(max_examples=50)
    def test_two_rules_sum_correctly(self, adj1, adj2):
        engine = PolicyEngine()
        engine._active_policy = PolicySet(
            rules=[
                _rule(action="*", risk_adjust=adj1),
                _rule(action="tool_*", risk_adjust=adj2),
            ],
            loaded_at=datetime.now(timezone.utc),
        )
        result = engine.evaluate("tool_x", {})
        total = sum(a.delta for a in result.risk_adjustments)
        assert total == adj1 + adj2


class TestThresholdResolution:
    @given(
        allow_max=st.integers(min_value=0, max_value=49),
        intervene_max=st.integers(min_value=50, max_value=100),
    )
    def test_ordering_preserved(self, allow_max, intervene_max):
        overrides = ThresholdOverrides(
            global_thresholds=ThresholdConfig(allow_max=allow_max, intervene_max=intervene_max),
        )
        result = resolve_thresholds("any_action", overrides, ThresholdConfig())
        assert result.allow_max < result.intervene_max


class TestMergeProperties:
    @given(action=action_names)
    @settings(max_examples=30)
    def test_override_replaces_base_for_same_pattern(self, action):
        base = PolicySet(
            rules=[_rule(action=action, risk_adjust=10)],
            loaded_at=datetime.now(timezone.utc),
        )
        override = PolicySet(
            rules=[_rule(action=action, risk_adjust=50)],
            loaded_at=datetime.now(timezone.utc),
        )
        merged = PolicyEngine.merge(base, override)
        matching = [r for r in merged.rules if r.action == action and r.type == "risk_adjust"]
        assert len(matching) == 1
        assert matching[0].risk_adjust == 50
