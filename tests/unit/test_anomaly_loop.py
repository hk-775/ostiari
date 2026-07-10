"""Unit tests for ostiari.anomaly.loop."""

from datetime import datetime, timezone

from ostiari.anomaly.loop import LoopDetector, _extract_tokens, _jaccard
from ostiari.models import TraceEntry


def _trace(action="tool_a", params=None, timestamp=None):
    return TraceEntry(
        trace_id="t1",
        timestamp=timestamp or datetime.now(timezone.utc),
        action=action,
        params=params or {},
        risk_score=0,
        tier="allow",
        duration_ms=1.0,
    )


class TestExactLoopDetection:
    def test_exact_loop_detected(self):
        detector = LoopDetector(threshold=3)
        history = [_trace(action="search", params={"q": "weather"}) for _ in range(3)]
        signal = detector.detect("search", {"q": "weather"}, history)
        assert signal is not None
        assert signal.detector == "loop"
        assert signal.evidence["similarity_type"] == "exact"
        assert signal.evidence["count"] == 3

    def test_below_threshold_no_signal(self):
        detector = LoopDetector(threshold=3)
        history = [_trace(action="search", params={"q": "weather"}) for _ in range(2)]
        signal = detector.detect("search", {"q": "weather"}, history)
        assert signal is None

    def test_severity_scales_with_count(self):
        detector = LoopDetector(threshold=3)
        history = [_trace(action="search", params={"q": "x"}) for _ in range(10)]
        signal = detector.detect("search", {"q": "x"}, history)
        assert signal is not None
        assert signal.severity == "critical"
        assert signal.score_contribution == 80

    def test_severity_medium_for_small_count(self):
        detector = LoopDetector(threshold=3)
        history = [_trace(action="search", params={"q": "x"}) for _ in range(4)]
        signal = detector.detect("search", {"q": "x"}, history)
        assert signal is not None
        assert signal.severity == "medium"

    def test_severity_high_for_6_repeats(self):
        detector = LoopDetector(threshold=3)
        history = [_trace(action="search", params={"q": "x"}) for _ in range(7)]
        signal = detector.detect("search", {"q": "x"}, history)
        assert signal is not None
        assert signal.severity == "high"

    def test_empty_history_no_signal(self):
        detector = LoopDetector()
        signal = detector.detect("search", {"q": "x"}, [])
        assert signal is None

    def test_empty_params_exact_match(self):
        detector = LoopDetector(threshold=2)
        history = [_trace(action="list_files", params={}) for _ in range(3)]
        signal = detector.detect("list_files", {}, history)
        assert signal is not None


class TestSimilarLoopDetection:
    def test_similar_params_detected(self):
        detector = LoopDetector(threshold=3, similarity_threshold=0.3)
        history = [
            _trace(action="search", params={"q": "weather today"}),
            _trace(action="search", params={"q": "weather now"}),
            _trace(action="search", params={"q": "current weather"}),
        ]
        signal = detector.detect("search", {"q": "weather forecast"}, history)
        assert signal is not None
        assert signal.evidence["similarity_type"] == "similar"

    def test_different_params_no_similar(self):
        detector = LoopDetector(threshold=3, similarity_threshold=0.8)
        history = [
            _trace(action="search", params={"q": "python tutorial"}),
            _trace(action="search", params={"q": "rust guide"}),
            _trace(action="search", params={"q": "go docs"}),
        ]
        signal = detector.detect("search", {"q": "java reference"}, history)
        assert signal is None


class TestSeparation:
    def test_separated_repeats_not_flagged(self):
        detector = LoopDetector(threshold=3, separation_threshold=3)
        history = [
            _trace(action="read_file", params={"path": "config.yaml"}),
            _trace(action="write_file", params={"path": "out.txt"}),
            _trace(action="analyze", params={}),
            _trace(action="validate", params={}),
        ]
        signal = detector.detect("read_file", {"path": "config.yaml"}, history)
        assert signal is None

    def test_not_separated_still_flags(self):
        detector = LoopDetector(threshold=3, separation_threshold=3)
        history = [
            _trace(action="search", params={"q": "x"}),
            _trace(action="search", params={"q": "x"}),
            _trace(action="search", params={"q": "x"}),
        ]
        signal = detector.detect("search", {"q": "x"}, history)
        assert signal is not None


class TestTokenHelpers:
    def test_extract_tokens_simple(self):
        tokens = _extract_tokens({"q": "Hello World"})
        assert tokens == {"hello", "world"}

    def test_extract_tokens_nested(self):
        tokens = _extract_tokens({"query": {"text": "foo bar"}, "tags": ["baz"]})
        assert "foo" in tokens
        assert "bar" in tokens
        assert "baz" in tokens

    def test_extract_tokens_empty(self):
        assert _extract_tokens({}) == set()

    def test_jaccard_identical(self):
        assert _jaccard({"a", "b"}, {"a", "b"}) == 1.0

    def test_jaccard_disjoint(self):
        assert _jaccard({"a", "b"}, {"c", "d"}) == 0.0

    def test_jaccard_partial(self):
        assert _jaccard({"a", "b", "c"}, {"b", "c", "d"}) == 0.5

    def test_jaccard_both_empty(self):
        assert _jaccard(set(), set()) == 1.0

    def test_jaccard_one_empty(self):
        assert _jaccard({"a"}, set()) == 0.0
