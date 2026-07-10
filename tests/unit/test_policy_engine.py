"""Unit tests for ostiari.policy.engine."""

import threading
from datetime import datetime, timedelta, timezone

import pytest

from ostiari.exceptions import PolicyValidationError
from ostiari.models import (
    EvalContext,
    Rule,
    TraceEntry,
)
from ostiari.policy.engine import PolicyEngine


def _write_policy(tmp_path, filename, content):
    f = tmp_path / filename
    f.write_text(content)
    return f


def _trace(action="tool_a", timestamp=None):
    return TraceEntry(
        trace_id="t1",
        timestamp=timestamp or datetime.now(timezone.utc),
        action=action,
        risk_score=0,
        tier="allow",
        duration_ms=1.0,
    )


class TestLoad:
    def test_single_file(self, tmp_path):
        f = _write_policy(tmp_path, "p.yaml", "block:\n  - rm_rf\n")
        engine = PolicyEngine()
        engine.load([f])
        assert engine.active_rule_count == 1

    def test_multiple_files_merge(self, tmp_path):
        f1 = _write_policy(tmp_path, "base.yaml", "allow:\n  - safe_read\n")
        f2 = _write_policy(tmp_path, "override.yaml", "block:\n  - dangerous\n")
        engine = PolicyEngine()
        engine.load([f1, f2])
        assert engine.active_rule_count == 2

    def test_load_raises_on_invalid(self, tmp_path):
        f = _write_policy(tmp_path, "bad.yaml", "rules:\n  - type: invalid\n    action: x\n")
        engine = PolicyEngine()
        with pytest.raises(PolicyValidationError):
            engine.load([f])

    def test_too_many_files(self, tmp_path):
        files = [_write_policy(tmp_path, f"p{i}.yaml", "allow:\n  - tool\n") for i in range(11)]
        engine = PolicyEngine()
        with pytest.raises(PolicyValidationError) as exc_info:
            engine.load(files)
        assert "10" in exc_info.value.message


class TestEvaluate:
    def test_block_short_circuits(self, tmp_path):
        f = _write_policy(tmp_path, "p.yaml", "block:\n  - rm_*\n")
        engine = PolicyEngine()
        engine.load([f])
        result = engine.evaluate("rm_rf", {})
        assert result.decision == "block"
        assert result.blocked_by is not None

    def test_allow_short_circuits(self, tmp_path):
        f = _write_policy(tmp_path, "p.yaml", "allow:\n  - safe_*\n")
        engine = PolicyEngine()
        engine.load([f])
        result = engine.evaluate("safe_read", {})
        assert result.decision == "allow"

    def test_risk_adjust_accumulation(self, tmp_path):
        content = """rules:
  - type: risk_adjust
    action: send_*
    risk_adjust: 15
  - type: risk_adjust
    action: "*"
    risk_adjust: 5
"""
        f = _write_policy(tmp_path, "p.yaml", content)
        engine = PolicyEngine()
        engine.load([f])
        result = engine.evaluate("send_email", {})
        assert result.decision == "evaluate"
        total = sum(a.delta for a in result.risk_adjustments)
        assert total == 20

    def test_context_rule_applied(self, tmp_path):
        content = """rules:
  - type: context_rule
    action: send_*
    context:
      type: repetition
      count: 2
      window_seconds: 60
      risk_adjust: 40
"""
        f = _write_policy(tmp_path, "p.yaml", content)
        engine = PolicyEngine()
        engine.load([f])
        now = datetime.now(timezone.utc)
        context = EvalContext(
            history=[
                _trace(action="send_email", timestamp=now - timedelta(seconds=i)) for i in range(2)
            ],
            current_time=now,
        )
        result = engine.evaluate("send_email", {}, context)
        assert len(result.risk_adjustments) == 1
        assert result.risk_adjustments[0].delta == 40

    def test_empty_policy_returns_evaluate(self):
        engine = PolicyEngine()
        result = engine.evaluate("anything", {})
        assert result.decision == "evaluate"
        assert result.risk_adjustments == []

    def test_threshold_override_in_result(self, tmp_path):
        content = """thresholds:
  per_tool:
    send_email:
      allow_max: 10
      intervene_max: 25
"""
        f = _write_policy(tmp_path, "p.yaml", content)
        engine = PolicyEngine()
        engine.load([f])
        result = engine.evaluate("send_email", {})
        assert result.effective_thresholds.allow_max == 10


class TestValidate:
    def test_valid_file(self, tmp_path):
        f = _write_policy(tmp_path, "p.yaml", "allow:\n  - safe_tool\n")
        engine = PolicyEngine()
        errors = engine.validate(f)
        assert errors == []

    def test_invalid_file(self, tmp_path):
        f = _write_policy(tmp_path, "p.yaml", "rules:\n  - type: bad\n    action: x\n")
        engine = PolicyEngine()
        errors = engine.validate(f)
        assert len(errors) >= 1


class TestReload:
    def test_reload_success(self, tmp_path):
        f = _write_policy(tmp_path, "p.yaml", "allow:\n  - tool_a\n")
        engine = PolicyEngine()
        engine.load([f])
        assert engine.active_rule_count == 1

        f.write_text("allow:\n  - tool_a\n  - tool_b\nblock:\n  - dangerous\n")
        assert engine.reload() is True
        assert engine.active_rule_count == 3

    def test_reload_failure_keeps_old(self, tmp_path):
        f = _write_policy(tmp_path, "p.yaml", "allow:\n  - tool_a\n")
        engine = PolicyEngine()
        engine.load([f])

        f.write_text("invalid: yaml: [broken")
        assert engine.reload() is False
        assert engine.active_rule_count == 1

    def test_reload_no_paths(self):
        engine = PolicyEngine()
        assert engine.reload() is False

    def test_reload_metrics(self, tmp_path):
        f = _write_policy(tmp_path, "p.yaml", "allow:\n  - x\n")
        engine = PolicyEngine()
        engine.load([f])
        engine.reload()
        assert engine.reload_count == 1
        assert engine.last_reload_time is not None


class TestGetRules:
    def test_returns_matching(self, tmp_path):
        content = "allow:\n  - safe_*\nblock:\n  - rm_*\n"
        f = _write_policy(tmp_path, "p.yaml", content)
        engine = PolicyEngine()
        engine.load([f])
        rules = engine.get_rules("safe_read")
        assert any(r.type == "allow" for r in rules)
        assert not any(r.type == "block" for r in rules)


class TestDecoratorRules:
    def test_yaml_overrides_decorator(self, tmp_path):
        engine = PolicyEngine()
        engine.register_decorator_rules(
            [
                Rule(type="risk_adjust", action="send_email", risk_adjust=10),
            ]
        )
        f = _write_policy(
            tmp_path,
            "p.yaml",
            """rules:
  - type: risk_adjust
    action: send_email
    risk_adjust: 50
""",
        )
        engine.load([f])
        result = engine.evaluate("send_email", {})
        deltas = [a.delta for a in result.risk_adjustments]
        assert 50 in deltas
        assert 10 not in deltas


class TestThreadSafety:
    def test_concurrent_evaluate_during_reload(self, tmp_path):
        f = _write_policy(tmp_path, "p.yaml", "allow:\n  - tool_a\n")
        engine = PolicyEngine()
        engine.load([f])

        errors = []
        stop = threading.Event()

        def evaluate_loop():
            while not stop.is_set():
                try:
                    result = engine.evaluate("tool_a", {})
                    assert result.decision in ("allow", "evaluate")
                except Exception as e:
                    errors.append(e)
                    break

        threads = [threading.Thread(target=evaluate_loop) for _ in range(4)]
        for t in threads:
            t.start()

        for _ in range(10):
            f.write_text("allow:\n  - tool_a\n  - tool_b\n")
            engine.reload()
            f.write_text("allow:\n  - tool_a\n")
            engine.reload()

        stop.set()
        for t in threads:
            t.join(timeout=5)

        assert errors == []
