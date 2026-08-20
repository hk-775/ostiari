"""Cost reporter — sends usage data to the control plane after each LLM call.

Calculates cost locally using the quota enforcer's pricing table,
then reports to the control plane with actual cost (not 0.0). Failed batches
remain buffered and are retried on the next flush.
"""

import asyncio
import contextlib
import logging
from typing import Any
from uuid import uuid4

import httpx

from ostiari_gateway.event_outbox import EventOutbox, scoped_stream
from ostiari_gateway.workload_identity import machine_headers

log = logging.getLogger("ostiari.sidecar.llm.cost")


class CostReporter:
    """Reports LLM usage to the control plane's cost API.

    Calculates cost locally using the quota enforcer's pricing,
    then buffers records until the control plane confirms receipt.
    """

    def __init__(
        self,
        control_plane_url: str = "",
        sidecar_id: str = "",
        quota_enforcer: Any = None,
        broker_policy: Any = None,
        shared_store: Any = None,
    ) -> None:
        self._url = control_plane_url.rstrip("/") if control_plane_url else ""
        self._sidecar_id = sidecar_id
        self._quota_enforcer = quota_enforcer
        self._broker_policy = broker_policy
        self._client: httpx.AsyncClient | None = None
        self._buffer: list[dict[str, Any]] = []
        self._buffer_max = 20
        self._flush_lock = asyncio.Lock()
        self._delivery_task: asyncio.Task[None] | None = None
        self._outbox = EventOutbox(
            scoped_stream("costs", sidecar_id),
            id_field="event_id",
            memory=self._buffer,
        )
        if shared_store is not None:
            self._outbox.attach_store(shared_store)

    @staticmethod
    async def _service_headers() -> dict[str, str]:
        return await machine_headers()

    def configure(self, control_plane_url: str, sidecar_id: str) -> None:
        self._outbox.rebind(scoped_stream("costs", sidecar_id))
        self._url = control_plane_url.rstrip("/")
        self._sidecar_id = sidecar_id

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    async def report(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        agent_id: str = "unknown",
        action: str = "",
        provider: str = "",
        experiment_name: str = "",
        experiment_variant: str = "",
        cost_usd: float | None = None,
        record_quota: bool = True,
    ) -> None:
        """Report a single usage record. Buffers and flushes in batches."""
        if not self.enabled:
            return

        # Native /invoke lets this reporter calculate and book cost. The API
        # shims already settle their own reservations, so they provide the exact
        # cost and disable the second quota booking while still emitting usage.
        if cost_usd is None:
            cost_usd = 0.0
            if self._quota_enforcer:
                cost_usd = self._quota_enforcer.calculate_cost(
                    model, input_tokens, output_tokens
                )
        if self._quota_enforcer and record_quota:
            self._quota_enforcer.record_spend(cost_usd)

        self._outbox.enqueue({
            # Generated once at buffer time and retained verbatim across retries.
            # The control plane uses (gateway_id, event_id) as the idempotency key
            # for the usage row, pool debit, and customer charge.
            "event_id": uuid4().hex,
            # The control plane's UsageRecordCreate names this gateway_id; sending
            # sidecar_id (the gateway's internal name for itself) 422s the batch.
            "gateway_id": self._sidecar_id,
            "agent_id": agent_id,
            "model": model,
            "experiment_name": experiment_name,
            "experiment_variant": experiment_variant,
            "provider": provider,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cost_usd": cost_usd,
            "action": action,
        })

        depth = self._outbox.depth()
        if depth is not None and depth >= self._buffer_max:
            await self.flush()

    async def flush(self) -> None:
        """Send oldest records, acknowledging only a confirmed 2xx batch."""
        if not self.enabled:
            return

        async with self._flush_lock:
            pending = self._outbox.pending(count=self._buffer_max)
            if not pending or not self.enabled:
                return

            records = [event.payload for event in pending]
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=5.0)

            try:
                response = await self._client.post(
                    f"{self._url}/api/costs/record/batch",
                    json=records,
                    headers=await self._service_headers(),
                )
                self._apply_broker_snapshot(response)
                response.raise_for_status()
            except Exception as e:
                log.warning(
                    "Failed to report %d cost record(s); retained for retry: %s",
                    len(records),
                    e,
                )
                return

            if not self._outbox.acknowledge(pending):
                log.warning(
                    "Control plane confirmed %d cost record(s), but durable "
                    "acknowledgement failed; idempotent retry will follow",
                    len(records),
                )

    def _apply_broker_snapshot(self, response: httpx.Response) -> None:
        """Adopt pool state even when billing returned a retryable 503."""
        if self._broker_policy is None:
            return
        try:
            payload = response.json()
            pools_by_gateway = payload.get("broker_pools", {})
            pools = pools_by_gateway.get(self._sidecar_id)
            if isinstance(pools, list):
                self._broker_policy.configure(pools)
        except Exception:  # noqa: BLE001 - accounting delivery still decides success
            return

    async def close(self) -> None:
        if self._delivery_task and not self._delivery_task.done():
            self._delivery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._delivery_task
            self._delivery_task = None
        await self.flush()
        if self._client:
            await self._client.aclose()
            self._client = None

    async def start_delivery(self, interval_seconds: float = 2.0) -> None:
        """Drain durable records after restarts even without new LLM traffic."""
        if self._delivery_task and not self._delivery_task.done():
            return

        async def _loop() -> None:
            while True:
                await self.flush()
                await asyncio.sleep(interval_seconds)

        self._delivery_task = asyncio.create_task(_loop())

    def delivery_status(self) -> dict[str, Any]:
        return {"costs": self._outbox.status()}
