"""Structured logging and observability for the LLM-Router.

Provides structured JSON log emission for requests, failures, and startup,
with per-project log level support. Log entries include a ``trace_id`` field
when one is supplied on the entry, so an upstream tracing system can correlate
logs — but this module does not itself emit OpenTelemetry spans or depend on
the opentelemetry SDK. OTEL span export lives in
``observability/otlp_exporter.py`` (standalone) and ``observability/
trace_forwarder.py`` (embedded in Ostiari).
"""

import json
import logging
from datetime import datetime, timezone

from src.gateway.config import DEFAULT_CONFIG
from src.gateway.models import RequestLogEntry


class GatewayLogger:
    """Structured logging for the LLM-Router.

    Emits JSON-formatted log entries for processed requests, provider failures,
    and startup configuration summaries. Supports per-project log levels and
    passes through a ``trace_id`` field when present for log correlation
    (OTEL span emission lives in observability/, not here — see module docstring).
    """

    def __init__(self, default_level: str = DEFAULT_CONFIG.logging.default_level):
        self._default_level = default_level.upper()
        self.logger = logging.getLogger(DEFAULT_CONFIG.logging.logger_name)
        self.logger.setLevel(getattr(logging, self._default_level, logging.INFO))
        self._project_levels: dict[str, str] = {}
        self._project_destinations: dict[str, str] = {}

    def log_request(self, entry: RequestLogEntry) -> None:
        """Emit a structured log entry for a processed request."""
        log_data = {
            "event": "request_completed",
            "request_id": entry.request_id,
            "project_id": entry.project_id,
            "user_id": entry.user_id,
            "model": entry.model,
            "provider": entry.provider,
            "latency_ms": entry.latency_ms,
            "status_code": entry.status_code,
            "prompt_tokens": entry.prompt_tokens,
            "completion_tokens": entry.completion_tokens,
            "total_tokens": entry.total_tokens,
            "cost": entry.cost,
            "timestamp": entry.timestamp.isoformat(),
            "trace_id": entry.trace_id,
            "is_streaming": entry.is_streaming,
            "is_cached": entry.is_cached,
            "retry_count": entry.retry_count,
            "fallback_providers_tried": entry.fallback_providers_tried,
        }
        project_logger = self.get_logger_for_project(entry.project_id)
        project_logger.info(json.dumps(log_data))

    def log_failure(
        self,
        provider: str,
        error_type: str,
        status_code: int,
        retry_attempt: int,
        message: str = "",
    ) -> None:
        """Emit a failure log with diagnostic fields."""
        log_data = {
            "event": "provider_failure",
            "provider": provider,
            "error_type": error_type,
            "status_code": status_code,
            "retry_attempt": retry_attempt,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.logger.error(json.dumps(log_data))

    def log_startup_summary(
        self,
        provider_count: int,
        model_count: int,
        project_count: int,
        routing_strategies: list[str],
    ) -> None:
        """Log startup configuration summary."""
        log_data = {
            "event": "startup",
            "provider_count": provider_count,
            "model_count": model_count,
            "project_count": project_count,
            "routing_strategies": routing_strategies,
        }
        self.logger.info(json.dumps(log_data))

    def set_project_log_level(self, project_id: str, level: str) -> None:
        """Configure per-project log level."""
        self._project_levels[project_id] = level.upper()

    def set_project_log_destination(self, project_id: str, destination: str) -> None:
        """Configure per-project log destination."""
        self._project_destinations[project_id] = destination

    def get_logger_for_project(self, project_id: str) -> logging.Logger:
        """Get a logger configured for a specific project's level."""
        level_name = self._project_levels.get(project_id, self._default_level)
        project_logger = logging.getLogger(f"gateway.project.{project_id}")
        project_logger.setLevel(getattr(logging, level_name, logging.INFO))
        return project_logger
