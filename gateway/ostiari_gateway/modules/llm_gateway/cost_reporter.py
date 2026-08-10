"""Cost reporter — sends usage data to the control plane after each LLM call.

Calculates cost locally using the quota enforcer's pricing table,
then reports to the control plane with actual cost (not 0.0). Failed batches
remain buffered and are retried on the next flush.
"""

import asyncio
import logging
import os
from typing import Any

import httpx

log = logging.getLogger("ostiari.sidecar.llm.cost")


class CostReporter:
    """Reports LLM usage to the control plane's cost API.

    Calculates cost locally using the quota enforcer's pricing,
    then buffers records until the control plane confirms receipt.
    """

    def __init__(self, control_plane_url: str = "", sidecar_id: str = "", quota_enforcer: Any = None) -> None:
        self._url = control_plane_url.rstrip("/") if control_plane_url else ""
        self._sidecar_id = sidecar_id
        self._quota_enforcer = quota_enforcer
        self._client: httpx.AsyncClient | None = None
        self._buffer: list[dict[str, Any]] = []
        self._buffer_max = 20
        self._flush_lock = asyncio.Lock()

    @staticmethod
    def _service_headers() -> dict[str, str]:
        token = os.environ.get("OSTIARI_SERVICE_TOKEN", "").strip()
        return {"X-Ostiari-Service-Key": token} if token else {}

    def configure(self, control_plane_url: str, sidecar_id: str) -> None:
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

        self._buffer.append({
            # The control plane's UsageRecordCreate names this gateway_id; sending
            # sidecar_id (the gateway's internal name for itself) 422s the batch.
            "gateway_id": self._sidecar_id,
            "agent_id": agent_id,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cost_usd": cost_usd,
            "action": action,
        })

        if len(self._buffer) >= self._buffer_max:
            await self.flush()

    async def flush(self) -> None:
        """Send buffered records, retaining the batch until a 2xx response."""
        if not self._buffer or not self.enabled:
            return

        async with self._flush_lock:
            if not self._buffer or not self.enabled:
                return

            records = self._buffer[:]
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=5.0)

            try:
                response = await self._client.post(
                    f"{self._url}/api/costs/record/batch",
                    json=records,
                    headers=self._service_headers(),
                )
                response.raise_for_status()
            except Exception as e:
                log.warning(
                    "Failed to report %d cost record(s); retained for retry: %s",
                    len(records),
                    e,
                )
                return

            # Records may have been appended while the request was in flight.
            # Remove only the confirmed snapshot and leave newer entries queued.
            del self._buffer[:len(records)]

    async def close(self) -> None:
        await self.flush()
        if self._client:
            await self._client.aclose()
            self._client = None
