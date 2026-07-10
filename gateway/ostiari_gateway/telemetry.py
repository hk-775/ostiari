"""OpenTelemetry integration for the sidecar.

Extracts trace context from incoming requests, creates spans for validation
and tool proxying, and propagates context to downstream tool endpoints
(whether or not they support OpenTelemetry).
"""

import logging
from typing import Any

from opentelemetry import context, trace
from opentelemetry.context import Context
from opentelemetry.propagate import extract, inject
from opentelemetry.trace import SpanKind, StatusCode, Tracer

log = logging.getLogger("ostiari.sidecar.telemetry")

TRACER_NAME = "ostiari.sidecar"


def get_tracer() -> Tracer:
    """Get the sidecar's tracer instance."""
    return trace.get_tracer(TRACER_NAME)


def extract_context_from_headers(headers: dict[str, str]) -> Context:
    """Extract OTel context from incoming request headers.

    If the caller sent traceparent/tracestate headers, this returns
    a context with the parent span. If not, returns the current context
    (a new root trace will be created).
    """
    return extract(carrier=headers)


def inject_context_into_headers(headers: dict[str, str]) -> dict[str, str]:
    """Inject current OTel context into outgoing headers.

    Adds traceparent/tracestate to the headers dict so the downstream
    tool endpoint can pick them up. If the tool doesn't support OTel,
    these headers are harmlessly ignored.
    """
    inject(carrier=headers)
    return headers


def start_validate_span(
    tracer: Tracer,
    action: str,
    agent_id: str,
    framework: str,
    ctx: Context,
) -> Any:
    """Start a span for the guard.validate() call."""
    return tracer.start_span(
        name=f"ostiari.validate {action}",
        kind=SpanKind.INTERNAL,
        context=ctx,
        attributes={
            "ostiari.action": action,
            "ostiari.agent_id": agent_id,
            "ostiari.framework": framework,
            "ostiari.component": "guard",
        },
    )


def start_proxy_span(
    tracer: Tracer,
    action: str,
    endpoint: str,
    method: str,
    ctx: Context,
) -> Any:
    """Start a span for the tool proxy HTTP call."""
    return tracer.start_span(
        name=f"ostiari.tool.proxy {action}",
        kind=SpanKind.CLIENT,
        context=ctx,
        attributes={
            "ostiari.action": action,
            "ostiari.tool.endpoint": endpoint,
            "ostiari.tool.method": method,
            "http.method": method,
            "http.url": endpoint,
        },
    )


def record_validate_result(
    span: Any,
    tier: str,
    score: int,
    blocked: bool,
) -> None:
    """Record validation outcome on the span."""
    span.set_attribute("ostiari.tier", tier)
    span.set_attribute("ostiari.score", score)
    span.set_attribute("ostiari.blocked", blocked)
    if blocked:
        span.set_status(StatusCode.OK, f"Blocked: score={score}")
    else:
        span.set_status(StatusCode.OK)
    span.end()


def record_proxy_result(
    span: Any,
    status_code: int,
    duration_ms: float,
    error: str | None = None,
) -> None:
    """Record tool proxy outcome on the span."""
    span.set_attribute("http.status_code", status_code)
    span.set_attribute("ostiari.tool.duration_ms", duration_ms)
    if error:
        span.set_attribute("ostiari.tool.error", error)
        span.set_status(StatusCode.ERROR, error)
    else:
        span.set_status(StatusCode.OK)
    span.end()
