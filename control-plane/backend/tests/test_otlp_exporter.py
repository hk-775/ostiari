"""Tests for the OTLP governance-trace exporter."""

from unittest.mock import MagicMock

import pytest

from control_plane.services.otlp_exporter import OTLPTraceExporter, _id_from


def _event(**over):
    e = {
        "trace_id": "child1", "parent_trace_id": "sess1", "is_span_root": False,
        "action": "llm.messages", "agent_id": "claude-code", "tier": "block",
        "score": 82, "model": "claude-sonnet-4-6", "framework": "claude-code",
        "session_id": "sess1", "blocked_reason": "risk 82", "limit_type": "risk",
        "timestamp": 1784600000.5,
        "params": {"input_tokens": 100, "output_tokens": 900, "routed": True},
    }
    e.update(over)
    return e


class TestEnableGate:
    def test_disabled_when_endpoint_unset(self, monkeypatch):
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        e = OTLPTraceExporter()
        assert e.enabled is False

    def test_export_is_noop_when_disabled(self, monkeypatch):
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        e = OTLPTraceExporter()
        e.export_event(_event())  # must not raise

    def test_enabled_when_endpoint_set(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        e = OTLPTraceExporter()
        assert e.enabled is True


class TestSpanMapping:
    def test_ids_and_parent(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        e = OTLPTraceExporter()
        e.enabled  # build
        span = e._to_span(_event())
        assert span.context.trace_id == _id_from("sess1", 16)   # session-shared trace
        assert span.context.span_id == _id_from("child1", 8)
        assert span.parent is not None
        assert span.parent.span_id == _id_from("sess1", 8)

    def test_root_has_no_parent(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        e = OTLPTraceExporter()
        e.enabled
        span = e._to_span(_event(trace_id="root1", parent_trace_id="root1", is_span_root=True))
        assert span.parent is None

    def test_attributes(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        e = OTLPTraceExporter()
        e.enabled
        a = dict(e._to_span(_event()).attributes)
        assert a["gen_ai.request.model"] == "claude-sonnet-4-6"
        assert a["gen_ai.usage.output_tokens"] == 900
        assert a["ostiari.tier"] == "block"
        assert a["ostiari.score"] == 82
        assert a["ostiari.blocked_reason"] == "risk 82"
        assert a["ostiari.routed"] is True

    def test_block_maps_to_error_status(self, monkeypatch):
        from opentelemetry.trace.status import StatusCode
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        e = OTLPTraceExporter()
        e.enabled
        assert e._to_span(_event(tier="block")).status.status_code == StatusCode.ERROR
        assert e._to_span(_event(tier="allow")).status.status_code == StatusCode.OK

    def test_export_calls_exporter(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        e = OTLPTraceExporter()
        e.enabled
        e._exporter = MagicMock()
        e.export_event(_event())
        assert e._exporter.export.called
        assert len(e._exporter.export.call_args[0][0]) == 1

    def test_export_swallows_errors(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        e = OTLPTraceExporter()
        e.enabled
        boom = MagicMock()
        boom.export.side_effect = RuntimeError("collector down")
        e._exporter = boom
        e.export_event(_event())  # must not raise — export never breaks ingest


def test_ids_are_nonzero():
    # OTEL forbids all-zero ids; empty input must still yield a valid id
    assert _id_from("", 16) != 0
    assert _id_from("", 8) != 0
