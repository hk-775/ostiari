"""Unit tests for the ReportGenerator module."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

from ostiari.models import TraceEntry
from ostiari.report import ReportGenerator


def _make_trace(action="test.run", tier="allow", risk_score=20, rule_triggered=None):
    return TraceEntry(
        trace_id=f"t-{action}-{tier}",
        correlation_id="agent-1",
        timestamp=datetime.now(timezone.utc),
        action=action,
        params={},
        result=None,
        risk_score=risk_score,
        tier=tier,
        duration_ms=5.0,
        signals=[],
        anomalies=[],
        breaker_state=None,
        metadata={"rule_triggered": rule_triggered} if rule_triggered else {},
    )


class TestReportGenerator:
    def test_json_report_with_traces(self):
        mock_storage = MagicMock()
        mock_storage.get_traces.return_value = [
            _make_trace("a.read", "allow", 10),
            _make_trace("b.write", "block", 80, rule_triggered="no-writes"),
        ]

        gen = ReportGenerator(mock_storage)
        data = gen.generate(period_days=7, format="json")
        report = json.loads(data)

        assert report["status"] == "ok"
        assert report["stats"]["total_actions"] == 2
        assert report["stats"]["allowed"] == 1
        assert report["stats"]["blocked"] == 1
        assert "no-writes" in report["evidence"]

    def test_csv_report(self):
        mock_storage = MagicMock()
        mock_storage.get_traces.return_value = [
            _make_trace("x.op", "allow", 15),
        ]

        gen = ReportGenerator(mock_storage)
        data = gen.generate(period_days=7, format="csv")
        text = data.decode("utf-8")

        assert "trace_id" in text
        assert "x.op" in text

    def test_no_activity_report(self):
        mock_storage = MagicMock()
        mock_storage.get_traces.return_value = []

        gen = ReportGenerator(mock_storage)
        data = gen.generate(period_days=7, format="json")
        report = json.loads(data)

        assert report["status"] == "no_activity"
        assert report["stats"]["total_actions"] == 0

    def test_csv_streaming_generator(self):
        traces = [_make_trace(f"action.{i}", "allow", 10) for i in range(3)]
        mock_storage = MagicMock()
        mock_storage.get_traces.side_effect = [traces, []]

        gen = ReportGenerator(mock_storage)
        rows = list(gen.generate_csv_rows(period_days=1))

        assert rows[0].startswith("trace_id,")
        assert len(rows) == 4  # header + 3 data rows

    def test_top_risky_tools(self):
        mock_storage = MagicMock()
        mock_storage.get_traces.return_value = [
            _make_trace("dangerous.tool", "allow", 90),
            _make_trace("dangerous.tool", "allow", 85),
            _make_trace("safe.tool", "allow", 5),
        ]

        gen = ReportGenerator(mock_storage)
        data = gen.generate(period_days=7, format="json")
        report = json.loads(data)

        top = report["stats"]["top_risky_tools"]
        assert top[0]["action"] == "dangerous.tool"
