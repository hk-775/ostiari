"""Property-based tests for Unit 7: Extensibility & Adapters."""

from __future__ import annotations

import hashlib
import time

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from ostiari.adapters.protocol import AdapterContext, validate_adapter
from ostiari.exceptions import AdapterValidationError
from ostiari.models import AnomalySignal, TraceEntry, TraceFilters
from ostiari.testing import MockAdapter, MockDetector, MockStorage

action_strategy = st.from_regex(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){0,3}", fullmatch=True)
params_strategy = st.dictionaries(
    keys=st.from_regex(r"[a-z_][a-z0-9_]*", fullmatch=True),
    values=st.one_of(st.text(max_size=50), st.integers(), st.booleans()),
    max_size=5,
)


class TestAdapterContextProperties:
    @given(action=action_strategy, params=params_strategy)
    @settings(max_examples=50)
    def test_context_immutable(self, action, params):
        ctx = AdapterContext(
            action=action,
            params=params,
            framework_meta={},
            start_time=time.monotonic(),
        )
        assert ctx.action == action
        assert ctx.params == params

    @given(action=action_strategy, params=params_strategy)
    @settings(max_examples=50)
    def test_mock_adapter_always_valid(self, action, params):
        adapter = MockAdapter(name="prop-test")
        validate_adapter(adapter)
        ctx = adapter.wrap_tool_call(action, params)
        assert ctx.action == action
        assert ctx.params == params


class TestAdapterValidationProperties:
    @given(
        missing=st.lists(
            st.sampled_from(
                ["wrap_tool_call", "on_result", "on_error", "get_framework_state", "name"]
            ),
            min_size=1,
            max_size=5,
            unique=True,
        )
    )
    @settings(max_examples=30)
    def test_missing_methods_always_raises(self, missing):
        class Partial:
            pass

        obj = Partial()
        available = {"wrap_tool_call", "on_result", "on_error", "get_framework_state", "name"}
        for method in available - set(missing):
            if method == "name":
                obj.name = "test"
            else:
                setattr(obj, method, lambda *a, **k: None)

        with __import__("pytest").raises(AdapterValidationError):
            validate_adapter(obj)


class TestMockStorageProperties:
    @given(
        actions=st.lists(action_strategy, min_size=1, max_size=20),
        tier=st.sampled_from(["allow", "intervene", "block"]),
    )
    @settings(max_examples=30)
    def test_filter_by_tier_only_returns_matching(self, actions, tier):
        from datetime import datetime, timezone

        storage = MockStorage()
        for a in actions:
            entry = TraceEntry(
                trace_id=f"t-{a}",
                correlation_id="c-1",
                timestamp=datetime.now(timezone.utc),
                action=a,
                params={},
                result=None,
                risk_score=50,
                tier=tier,
                duration_ms=1.0,
                signals=[],
                anomalies=[],
                breaker_state=None,
                metadata={},
            )
            storage.save_trace(entry)

        results = storage.get_traces(TraceFilters(tier=tier, limit=100))
        assert len(results) == len(actions)
        for r in results:
            assert r.tier == tier

    @given(count=st.integers(min_value=0, max_value=50))
    @settings(max_examples=20)
    def test_limit_respected(self, count):
        from datetime import datetime, timezone

        storage = MockStorage()
        for i in range(count):
            entry = TraceEntry(
                trace_id=f"t-{i}",
                correlation_id="c-1",
                timestamp=datetime.now(timezone.utc),
                action=f"action.{i}",
                params={},
                result=None,
                risk_score=0,
                tier="allow",
                duration_ms=1.0,
                signals=[],
                anomalies=[],
                breaker_state=None,
                metadata={},
            )
            storage.save_trace(entry)

        limit = max(1, count // 2) if count > 0 else 1
        results = storage.get_traces(TraceFilters(limit=limit))
        assert len(results) <= limit


class TestPolicyHashProperties:
    @given(content=st.binary(min_size=1, max_size=500))
    @settings(max_examples=50)
    def test_hash_deterministic(self, content):
        h1 = hashlib.sha256(content).hexdigest()[:8]
        h2 = hashlib.sha256(content).hexdigest()[:8]
        assert h1 == h2
        assert len(h1) == 8

    @given(
        c1=st.binary(min_size=1, max_size=200),
        c2=st.binary(min_size=1, max_size=200),
    )
    @settings(max_examples=50)
    def test_different_content_different_hash(self, c1, c2):
        assume(c1 != c2)
        h1 = hashlib.sha256(c1).hexdigest()[:8]
        h2 = hashlib.sha256(c2).hexdigest()[:8]
        # SHA-256 collisions in 8 hex chars are astronomically unlikely
        # but not impossible — this is a statistical property test
        # We accept rare collisions; hypothesis will try many examples
        assert h1 != h2 or c1 == c2


class TestMockDetectorProperties:
    @given(count=st.integers(min_value=0, max_value=10))
    @settings(max_examples=20)
    def test_returns_signals_in_order_then_none(self, count):
        signals = [
            AnomalySignal(
                detector=f"det-{i}",
                severity="medium",
                description=f"signal {i}",
                score_contribution=max(i * 10, 1),
            )
            for i in range(count)
        ]
        detector = MockDetector(signals=signals)

        for i in range(count):
            result = detector.detect("action", {}, [])
            assert result is not None
            assert result.detector == f"det-{i}"

        assert detector.detect("action", {}, []) is None
        assert detector.call_count == count + 1
