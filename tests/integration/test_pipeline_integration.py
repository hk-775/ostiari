"""Integration tests for the full action pipeline."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from ostiari.decorators import protect, reset_guard
from ostiari.exceptions import ActionBlockedError, ActionInterventionTimeout
from ostiari.guard import Guard
from ostiari.models import (
    OstiariConfig,
    PolicyResult,
    RiskAdjustment,
    Rule,
    ThresholdConfig,
)
from ostiari.policy import PolicyEngine


@pytest.fixture(autouse=True)
def cleanup():
    yield
    reset_guard()


@pytest.fixture
def mock_storage():
    storage = MagicMock()
    storage.get_traces.return_value = []
    storage.save_traces_batch.return_value = None
    return storage


class TestFullPipeline:
    def test_allow_path_end_to_end(self, mock_storage):
        with Guard(storage=mock_storage) as g:
            result = g.validate("read_file", {"path": "/tmp/test.txt"})
            assert result.tier == "allow"
            assert result.score == 0
            assert g.tracer.queue_depth == 1

    def test_block_path_with_policy(self, mock_storage):
        engine = MagicMock(spec=PolicyEngine)
        engine.evaluate.return_value = PolicyResult(
            decision="block",
            blocked_by=Rule(type="block", action="rm_rf"),
        )
        with Guard(policy_engine=engine, storage=mock_storage) as g:
            with pytest.raises(ActionBlockedError) as exc_info:
                g.validate("rm_rf", {"path": "/"})
            assert exc_info.value.score == 100
            # Trace still recorded for blocked actions
            assert g.tracer.queue_depth == 1

    def test_intervention_with_real_thread_approve(self, mock_storage):
        engine = MagicMock(spec=PolicyEngine)
        engine.evaluate.return_value = PolicyResult(
            decision="evaluate",
            risk_adjustments=[
                RiskAdjustment(
                    delta=50,
                    source_rule=Rule(type="risk_adjust", action="*", risk_adjust=50),
                    reason="risky",
                )
            ],
        )
        config = OstiariConfig(thresholds=ThresholdConfig(allow_max=30, intervene_max=70))

        def real_callback(action, params, score):
            time.sleep(0.05)  # simulate real work
            return True

        with Guard(config=config, policy_engine=engine, storage=mock_storage) as g:
            g.gateway.set_intervention_callback(real_callback)
            result = g.validate("delete_file", {"path": "/data"})
            assert result.tier == "allow"
            assert result.score == 50

    def test_intervention_with_real_thread_timeout(self, mock_storage):
        engine = MagicMock(spec=PolicyEngine)
        engine.evaluate.return_value = PolicyResult(
            decision="evaluate",
            risk_adjustments=[
                RiskAdjustment(
                    delta=50,
                    source_rule=Rule(type="risk_adjust", action="*", risk_adjust=50),
                    reason="risky",
                )
            ],
        )
        config = OstiariConfig(thresholds=ThresholdConfig(allow_max=30, intervene_max=70))

        def slow_callback(action, params, score):
            time.sleep(5)
            return True

        with Guard(config=config, policy_engine=engine, storage=mock_storage) as g:
            g.gateway._intervention_timeout = 0.1
            g.gateway.set_intervention_callback(slow_callback)
            with pytest.raises(ActionInterventionTimeout):
                g.validate("action", {})


class TestDecoratorIntegration:
    def test_decorator_full_pipeline(self, mock_storage):
        @protect()
        def safe_function(x: int) -> int:
            return x + 1

        result = safe_function(5)
        assert result == 6

    def test_tracer_flushes_to_storage(self, mock_storage):
        with Guard(storage=mock_storage) as g:
            for _ in range(3):
                g.validate("action", {})
            g.tracer._flush_all()
        mock_storage.save_traces_batch.assert_called()
        total_flushed = sum(
            len(call[0][0]) for call in mock_storage.save_traces_batch.call_args_list
        )
        assert total_flushed == 3
