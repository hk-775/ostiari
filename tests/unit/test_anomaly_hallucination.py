"""Unit tests for ostiari.anomaly.hallucination."""

from datetime import datetime, timezone

from ostiari.anomaly.hallucination import HallucinationDetector
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


class TestUnknownToolDetection:
    def test_unknown_tool_flagged(self):
        inventory = {"read_file": None, "write_file": None, "search": None}
        detector = HallucinationDetector(inventory_ref=inventory)
        signal = detector.detect("execute_sql", {}, [])
        assert signal is not None
        assert signal.detector == "hallucination"
        assert signal.severity == "high"
        assert signal.score_contribution == 70
        assert signal.evidence["attempted_tool"] == "execute_sql"

    def test_known_tool_no_signal(self):
        inventory = {"read_file": None, "write_file": None}
        detector = HallucinationDetector(inventory_ref=inventory)
        signal = detector.detect("read_file", {}, [])
        assert signal is None

    def test_empty_inventory_bypass(self):
        detector = HallucinationDetector(inventory_ref={})
        signal = detector.detect("anything", {}, [])
        assert signal is None


class TestTypoSuggestion:
    def test_close_match_suggests(self):
        inventory = {"read_file": None, "write_file": None, "search": None}
        detector = HallucinationDetector(inventory_ref=inventory)
        signal = detector.detect("read_files", {}, [])
        assert signal is not None
        assert signal.evidence["suggestion"] is not None
        assert "read_file" in signal.evidence["suggestion"]
        assert signal.evidence["similarity_score"] is not None
        assert signal.evidence["similarity_score"] >= 0.7

    def test_no_close_match_no_suggestion(self):
        inventory = {"read_file": None, "write_file": None}
        detector = HallucinationDetector(inventory_ref=inventory)
        signal = detector.detect("completely_different_xyz", {}, [])
        assert signal is not None
        assert signal.evidence["suggestion"] is None
        assert signal.evidence["similarity_score"] is None

    def test_best_match_among_multiple(self):
        inventory = {"search": None, "search_web": None, "search_local": None}
        detector = HallucinationDetector(inventory_ref=inventory)
        signal = detector.detect("serch", {}, [])
        assert signal is not None
        assert signal.evidence["suggestion"] is not None
        assert "search" in signal.evidence["suggestion"]


class TestDynamicInventory:
    def test_newly_registered_tool_recognized(self):
        inventory = {"read_file": None}
        detector = HallucinationDetector(inventory_ref=inventory)

        signal = detector.detect("analyze_data", {}, [])
        assert signal is not None

        inventory["analyze_data"] = None
        signal = detector.detect("analyze_data", {}, [])
        assert signal is None
