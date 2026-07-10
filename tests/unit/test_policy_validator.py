"""Unit tests for ostiari.policy.validator."""

from pathlib import Path

import pytest

from ostiari.exceptions import PolicyValidationError
from ostiari.policy.parser import ParsedYAML
from ostiari.policy.validator import validate_policy, validate_policy_collected


def _parsed(allow=None, block=None, rules=None, thresholds=None) -> ParsedYAML:
    return ParsedYAML(
        allow=allow or [],
        block=block or [],
        rules=rules or [],
        thresholds=thresholds or {},
        source_path=Path("test.yaml"),
    )


class TestValidPolicies:
    def test_empty_policy(self):
        ps = validate_policy(_parsed())
        assert ps.rules == []

    def test_allow_only(self):
        ps = validate_policy(_parsed(allow=["safe_*"]))
        assert len(ps.rules) == 1
        assert ps.rules[0].type == "allow"
        assert ps.rules[0].action == "safe_*"

    def test_block_only(self):
        ps = validate_policy(_parsed(block=["rm_*"]))
        assert len(ps.rules) == 1
        assert ps.rules[0].type == "block"

    def test_risk_adjust_rule(self):
        ps = validate_policy(
            _parsed(
                rules=[
                    {
                        "type": "risk_adjust",
                        "action": "send_email",
                        "risk_adjust": 20,
                    }
                ]
            )
        )
        assert len(ps.rules) == 1
        assert ps.rules[0].risk_adjust == 20

    def test_context_rule_repetition(self):
        ps = validate_policy(
            _parsed(
                rules=[
                    {
                        "type": "context_rule",
                        "action": "send_*",
                        "context": {
                            "type": "repetition",
                            "count": 5,
                            "window_seconds": 60,
                            "risk_adjust": 40,
                        },
                    }
                ]
            )
        )
        assert ps.rules[0].context is not None
        assert ps.rules[0].context.type == "repetition"
        assert ps.rules[0].context.count == 5

    def test_threshold_override_rule(self):
        ps = validate_policy(
            _parsed(
                rules=[
                    {
                        "type": "threshold_override",
                        "action": "send_email",
                        "threshold_override": {"allow_max": 10, "intervene_max": 30},
                    }
                ]
            )
        )
        assert ps.rules[0].threshold_override is not None
        assert ps.rules[0].threshold_override.allow_max == 10

    def test_thresholds_section(self):
        ps = validate_policy(
            _parsed(
                thresholds={
                    "global": {"allow_max": 20, "intervene_max": 60},
                    "per_tool": {"send_email": {"allow_max": 10, "intervene_max": 25}},
                }
            )
        )
        assert ps.thresholds.global_thresholds.allow_max == 20
        assert "send_email" in ps.thresholds.per_tool


class TestValidationErrors:
    def test_allow_block_conflict(self):
        with pytest.raises(PolicyValidationError) as exc_info:
            validate_policy(_parsed(allow=["tool_a"], block=["tool_a"]))
        assert "both allow and block" in exc_info.value.message

    def test_invalid_rule_type(self):
        with pytest.raises(PolicyValidationError) as exc_info:
            validate_policy(_parsed(rules=[{"type": "invalid", "action": "x"}]))
        assert "Invalid rule type" in exc_info.value.message

    def test_missing_action(self):
        with pytest.raises(PolicyValidationError) as exc_info:
            validate_policy(_parsed(rules=[{"type": "allow"}]))
        assert "action" in exc_info.value.message

    def test_risk_adjust_zero(self):
        with pytest.raises(PolicyValidationError) as exc_info:
            validate_policy(
                _parsed(
                    rules=[
                        {
                            "type": "risk_adjust",
                            "action": "x",
                            "risk_adjust": 0,
                        }
                    ]
                )
            )
        assert "no effect" in exc_info.value.message

    def test_risk_adjust_missing(self):
        with pytest.raises(PolicyValidationError) as exc_info:
            validate_policy(
                _parsed(
                    rules=[
                        {
                            "type": "risk_adjust",
                            "action": "x",
                        }
                    ]
                )
            )
        assert "risk_adjust" in exc_info.value.message

    def test_pattern_too_long(self):
        with pytest.raises(PolicyValidationError) as exc_info:
            validate_policy(_parsed(allow=["x" * 300]))
        assert "256" in exc_info.value.message

    def test_pattern_null_byte(self):
        with pytest.raises(PolicyValidationError) as exc_info:
            validate_policy(_parsed(allow=["foo\x00bar"]))
        assert "null byte" in exc_info.value.message

    def test_threshold_ordering_violated(self):
        with pytest.raises(PolicyValidationError) as exc_info:
            validate_policy(
                _parsed(
                    thresholds={
                        "global": {"allow_max": 80, "intervene_max": 20},
                    }
                )
            )
        assert "allow_max" in exc_info.value.message

    def test_capacity_exceeded(self):
        rules = [
            {"type": "risk_adjust", "action": f"tool_{i}", "risk_adjust": 10} for i in range(501)
        ]
        with pytest.raises(PolicyValidationError) as exc_info:
            validate_policy(_parsed(rules=rules))
        assert "500" in exc_info.value.message

    def test_context_repetition_invalid_count(self):
        with pytest.raises(PolicyValidationError) as exc_info:
            validate_policy(
                _parsed(
                    rules=[
                        {
                            "type": "context_rule",
                            "action": "x",
                            "context": {
                                "type": "repetition",
                                "count": 1,
                                "window_seconds": 60,
                                "risk_adjust": 10,
                            },
                        }
                    ]
                )
            )
        assert "count" in exc_info.value.message

    def test_context_escalation_missing_preceding(self):
        with pytest.raises(PolicyValidationError) as exc_info:
            validate_policy(
                _parsed(
                    rules=[
                        {
                            "type": "context_rule",
                            "action": "x",
                            "context": {"type": "escalation", "risk_adjust": 10},
                        }
                    ]
                )
            )
        assert "preceding_action" in exc_info.value.message

    def test_context_time_invalid_hours(self):
        with pytest.raises(PolicyValidationError) as exc_info:
            validate_policy(
                _parsed(
                    rules=[
                        {
                            "type": "context_rule",
                            "action": "x",
                            "context": {
                                "type": "time_of_day",
                                "outside_hours": [25, 17],
                                "risk_adjust": 10,
                            },
                        }
                    ]
                )
            )
        assert "0-23" in exc_info.value.message

    def test_invalid_timezone(self):
        with pytest.raises(PolicyValidationError) as exc_info:
            validate_policy(
                _parsed(
                    rules=[
                        {
                            "type": "context_rule",
                            "action": "x",
                            "context": {
                                "type": "time_of_day",
                                "outside_hours": [9, 17],
                                "timezone": "Invalid/TZ",
                                "risk_adjust": 10,
                            },
                        }
                    ]
                )
            )
        assert "timezone" in exc_info.value.message


class TestCollectedValidation:
    def test_multiple_errors_collected(self):
        parsed = _parsed(
            allow=[""],
            rules=[{"type": "invalid", "action": "x"}],
        )
        policy_set, errors = validate_policy_collected(parsed)
        assert policy_set is None
        assert len(errors) >= 2

    def test_valid_returns_policy_set(self):
        parsed = _parsed(allow=["safe_tool"])
        policy_set, errors = validate_policy_collected(parsed)
        assert policy_set is not None
        assert errors == []


class TestRuleSorting:
    def test_block_before_allow_before_others(self):
        ps = validate_policy(
            _parsed(
                allow=["tool_a"],
                block=["tool_b"],
                rules=[{"type": "risk_adjust", "action": "tool_c", "risk_adjust": 10}],
            )
        )
        types = [r.type for r in ps.rules]
        assert types.index("block") < types.index("allow")
        assert types.index("allow") < types.index("risk_adjust")

    def test_priority_ordering(self):
        ps = validate_policy(
            _parsed(
                rules=[
                    {"type": "risk_adjust", "action": "a", "risk_adjust": 10, "priority": 0},
                    {"type": "risk_adjust", "action": "b", "risk_adjust": 20, "priority": 5},
                ]
            )
        )
        assert ps.rules[0].action == "b"
        assert ps.rules[1].action == "a"
