"""Integration tests for extensibility features (adapters, policy poller, custom detectors)."""

from __future__ import annotations

import hashlib
import time
from unittest.mock import MagicMock, patch

import pytest

from ostiari.exceptions import ActionBlockedError, AdapterValidationError
from ostiari.guard import Guard
from ostiari.models import AnomalySignal, TraceFilters
from ostiari.testing import MockAdapter, MockDetector, MockStorage


@pytest.fixture
def storage():
    return MockStorage()


class TestAdapterEndToEnd:
    def test_guard_with_adapter_allow_path(self, storage):
        adapter = MockAdapter(name="integration-test")
        with Guard(storage=storage, adapter=adapter) as g:
            result = g.validate("file.read", {"path": "/tmp/test.txt"})

        assert result.tier == "allow"
        assert len(adapter.pre_hook_calls) == 1
        assert adapter.pre_hook_calls[0] == ("file.read", {"path": "/tmp/test.txt"})
        assert len(adapter.post_hook_calls) == 1

    def test_guard_with_adapter_block_path(self, storage):
        from ostiari.policy import PolicyEngine

        adapter = MockAdapter(name="block-test")
        engine = PolicyEngine()
        content = b"block:\n  - '*.delete'"
        engine.reload_from_content(content, source="test")

        with Guard(storage=storage, adapter=adapter, policy_engine=engine) as g:
            with pytest.raises(ActionBlockedError):
                g.validate("db.delete", {"table": "users"})

        assert len(adapter.pre_hook_calls) == 1

    def test_multiple_adapters_called_in_order(self, storage):
        call_order: list[str] = []

        class OrderedAdapter(MockAdapter):
            def wrap_tool_call(self, tool, params):
                call_order.append(self.name)
                return super().wrap_tool_call(tool, params)

        a1 = OrderedAdapter(name="first")
        a2 = OrderedAdapter(name="second")
        a3 = OrderedAdapter(name="third")

        with Guard(storage=storage, adapter=[a1, a2, a3]) as g:
            g.validate("test.action", {})

        assert call_order == ["first", "second", "third"]
        assert len(a1.pre_hook_calls) == 1
        assert len(a2.pre_hook_calls) == 1
        assert len(a3.pre_hook_calls) == 1

    def test_adapter_failure_does_not_block_pipeline(self, storage):
        class FailingAdapter(MockAdapter):
            def wrap_tool_call(self, tool, params):
                raise RuntimeError("adapter crashed")

        failing = FailingAdapter(name="broken")
        working = MockAdapter(name="working")

        with Guard(storage=storage, adapter=[failing, working]) as g:
            result = g.validate("safe.action", {})

        assert result.tier == "allow"
        assert len(working.pre_hook_calls) == 1

    def test_invalid_adapter_rejected_at_registration(self, storage):
        class BadAdapter:
            pass

        with Guard(storage=storage) as g, pytest.raises(AdapterValidationError):
            g.register_adapter(BadAdapter())


class TestCustomDetectorEndToEnd:
    def test_guard_with_custom_detector(self, storage):
        signal = AnomalySignal(
            detector="custom-test",
            severity="medium",
            description="suspicious pattern",
            score_contribution=30,
        )
        detector = MockDetector(signals=[signal])

        with Guard(storage=storage) as g:
            g.register_detector(detector)
            result = g.validate("risky.action", {"data": "sensitive"})

        assert result.score >= 30
        assert detector.call_count == 1

    def test_custom_detector_failure_isolated(self, storage):
        class CrashingDetector:
            @property
            def name(self):
                return "crasher"

            def detect(self, action, params, history):
                raise ValueError("detector exploded")

        with Guard(storage=storage) as g:
            g.register_detector(CrashingDetector())
            result = g.validate("normal.action", {})

        assert result.tier == "allow"


class TestCustomStorageEndToEnd:
    def test_guard_with_mock_storage_records_traces(self, storage):
        with Guard(storage=storage) as g:
            g.validate("action.one", {})
            g.validate("action.two", {"key": "val"})

        time.sleep(0.3)
        g.tracer._flush_all()
        traces = storage.get_traces(TraceFilters(limit=100))
        assert len(traces) >= 2
        actions = [t.action for t in traces]
        assert "action.one" in actions
        assert "action.two" in actions


class TestPolicyVersionInTraceMetadata:
    def test_policy_version_recorded(self, storage):
        from ostiari.policy import PolicyEngine

        engine = PolicyEngine()
        content = b"rules: []"
        engine.reload_from_content(content, source="file:///test/policy.yaml")
        expected_hash = hashlib.sha256(content).hexdigest()[:8]

        with Guard(storage=storage, policy_engine=engine) as g:
            g.validate("test.action", {})

        time.sleep(0.3)
        g.tracer._flush_all()
        traces = storage.get_traces(TraceFilters(limit=10))
        assert len(traces) >= 1
        metadata = traces[0].metadata
        assert metadata["policy_version"] == expected_hash
        assert metadata["policy_source"] == "file:///test/policy.yaml"


class TestPolicyPollerIntegration:
    def test_guard_starts_poller_with_policy_source(self, storage):
        with patch("ostiari.policy.poller.get_fetcher") as mock_get:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch.return_value = b"rules: []"
            mock_get.return_value = mock_fetcher

            g = Guard(storage=storage, policy_source="file:///tmp/policy.yaml")
            g.start()
            assert g._policy_poller is not None
            assert g._policy_poller.is_running
            g.shutdown()
            assert not g._policy_poller

    def test_guard_without_policy_source_no_poller(self, storage):
        with Guard(storage=storage) as g:
            assert g._policy_poller is None
