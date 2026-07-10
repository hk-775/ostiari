"""Unit tests for ostiari.anomaly.contradiction."""

from datetime import datetime, timezone

from ostiari.anomaly.contradiction import ContradictionDetector
from ostiari.models import TraceEntry


def _trace(action="tool_a", result=None):
    return TraceEntry(
        trace_id="t1",
        timestamp=datetime.now(timezone.utc),
        action=action,
        result=result,
        risk_score=0,
        tier="allow",
        duration_ms=1.0,
    )


class TestContradictionDetection:
    def test_mismatch_detected(self):
        detector = ContradictionDetector()
        history = [_trace(action="get_status", result={"status": "failed"})]
        params = {"expected_result": {"status": "success"}}
        signal = detector.detect("get_status", params, history)
        assert signal is not None
        assert signal.detector == "contradiction"
        assert signal.severity == "medium"
        assert signal.score_contribution == 40
        assert signal.evidence["field"] == "status"

    def test_matching_result_no_signal(self):
        detector = ContradictionDetector()
        history = [_trace(action="get_status", result={"status": "success"})]
        params = {"expected_result": {"status": "success"}}
        signal = detector.detect("get_status", params, history)
        assert signal is None

    def test_no_expected_result_bypass(self):
        detector = ContradictionDetector()
        history = [_trace(action="get_status", result="ok")]
        signal = detector.detect("get_status", {"other": "param"}, history)
        assert signal is None

    def test_no_prior_result_bypass(self):
        detector = ContradictionDetector()
        history = [_trace(action="other_action", result="data")]
        params = {"expected_result": "something"}
        signal = detector.detect("get_status", params, history)
        assert signal is None

    def test_prior_with_none_result_bypass(self):
        detector = ContradictionDetector()
        history = [_trace(action="get_status", result=None)]
        params = {"expected_result": "something"}
        signal = detector.detect("get_status", params, history)
        assert signal is None


class TestPartialMismatch:
    def test_dict_partial_mismatch(self):
        detector = ContradictionDetector()
        history = [_trace(action="check", result={"count": 5, "status": "ok"})]
        params = {"expected_result": {"count": 10, "status": "ok"}}
        signal = detector.detect("check", params, history)
        assert signal is not None
        assert signal.evidence["field"] == "count"

    def test_dict_missing_key(self):
        detector = ContradictionDetector()
        history = [_trace(action="check", result={"status": "ok"})]
        params = {"expected_result": {"status": "ok", "extra": "field"}}
        signal = detector.detect("check", params, history)
        assert signal is not None
        assert signal.evidence["field"] == "extra"


class TestNonDictComparison:
    def test_scalar_mismatch(self):
        detector = ContradictionDetector()
        history = [_trace(action="get_count", result=42)]
        params = {"expected_result": 100}
        signal = detector.detect("get_count", params, history)
        assert signal is not None
        assert signal.evidence["field"] == "result"

    def test_scalar_match(self):
        detector = ContradictionDetector()
        history = [_trace(action="get_count", result=42)]
        params = {"expected_result": 42}
        signal = detector.detect("get_count", params, history)
        assert signal is None
