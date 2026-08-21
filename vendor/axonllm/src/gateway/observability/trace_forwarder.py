"""Forward AxonLLM request traces to an embedding Ostiari instance.

When AxonLLM runs embedded inside Ostiari, each completed request (a UsageRecord)
is forwarded as a trace event so it shows up in Ostiari's Live Traces view. Two
explicitly configured delivery paths may be active:

1. HTTP sink — POST the event to Ostiari's control-plane ingest endpoint
   supplied by the standalone host adapter. Loosely coupled; works across
   processes/containers.
2. In-process sinks — an embedding Ostiari passes sink objects or callables to
   the constructor. No global registration, no Ostiari dependency, and no
   network hop.

Design rules:
- Forwarding is best-effort and MUST NOT affect the request path. Every failure
  is swallowed with a log; a slow/broken Ostiari never slows or fails a chat call.
- AxonLLM is a routing/cost layer, not a risk scorer. It sends neutral risk
  fields (tier="allow", score=0) and puts its real signal (tokens, cost,
  latency, provider) in params/metadata. Ostiari owns risk scoring.
- Standalone AxonLLM is unaffected: with no URL and no injected sink, the
  forwarder is a no-op.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from src.gateway.models import UsageRecord

logger = logging.getLogger("gateway.observability.traces")

class TraceSink(Protocol):
    """Structural subset implemented by ``axonllm.TelemetrySink`` hosts."""

    async def emit(self, event: dict[str, Any]) -> None:
        """Record one mapped trace event."""


Sink = (
    TraceSink
    | Callable[[dict[str, Any]], None]
    | Callable[[dict[str, Any]], Awaitable[None]]
)


def map_usage_to_trace_event(
    record: UsageRecord,
    *,
    gateway_id: str = "axonllm",
) -> dict[str, Any]:
    """Map an AxonLLM UsageRecord to Ostiari's trace-event shape.

    Matches the flat event dict Ostiari's control-plane `/api/traces/ingest`
    expects (see control-plane routers/traces.py). Neutral risk fields; AxonLLM
    specifics carried in params/metadata.
    """
    status = getattr(record, "status", "success")
    # AxonLLM does not compute risk; report a neutral tier. Surface an error tier
    # only when the request itself failed, without inventing a numeric score.
    tier = "error" if status not in ("success", "") else "allow"
    ts = record.timestamp.timestamp() if getattr(record, "timestamp", None) else None

    return {
        "sidecar_id": gateway_id,
        "gateway_id": gateway_id,
        "action": "chat.completion",
        "tier": tier,
        "score": 0,  # AxonLLM is a routing/cost layer; Ostiari owns risk scoring
        "duration_ms": getattr(record, "latency_ms", 0.0),
        "agent_id": record.user_id or "",
        "framework": "axonllm",
        "is_mcp": False,
        "endpoint": "",
        "session_id": "",
        "model": record.model,
        "params": {
            "provider": record.provider,
            "project_id": record.project_id,
            "prompt_tokens": record.prompt_tokens,
            "completion_tokens": record.completion_tokens,
            "total_tokens": record.total_tokens,
            "cached_tokens": getattr(record, "cached_tokens", 0),
            "cost": record.cost,
            "routing_strategy": getattr(record, "routing_strategy", ""),
            "status": status,
        },
        "metadata": {
            "source": "axonllm",
            "request_id": record.request_id,
            # The provider's own id for the upstream call, for cross-referencing
            # provider-side logs. Omitted when the provider didn't supply one.
            "provider_request_id": getattr(record, "provider_request_id", "") or "",
        },
        "timestamp": ts,
    }


class TraceForwarder:
    """Best-effort forwarder of AxonLLM request traces to Ostiari."""

    def __init__(
        self,
        *,
        url: str | None = None,
        gateway_id: str = "axonllm",
        sinks: Iterable[Sink] = (),
        ingest_key: str | None = None,
        timeout_seconds: float = 3.0,
    ) -> None:
        if not isinstance(gateway_id, str) or not gateway_id.strip():
            raise ValueError("gateway_id must be a non-empty string")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._url = url.strip() if url else None
        self._gateway_id = gateway_id.strip()
        self._sinks = tuple(sinks)
        self._ingest_key = ingest_key
        self._timeout_seconds = float(timeout_seconds)
        self._http_client: Any = None

    @property
    def enabled(self) -> bool:
        """True when the host explicitly configured HTTP or in-process delivery."""
        return bool(self._url) or bool(self._sinks)

    def _get_http_client(self) -> Any:
        if self._http_client is None:
            import httpx

            self._http_client = httpx.AsyncClient()
        return self._http_client

    async def forward(self, record: UsageRecord) -> None:
        """Forward one request trace. Never raises — failures are logged only."""
        if not self.enabled:
            return
        try:
            event = map_usage_to_trace_event(
                record,
                gateway_id=self._gateway_id,
            )
        except Exception:
            logger.debug("failed to map usage record to trace event", exc_info=True)
            return

        await self._deliver_to_sinks(event)
        await self._deliver_http(event)

    async def _deliver_to_sinks(self, event: dict[str, Any]) -> None:
        import inspect

        for sink in self._sinks:
            try:
                callback = getattr(sink, "emit", None)
                result = callback(event) if callable(callback) else sink(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.debug("in-process trace sink raised", exc_info=True)

    async def _deliver_http(self, event: dict[str, Any]) -> None:
        if not self._url:
            return
        headers = {"Content-Type": "application/json"}
        if self._ingest_key:
            headers["X-Ingest-Key"] = self._ingest_key
        try:
            client = self._get_http_client()
            resp = await client.post(
                self._url,
                json=event,
                headers=headers,
                timeout=self._timeout_seconds,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "Ostiari trace ingest returned %d for %s",
                    resp.status_code,
                    event.get("metadata", {}).get("request_id"),
                )
        except ImportError:
            logger.warning("httpx not installed — Ostiari trace HTTP forwarding unavailable")
        except Exception:
            logger.debug("Ostiari trace HTTP forward failed", exc_info=True)

    async def close(self) -> None:
        """Close the lazily created HTTP client, if any."""

        client = self._http_client
        self._http_client = None
        close = getattr(client, "aclose", None)
        if callable(close):
            await close()
