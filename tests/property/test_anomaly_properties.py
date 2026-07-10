"""Property-based tests for ostiari.anomaly."""

from datetime import datetime, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from ostiari.anomaly.detector import AnomalyDetector
from ostiari.anomaly.loop import LoopDetector, _jaccard
from ostiari.models import TraceEntry


def _trace(action="tool_a", params=None):
    return TraceEntry(
        trace_id="t1",
        timestamp=datetime.now(timezone.utc),
        action=action,
        params=params or {},
        risk_score=0,
        tier="allow",
        duration_ms=1.0,
    )


action_names = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz_"),
    min_size=1,
    max_size=15,
)

param_values = st.dictionaries(
    keys=st.text(alphabet="abcdefghijk", min_size=1, max_size=5),
    values=st.text(alphabet="abcdefghijklmnop ", min_size=0, max_size=20),
    max_size=3,
)


class TestMonotonicity:
    @given(count=st.integers(min_value=3, max_value=20))
    @settings(max_examples=30)
    def test_more_repeats_higher_or_equal_score(self, count):
        detector = LoopDetector(threshold=3)
        history_small = [_trace(action="tool", params={"k": "v"}) for _ in range(3)]
        history_large = [_trace(action="tool", params={"k": "v"}) for _ in range(count)]

        signal_small = detector.detect("tool", {"k": "v"}, history_small)
        signal_large = detector.detect("tool", {"k": "v"}, history_large)

        assert signal_small is not None
        assert signal_large is not None
        assert signal_large.score_contribution >= signal_small.score_contribution


class TestIdempotency:
    @given(action=action_names, params=param_values)
    @settings(max_examples=30)
    def test_same_input_same_output(self, action, params):
        detector = LoopDetector(threshold=3)
        history = [_trace(action=action, params=params) for _ in range(5)]
        result1 = detector.detect(action, params, history)
        result2 = detector.detect(action, params, history)
        assert result1 == result2


class TestBoundedOutput:
    @given(count=st.integers(min_value=1, max_value=50))
    @settings(max_examples=30)
    def test_score_always_bounded(self, count):
        detector = LoopDetector(threshold=1)
        history = [_trace(action="tool", params={"k": "v"}) for _ in range(count)]
        signal = detector.detect("tool", {"k": "v"}, history)
        if signal is not None:
            assert 0 <= signal.score_contribution <= 100


class TestDetectorIsolation:
    @given(action=action_names)
    @settings(max_examples=20)
    def test_failing_custom_doesnt_affect_builtin(self, action):
        class _Crasher:
            @property
            def name(self):
                return "crasher"

            def detect(self, action, params, history):
                raise ValueError("boom")

        ad = AnomalyDetector(loop_threshold=3)
        ad.register_tool("safe_tool")
        ad.register_custom(_Crasher())

        signals = ad.analyze(action, {}, [])
        error_signals = [s for s in signals if s.detector == "_error"]
        assert len(error_signals) == 1
        assert error_signals[0].score_contribution == 10


class TestJaccardProperties:
    @given(
        tokens=st.frozensets(
            st.text(alphabet="abcde", min_size=1, max_size=5), min_size=1, max_size=10
        ),
    )
    def test_symmetry(self, tokens):
        a = set(tokens)
        b = set(tokens)
        assert _jaccard(a, b) == _jaccard(b, a)

    @given(
        a=st.frozensets(st.text(alphabet="abc", min_size=1, max_size=3), max_size=5),
        b=st.frozensets(st.text(alphabet="abc", min_size=1, max_size=3), max_size=5),
    )
    def test_bounded(self, a, b):
        result = _jaccard(set(a), set(b))
        assert 0.0 <= result <= 1.0
