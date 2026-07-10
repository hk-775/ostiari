"""Unit tests for the TUI module."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

pytest.importorskip("textual")

from ostiari.models import TraceEntry


def _make_trace(action="test", tier="allow", risk_score=20):
    return TraceEntry(
        trace_id="t1",
        correlation_id=None,
        timestamp=datetime.now(timezone.utc),
        action=action,
        params={},
        result=None,
        risk_score=risk_score,
        tier=tier,
        duration_ms=3.0,
        signals=[],
        anomalies=[],
        breaker_state=None,
        metadata={},
    )


class TestActionStream:
    def test_add_trace(self):
        from ostiari.tui.widgets.stream import ActionStream

        stream = ActionStream()
        trace = _make_trace()
        stream.add_trace(trace)
        assert len(stream._entries) == 1

    def test_max_visible_limit(self):
        from ostiari.tui.widgets.stream import MAX_VISIBLE, ActionStream

        stream = ActionStream()
        for i in range(MAX_VISIBLE + 10):
            stream.add_trace(_make_trace(action=f"action.{i}"))
        assert len(stream._entries) == MAX_VISIBLE


class TestMetricsBar:
    def test_update_from_traces(self):
        from ostiari.tui.widgets.metrics import MetricsBar

        bar = MetricsBar()
        traces = [
            _make_trace(tier="allow", risk_score=10),
            _make_trace(tier="block", risk_score=80),
            _make_trace(tier="allow", risk_score=30),
        ]
        bar.update_from_traces(traces)
        assert bar._total == 3
        assert bar._allowed == 2
        assert bar._blocked == 1


class TestBreakerPanel:
    def test_refresh_no_breakers(self):
        from ostiari.tui.widgets.breakers import BreakerPanel

        panel = BreakerPanel()
        mock_storage = MagicMock()
        mock_storage.get_breaker_state.return_value = None
        panel.refresh_states(mock_storage)


class TestInterventionModal:
    def test_modal_instantiates(self):
        from ostiari.tui.widgets.intervention import InterventionModal

        modal = InterventionModal(action="test.action", risk_score=75.0, question="Allow?")
        assert modal._action == "test.action"
        assert modal._risk_score == 75.0
