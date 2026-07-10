"""Unit tests for ostiari.anomaly.detector (orchestrator)."""

from datetime import datetime, timezone
from typing import Any

import pytest

from ostiari.anomaly.detector import AnomalyDetector
from ostiari.models import AnomalySignal, TraceEntry


def _trace(action="tool_a", params=None, result=None):
    return TraceEntry(
        trace_id="t1",
        timestamp=datetime.now(timezone.utc),
        action=action,
        params=params or {},
        result=result,
        risk_score=0,
        tier="allow",
        duration_ms=1.0,
    )


class _GoodDetector:
    @property
    def name(self) -> str:
        return "custom_good"

    def detect(
        self, action: str, params: dict[str, Any], history: list[TraceEntry]
    ) -> AnomalySignal | None:
        if action == "trigger_custom":
            return AnomalySignal(
                detector="custom_good",
                severity="low",
                score_contribution=5,
                description="Custom detected",
                evidence={},
            )
        return None


class _FailingDetector:
    @property
    def name(self) -> str:
        return "custom_failing"

    def detect(
        self, action: str, params: dict[str, Any], history: list[TraceEntry]
    ) -> AnomalySignal | None:
        raise RuntimeError("Detector crashed")


class _InvalidDetector:
    pass


class TestAnalyzeDispatch:
    def test_hallucination_detected(self):
        ad = AnomalyDetector()
        ad.register_tool("read_file")
        ad.register_tool("write_file")
        signals = ad.analyze("execute_sql", {}, [])
        assert any(s.detector == "hallucination" for s in signals)

    def test_loop_detected(self):
        ad = AnomalyDetector(loop_threshold=3)
        history = [_trace(action="search", params={"q": "x"}) for _ in range(3)]
        signals = ad.analyze("search", {"q": "x"}, history)
        assert any(s.detector == "loop" for s in signals)

    def test_drift_detected(self):
        ad = AnomalyDetector()
        ad.set_scope(["read_*"])
        signals = ad.analyze("send_email", {}, [])
        assert any(s.detector == "drift" for s in signals)

    def test_no_signals_on_valid_action(self):
        ad = AnomalyDetector()
        ad.register_tool("read_file")
        signals = ad.analyze("read_file", {}, [])
        assert signals == []

    def test_multiple_signals_collected(self):
        ad = AnomalyDetector(loop_threshold=3)
        ad.register_tool("read_file")
        ad.set_scope(["read_*"])
        history = [_trace(action="send_email", params={"to": "x"}) for _ in range(3)]
        signals = ad.analyze("send_email", {"to": "x"}, history)
        detectors_hit = {s.detector for s in signals}
        assert "hallucination" in detectors_hit
        assert "loop" in detectors_hit
        assert "drift" in detectors_hit

    def test_empty_history_works(self):
        ad = AnomalyDetector()
        signals = ad.analyze("any_action", {}, [])
        assert isinstance(signals, list)


class TestWindowSlicing:
    def test_history_sliced_to_window(self):
        ad = AnomalyDetector(loop_window=5, loop_threshold=3)
        history = [_trace(action="search", params={"q": "x"}) for _ in range(30)]
        signals = ad.analyze("search", {"q": "x"}, history)
        loop_signals = [s for s in signals if s.detector == "loop"]
        if loop_signals:
            assert loop_signals[0].evidence["window_size"] <= 5


class TestRegisterTool:
    def test_register_adds_to_inventory(self):
        ad = AnomalyDetector()
        ad.register_tool("my_tool", schema={"type": "object"})
        assert ad.inventory_size == 1
        signals = ad.analyze("my_tool", {}, [])
        assert not any(s.detector == "hallucination" for s in signals)

    def test_unregister_removes(self):
        ad = AnomalyDetector()
        ad.register_tool("my_tool")
        ad.unregister_tool("my_tool")
        assert ad.inventory_size == 0

    def test_unregister_nonexistent_no_error(self):
        ad = AnomalyDetector()
        ad.unregister_tool("nonexistent")
        assert ad.inventory_size == 0


class TestRegisterCustom:
    def test_valid_custom_detector(self):
        ad = AnomalyDetector()
        ad.register_custom(_GoodDetector())
        assert ad.detector_count == 5
        signals = ad.analyze("trigger_custom", {}, [])
        assert any(s.detector == "custom_good" for s in signals)

    def test_invalid_detector_raises_type_error(self):
        ad = AnomalyDetector()
        with pytest.raises(TypeError) as exc_info:
            ad.register_custom(_InvalidDetector())
        assert "CustomDetector protocol" in str(exc_info.value)


class TestDetectorFailureIsolation:
    def test_failing_detector_produces_error_signal(self):
        ad = AnomalyDetector()
        ad.register_custom(_FailingDetector())
        signals = ad.analyze("anything", {}, [])
        error_signals = [s for s in signals if s.detector == "_error"]
        assert len(error_signals) == 1
        assert error_signals[0].severity == "medium"
        assert error_signals[0].score_contribution == 10
        assert "RuntimeError" in error_signals[0].description

    def test_other_detectors_still_run_after_failure(self):
        ad = AnomalyDetector()
        ad.register_tool("read_file")
        ad.register_custom(_FailingDetector())
        ad.register_custom(_GoodDetector())
        signals = ad.analyze("trigger_custom", {}, [])
        detectors_hit = {s.detector for s in signals}
        assert "_error" in detectors_hit
        assert "custom_good" in detectors_hit
        assert "hallucination" in detectors_hit


class TestScope:
    def test_set_scope(self):
        ad = AnomalyDetector()
        ad.set_scope(["read_*", "analyze_*"])
        assert ad.scope_patterns == ["read_*", "analyze_*"]

    def test_clear_scope(self):
        ad = AnomalyDetector()
        ad.set_scope(["read_*"])
        ad.clear_scope()
        assert ad.scope_patterns is None
        signals = ad.analyze("anything", {}, [])
        assert not any(s.detector == "drift" for s in signals)
