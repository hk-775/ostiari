"""Unit tests for ostiari.anomaly.drift."""

from datetime import datetime, timezone

from ostiari.anomaly.drift import DriftDetector
from ostiari.models import TraceEntry


def _trace(action="tool_a"):
    return TraceEntry(
        trace_id="t1",
        timestamp=datetime.now(timezone.utc),
        action=action,
        risk_score=0,
        tier="allow",
        duration_ms=1.0,
    )


class _MockOrchestrator:
    def __init__(self, scope=None):
        self._scope = scope


class _Scope:
    def __init__(self, patterns):
        self.allowed_patterns = patterns


class TestScopeViolation:
    def test_out_of_scope_flagged(self):
        scope = _Scope(["read_*", "analyze_*"])
        orchestrator = _MockOrchestrator(scope=scope)
        detector = DriftDetector(orchestrator=orchestrator)
        signal = detector.detect("send_email", {}, [])
        assert signal is not None
        assert signal.detector == "drift"
        assert signal.severity == "high"
        assert signal.score_contribution == 60
        assert signal.evidence["drift_type"] == "scope_violation"

    def test_in_scope_no_signal(self):
        scope = _Scope(["read_*", "analyze_*"])
        orchestrator = _MockOrchestrator(scope=scope)
        detector = DriftDetector(orchestrator=orchestrator)
        signal = detector.detect("read_file", {}, [])
        assert signal is None

    def test_glob_pattern_matches(self):
        scope = _Scope(["*_file", "search"])
        orchestrator = _MockOrchestrator(scope=scope)
        detector = DriftDetector(orchestrator=orchestrator)
        assert detector.detect("read_file", {}, []) is None
        assert detector.detect("write_file", {}, []) is None
        assert detector.detect("search", {}, []) is None
        assert detector.detect("delete_db", {}, []) is not None


class TestNoScope:
    def test_no_scope_bypass(self):
        orchestrator = _MockOrchestrator(scope=None)
        detector = DriftDetector(orchestrator=orchestrator)
        signal = detector.detect("anything", {}, [])
        assert signal is None


class TestProgressiveDrift:
    def test_progressive_drift_detected(self):
        scope = _Scope(["read_*"])
        orchestrator = _MockOrchestrator(scope=scope)
        detector = DriftDetector(orchestrator=orchestrator)
        history = [
            _trace(action="read_file"),
            _trace(action="send_email"),
            _trace(action="delete_db"),
        ]
        signal = detector.detect("execute_code", {}, history)
        assert signal is not None
        assert signal.severity == "critical"
        assert signal.score_contribution == 80
        assert signal.evidence["drift_type"] == "progressive"

    def test_mixed_history_no_progressive(self):
        scope = _Scope(["read_*", "analyze_*"])
        orchestrator = _MockOrchestrator(scope=scope)
        detector = DriftDetector(orchestrator=orchestrator)
        history = [
            _trace(action="read_file"),
            _trace(action="analyze_data"),
            _trace(action="read_db"),
            _trace(action="analyze_output"),
        ]
        # Only 1 out-of-scope (current action) + 0 from history = 1, not >= 3
        signal = detector.detect("send_email", {}, history)
        assert signal is not None
        assert signal.evidence["drift_type"] == "scope_violation"

    def test_all_recent_out_of_scope(self):
        scope = _Scope(["read_*"])
        orchestrator = _MockOrchestrator(scope=scope)
        detector = DriftDetector(orchestrator=orchestrator)
        history = [
            _trace(action="send_email"),
            _trace(action="delete_file"),
            _trace(action="execute_code"),
            _trace(action="drop_table"),
        ]
        signal = detector.detect("rm_rf", {}, history)
        assert signal is not None
        assert signal.severity == "critical"
