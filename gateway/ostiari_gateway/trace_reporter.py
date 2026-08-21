"""Trace reporter — sends tool call events to the control plane in real-time.

Also handles per-agent spend persistence: pushes snapshots periodically
and restores on gateway startup.
"""

import asyncio
import contextlib
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from ostiari_gateway.event_outbox import EventOutbox, scoped_stream
from ostiari_gateway.workload_identity import machine_headers

log = logging.getLogger("ostiari.sidecar.traces")


class TraceReporter:
    """Reports tool call events to the control plane for the live trace viewer.

    Sends immediately (not batched) for real-time visibility.
    Fire-and-forget — never blocks the agent response.

    Also persists per-agent spend to the Control Plane so budgets
    survive gateway restarts.
    """

    def __init__(self, control_plane_url: str = "", sidecar_id: str = "") -> None:
        self._url = control_plane_url.rstrip("/") if control_plane_url else ""
        self._sidecar_id = sidecar_id
        self._client: httpx.AsyncClient | None = None
        self._spend_task: asyncio.Task[None] | None = None
        self._delivery_task: asyncio.Task[None] | None = None
        self._agent_auth: Any = None
        self._pending_reset_at: str | None = None
        self._trace_buffer: list[dict[str, Any]] = []
        self._payment_buffer: list[dict[str, Any]] = []
        self._alert_buffer: list[dict[str, Any]] = []
        self._flush_locks = {
            "traces": asyncio.Lock(),
            "payments": asyncio.Lock(),
            "budget_alerts": asyncio.Lock(),
        }
        self._trace_outbox = EventOutbox(
            scoped_stream("traces", sidecar_id),
            id_field="trace_id",
            memory=self._trace_buffer,
        )
        self._payment_outbox = EventOutbox(
            scoped_stream("payments", sidecar_id),
            id_field="event_id",
            memory=self._payment_buffer,
        )
        self._alert_outbox = EventOutbox(
            scoped_stream("budget_alerts", sidecar_id),
            id_field="event_id",
            memory=self._alert_buffer,
        )

    @staticmethod
    async def _service_headers() -> dict[str, str]:
        return await machine_headers()

    @staticmethod
    async def _ingest_headers() -> dict[str, str]:
        return await machine_headers(legacy="ingest")

    def configure(self, control_plane_url: str, sidecar_id: str) -> None:
        self._trace_outbox.rebind(scoped_stream("traces", sidecar_id))
        self._payment_outbox.rebind(scoped_stream("payments", sidecar_id))
        self._alert_outbox.rebind(scoped_stream("budget_alerts", sidecar_id))
        self._url = control_plane_url.rstrip("/")
        self._sidecar_id = sidecar_id

    def set_agent_auth(self, agent_auth: Any) -> None:
        """Wire the AgentAuthPolicy for spend persistence."""
        self._agent_auth = agent_auth

    def attach_shared_store(self, store: Any) -> None:
        if store is None:
            return
        self._trace_outbox.attach_store(store)
        self._payment_outbox.attach_store(store)
        self._alert_outbox.attach_store(store)

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    async def report(
        self,
        action: str,
        tier: str,
        score: int,
        duration_ms: float,
        agent_id: str = "unknown",
        framework: str = "unknown",
        is_mcp: bool = False,
        blocked_reason: str | None = None,
        endpoint: str = "",
        session_id: str = "",
        plan: str = "",
        step: str = "",
        params: dict[str, Any] | None = None,
        model: str = "",
        shadow: bool = False,
        would_block: bool = False,
        delegation_chain: list[str] | None = None,
        limit_type: str = "",
    ) -> None:
        """Report a single tool call event to the control plane.

        shadow: the gateway was in shadow mode for this call (no real enforcement).
        would_block: in shadow mode, this call WOULD have been blocked in enforce mode.
        """
        if not self.enabled:
            return

        event = {
            # Stable, unique identifier for this trace — the durable handle that
            # dedup, deep-links, and cross-references (decision/HITL/payment/audit)
            # hang off. Stamped at the gateway so it's end-to-end consistent.
            "trace_id": uuid.uuid4().hex,
            "sidecar_id": self._sidecar_id,
            # Same value under the name consumers actually read (the trace
            # viewer's Gateway column, delegation reports). Sending only
            # sidecar_id left that column blank for every live trace.
            "gateway_id": self._sidecar_id,
            "action": action,
            "tier": tier,
            "score": score,
            "duration_ms": round(duration_ms, 2),
            "agent_id": agent_id,
            "framework": framework,
            "is_mcp": is_mcp,
            "blocked_reason": blocked_reason,
            "endpoint": endpoint,
            "session_id": session_id,
            "plan": plan,
            "step": step,
            "params": params,
            "model": model,
            "shadow": shadow,
            "would_block": would_block,
            "delegation_chain": delegation_chain or [],
            "limit_type": limit_type,
            "timestamp": time.time(),
        }

        self._trace_outbox.enqueue(event)
        await self.flush_traces()

    async def flush_traces(self) -> None:
        """Deliver trace events in order and retain unconfirmed events."""
        await self._flush_single_event_queue(
            self._trace_outbox,
            lock=self._flush_locks["traces"],
            path="/api/traces/ingest",
            headers=await self._ingest_headers(),
            label="trace",
        )

    async def report_payment(
        self,
        *,
        agent_id: str,
        action: str,
        amount_usdc: float,
        settled: bool,
        tx_hash: str = "",
        mode: str = "simulated",
        source: str = "policy",
        reason: str = "",
        event_id: str = "",
        wallet_debited: bool | None = None,
    ) -> None:
        """Report an x402 charge (settled or blocked) to the control-plane ledger.

        Failed deliveries remain queued with the same event id and retry on the
        next payment, persistence tick, or graceful shutdown.
        """
        if not self.enabled:
            return
        self._payment_outbox.enqueue({
            "event_id": event_id or uuid.uuid4().hex,
            "agent_id": agent_id,
            "gateway_id": self._sidecar_id,
            "action": action,
            "amount_usdc": amount_usdc,
            "settled": settled,
            "wallet_debited": settled if wallet_debited is None else wallet_debited,
            "tx_hash": tx_hash,
            "mode": mode,
            "source": source,
            "reason": reason,
        })
        await self.flush_payments()

    async def flush_payments(self) -> None:
        """Deliver queued ledger events in order, retaining failed events."""
        await self._flush_single_event_queue(
            self._payment_outbox,
            lock=self._flush_locks["payments"],
            path="/api/payments/ingest",
            headers=await self._service_headers(),
            label="payment",
        )

    async def report_budget_alert(
        self,
        *,
        threshold: str,
        spend_usd: float,
        budget_usd: float,
        agent_id: str = "",
    ) -> None:
        """Report a crossed budget threshold to the control plane.

        Fire-and-forget, like trace reporting. The gateway logs the alert either
        way; this is what makes it visible to an operator who isn't tailing logs.
        """
        if not self.enabled:
            return
        self._alert_outbox.enqueue({
            "event_id": uuid.uuid4().hex,
            "gateway_id": self._sidecar_id,
            "agent_id": agent_id,
            "threshold": threshold,
            "spend_usd": round(spend_usd, 4),
            "budget_usd": budget_usd,
            "timestamp": time.time(),
        })
        await self.flush_budget_alerts()

    async def flush_budget_alerts(self) -> None:
        await self._flush_single_event_queue(
            self._alert_outbox,
            lock=self._flush_locks["budget_alerts"],
            path="/api/quotas/alerts",
            headers=await self._service_headers(),
            label="budget alert",
        )

    async def _flush_single_event_queue(
        self,
        outbox: EventOutbox,
        *,
        lock: asyncio.Lock,
        path: str,
        headers: dict[str, str],
        label: str,
    ) -> None:
        if not self.enabled:
            return
        async with lock:
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=3.0)
            while True:
                pending = outbox.pending(count=1)
                if not pending:
                    return
                event = pending[0]
                try:
                    response = await self._client.post(
                        f"{self._url}{path}",
                        json=event.payload,
                        headers=headers,
                    )
                    if not 200 <= response.status_code < 300:
                        raise RuntimeError(
                            f"control plane returned HTTP {response.status_code}"
                        )
                except Exception as e:
                    log.warning(
                        "Failed to report %s %s; retained for retry: %s",
                        label,
                        event.event_id,
                        e,
                    )
                    return
                if not outbox.acknowledge([event]):
                    log.warning(
                        "Control plane confirmed %s %s, but durable "
                        "acknowledgement failed; idempotent retry will follow",
                        label,
                        event.event_id,
                    )
                    return

    async def push_spend_snapshot(
        self,
        *,
        reset: bool = False,
        reset_at: str | None = None,
    ) -> None:
        """Push current per-agent spend to the Control Plane for persistence."""
        if not self.enabled or not self._agent_auth:
            return
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=3.0)

        if reset:
            self._pending_reset_at = reset_at or datetime.now(timezone.utc).isoformat()
        pending_reset_at = self._pending_reset_at
        snapshot = self._agent_auth.get_spend_snapshot(
            include_zero=pending_reset_at is not None
        )
        if not snapshot and pending_reset_at is None:
            return

        try:
            response = await self._client.post(
                f"{self._url}/api/gateways/{self._sidecar_id}/spend",
                json={
                    "spend": snapshot,
                    "reset": pending_reset_at is not None,
                    "reset_at": pending_reset_at,
                },
                headers=await self._service_headers(),
            )
            response.raise_for_status()
            if self._pending_reset_at == pending_reset_at:
                self._pending_reset_at = None
            log.debug("Pushed spend snapshot: %d agents", len(snapshot))
        except Exception as e:
            log.debug("Failed to push spend snapshot: %s", e)

    async def restore_spend_from_control_plane(self) -> None:
        """Restore per-agent spend from the Control Plane on startup."""
        if not self.enabled or not self._agent_auth:
            return
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=3.0)

        try:
            resp = await self._client.get(
                f"{self._url}/api/gateways/{self._sidecar_id}/spend",
                headers=await self._service_headers(),
            )
            if resp.status_code == 200:
                data = resp.json()
                snapshot = data.get("spend", {})
                if snapshot:
                    self._agent_auth.restore_spend(snapshot)
                    log.info("Restored spend from Control Plane: %d agents", len(snapshot))
        except Exception as e:
            log.debug("Failed to restore spend: %s (starting fresh)", e)

    async def start_spend_persistence(self, interval_seconds: float = 30.0) -> None:
        """Start a background task that pushes spend snapshots periodically."""
        if self._spend_task and not self._spend_task.done():
            return
        await self.restore_spend_from_control_plane()

        async def _loop() -> None:
            while True:
                await asyncio.sleep(interval_seconds)
                await self.push_spend_snapshot()

        self._spend_task = asyncio.create_task(_loop())
        log.info("Spend persistence started (every %.0fs)", interval_seconds)

    async def start_delivery(self, interval_seconds: float = 2.0) -> None:
        """Drain durable events after restarts without waiting for new traffic."""
        if self._delivery_task and not self._delivery_task.done():
            return

        async def _loop() -> None:
            while True:
                await self.flush_traces()
                await self.flush_payments()
                await self.flush_budget_alerts()
                await asyncio.sleep(interval_seconds)

        self._delivery_task = asyncio.create_task(_loop())

    async def close(self) -> None:
        for task_name in ("_spend_task", "_delivery_task"):
            task = getattr(self, task_name)
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            setattr(self, task_name, None)
        if self._agent_auth:
            await self.push_spend_snapshot()
        await self.flush_traces()
        await self.flush_payments()
        await self.flush_budget_alerts()
        if self._client:
            await self._client.aclose()
            self._client = None

    def delivery_status(self) -> dict[str, dict[str, Any]]:
        return {
            "traces": self._trace_outbox.status(),
            "payments": self._payment_outbox.status(),
            "budget_alerts": self._alert_outbox.status(),
        }
