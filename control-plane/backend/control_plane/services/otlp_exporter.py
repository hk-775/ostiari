"""Export Ostiari governance traces as OpenTelemetry spans over OTLP.

Ostiari's trace events carry the differentiated governance signal — risk score,
decision tier, per-agent cost, blocked reason, and session parent grouping. This
maps each event into an OTEL span and ships it via OTLP so that signal lands in
whatever observability backend a customer already runs (Datadog, Honeycomb,
Grafana Tempo, Jaeger, an OTEL Collector).

Mapping:
- OTEL trace_id  <- the session parent_trace_id (so a prompt's sub-calls share one trace)
- OTEL span_id   <- the event's own trace_id
- parent span    <- parent_trace_id (None when the event IS the root)
- attributes     <- gen_ai.* where OTEL conventions exist (model, tokens),
                    ostiari.* for the governance-specific fields (tier, risk
                    score, decision, blocked reason, routed).

No-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set. Degrades gracefully (disabled)
if the OTLP exporter package isn't installed.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

log = logging.getLogger("control_plane.otlp")


def _id_from(text: str, nbytes: int) -> int:
    """Deterministic OTEL id (trace=16B, span=8B) from an Ostiari id string."""
    h = hashlib.sha256((text or "").encode()).digest()[:nbytes]
    val = int.from_bytes(h, "big")
    return val or 1  # OTEL forbids all-zero ids


class OTLPTraceExporter:
    """Lazily-built OTLP exporter for Ostiari governance trace events."""

    def __init__(self) -> None:
        self._exporter: Any = None
        self._resource: Any = None
        self._built = False
        self._enabled = False

    @property
    def enabled(self) -> bool:
        self._ensure()
        return self._enabled

    def _ensure(self) -> None:
        if self._built:
            return
        self._built = True
        if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
            return  # opt-in: no endpoint configured
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource

            self._exporter = OTLPSpanExporter()  # reads OTEL_EXPORTER_OTLP_* env
            self._resource = Resource.create({"service.name": os.environ.get(
                "OTEL_SERVICE_NAME", "ostiari")})
            self._enabled = True
            log.info("OTLP trace export enabled → %s",
                     os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))
        except Exception as e:  # noqa: BLE001 — missing pkg / bad config => disabled
            self._exporter = None
            self._enabled = False
            log.warning("OTLP export unavailable (%s) — disabled", e)

    def export_event(self, event: dict[str, Any]) -> None:
        """Map one Ostiari trace event to an OTEL span and export it (best-effort)."""
        self._ensure()
        if not self._enabled or self._exporter is None:
            return
        try:
            span = self._to_span(event)
            self._exporter.export([span])
        except Exception as e:  # noqa: BLE001 — export must never break ingest
            log.debug("OTLP export failed: %s", e)

    def _to_span(self, event: dict[str, Any]) -> Any:
        from opentelemetry.sdk.trace import ReadableSpan
        from opentelemetry.trace import SpanContext, SpanKind, TraceFlags
        from opentelemetry.trace.status import Status, StatusCode

        tid = event.get("trace_id", "")
        parent_tid = event.get("parent_trace_id") or tid
        is_root = event.get("is_span_root", parent_tid == tid)

        otel_trace_id = _id_from(parent_tid, 16)   # whole session shares this trace
        otel_span_id = _id_from(tid, 8)

        ctx = SpanContext(trace_id=otel_trace_id, span_id=otel_span_id,
                          is_remote=False, trace_flags=TraceFlags(TraceFlags.SAMPLED))
        parent_ctx = None
        if not is_root:
            parent_ctx = SpanContext(trace_id=otel_trace_id,
                                     span_id=_id_from(parent_tid, 8),
                                     is_remote=False,
                                     trace_flags=TraceFlags(TraceFlags.SAMPLED))

        params = event.get("params") or {}
        tier = event.get("tier", "allow")
        attrs: dict[str, Any] = {
            # GenAI semantic conventions (stable-ish subset)
            "gen_ai.request.model": event.get("model", ""),
            "gen_ai.usage.input_tokens": int(params.get("input_tokens", 0) or 0),
            "gen_ai.usage.output_tokens": int(params.get("output_tokens", 0) or 0),
            # Ostiari governance-specific attributes (no OTEL standard exists)
            "ostiari.action": event.get("action", ""),
            "ostiari.agent_id": event.get("agent_id", ""),
            "ostiari.tier": tier,
            "ostiari.score": int(event.get("score", 0) or 0),
            "ostiari.framework": event.get("framework", ""),
            "ostiari.session_id": event.get("session_id", "") or "",
            "ostiari.routed": bool(params.get("routed", False)),
            "ostiari.trace_id": tid,
        }
        if event.get("blocked_reason"):
            attrs["ostiari.blocked_reason"] = event["blocked_reason"]
        if event.get("limit_type"):
            attrs["ostiari.limit_type"] = event["limit_type"]

        ts = event.get("timestamp")
        start_ns = int(float(ts) * 1e9) if ts else None
        status = Status(StatusCode.ERROR if tier == "block" else StatusCode.OK)

        return ReadableSpan(
            name=event.get("action", "llm.call"),
            context=ctx,
            parent=parent_ctx,
            resource=self._resource,
            attributes=attrs,
            kind=SpanKind.CLIENT,
            status=status,
            start_time=start_ns,
            end_time=start_ns,
        )


exporter = OTLPTraceExporter()
