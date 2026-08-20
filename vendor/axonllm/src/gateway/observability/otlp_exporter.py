"""Export AxonLLM request telemetry as OpenTelemetry spans over OTLP.

This is the STANDALONE observability path. Each completed request (a
``UsageRecord``) is mapped to one OTEL span carrying the routing/cost signal —
model, provider, token usage, cost, latency — using the GenAI semantic
conventions where they exist (``gen_ai.*``) plus ``axon.*`` for the fields OTEL
has no standard for (provider, cost, routing strategy).

Relationship to Ostiari (important — avoids double-export):
- Standalone AxonLLM: this exporter emits the span directly to the customer's
  OTLP backend (Datadog, Honeycomb, Tempo, Jaeger, an OTEL Collector).
- Embedded in Ostiari: AxonLLM's ``TraceForwarder`` already forwards each request
  to Ostiari, whose own OTLP exporter emits the span WITH the governance signal
  (risk tier, decision, session parent grouping). So when embedded this exporter
  SUPPRESSES itself — the agent only calls it when not embedded — giving exactly
  one span per request in either mode.

Design rules (mirrors trace_forwarder + Ostiari's otlp_exporter):
- No-op unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set (opt-in).
- Degrades gracefully to disabled if the opentelemetry packages aren't installed.
- Best-effort: export MUST NEVER raise into or slow the request path. Spans are
  handed to a BatchSpanProcessor, which queues them and ships them on a
  background worker thread — so the OTLP HTTP round-trip (and its retries on a
  down collector) never block the chat request.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.gateway.models import UsageRecord

log = logging.getLogger("gateway.observability.otlp")


def _id_from(text: str, nbytes: int) -> int:
    """Deterministic OTEL id (trace=16B, span=8B) from an id string.

    Same scheme as Ostiari's exporter, so a request that also flows through
    Ostiari would map to the same ids — trace correlation across the two layers.
    """
    h = hashlib.sha256((text or "").encode()).digest()[:nbytes]
    val = int.from_bytes(h, "big")
    return val or 1  # OTEL forbids all-zero ids


class OTLPSpanExporter:
    """Lazily-built OTLP exporter for AxonLLM request telemetry."""

    def __init__(self) -> None:
        self._exporter: Any = None      # raw OTLPSpanExporter (used in tests / flush)
        self._processor: Any = None     # BatchSpanProcessor — non-blocking queue
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
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter as _Exp
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            self._exporter = _Exp()  # reads OTEL_EXPORTER_OTLP_* env
            # BatchSpanProcessor owns a background worker thread: on_end() just
            # enqueues, so a slow/down collector never blocks the request path.
            self._processor = BatchSpanProcessor(self._exporter)
            self._resource = Resource.create({"service.name": os.environ.get(
                "OTEL_SERVICE_NAME", "axonllm")})
            self._enabled = True
            log.info("OTLP trace export enabled → %s",
                     os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))
        except Exception as e:  # noqa: BLE001 — missing pkg / bad config => disabled
            self._exporter = None
            self._processor = None
            self._enabled = False
            log.warning("OTLP export unavailable (%s) — disabled", e)

    def export_usage(self, record: UsageRecord) -> None:
        """Map one UsageRecord to a span and enqueue it (non-blocking, best-effort)."""
        self._ensure()
        if not self._enabled or self._processor is None:
            return
        try:
            self._processor.on_end(self._to_span(record))  # enqueue → returns immediately
        except Exception as e:  # noqa: BLE001 — export must never break the request
            log.debug("OTLP export failed: %s", e)

    def shutdown(self) -> None:
        """Flush and stop the background processor (call on graceful shutdown)."""
        if self._processor is not None:
            try:
                self._processor.shutdown()
            except Exception as e:  # noqa: BLE001
                log.debug("OTLP processor shutdown failed: %s", e)

    def _to_span(self, record: UsageRecord) -> Any:
        from opentelemetry.sdk.trace import ReadableSpan
        from opentelemetry.trace import SpanContext, SpanKind, TraceFlags
        from opentelemetry.trace.status import Status, StatusCode

        # One span per request; request_id anchors the trace/span ids.
        rid = getattr(record, "request_id", "") or ""
        otel_trace_id = _id_from(rid, 16)
        otel_span_id = _id_from(rid, 8)
        ctx = SpanContext(trace_id=otel_trace_id, span_id=otel_span_id,
                          is_remote=False, trace_flags=TraceFlags(TraceFlags.SAMPLED))

        status_str = getattr(record, "status", "success") or "success"
        attrs: dict[str, Any] = {
            # GenAI semantic conventions (shared with Ostiari's exporter)
            "gen_ai.request.model": record.model,
            "gen_ai.usage.input_tokens": int(record.prompt_tokens or 0),
            "gen_ai.usage.output_tokens": int(record.completion_tokens or 0),
            # AxonLLM-specific — OTEL has no standard for these
            "axon.provider": record.provider or "",
            "axon.project_id": record.project_id or "",
            "axon.cost_usd": float(record.cost or 0.0),
            "axon.total_tokens": int(record.total_tokens or 0),
            "axon.cached_tokens": int(getattr(record, "cached_tokens", 0) or 0),
            "axon.routing_strategy": getattr(record, "routing_strategy", "") or "",
            "axon.request_id": rid,
            "axon.status": status_str,
        }
        # Provider's own id for the upstream call — set only when supplied, so a
        # provider that omits it doesn't add an empty attribute to every span.
        provider_rid = getattr(record, "provider_request_id", "") or ""
        if provider_rid:
            attrs["axon.provider_request_id"] = provider_rid

        ts = record.timestamp.timestamp() if getattr(record, "timestamp", None) else None
        start_ns = int(ts * 1e9) if ts else None
        latency_ns = int(float(getattr(record, "latency_ms", 0.0) or 0.0) * 1e6)
        end_ns = (start_ns + latency_ns) if start_ns is not None else None
        status = Status(StatusCode.OK if status_str in ("success", "") else StatusCode.ERROR)

        return ReadableSpan(
            name="llm.completion",
            context=ctx,
            parent=None,  # standalone: AxonLLM's span is the root
            resource=self._resource,
            attributes=attrs,
            kind=SpanKind.CLIENT,
            status=status,
            start_time=start_ns,
            end_time=end_ns,
        )


# Module-level singleton, mirroring Ostiari's `exporter` convention.
exporter = OTLPSpanExporter()
