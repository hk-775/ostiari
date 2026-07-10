"""Cost reporter — sends usage data to the control plane after each LLM call.

Calculates cost locally using the quota enforcer's pricing table,
then reports to the control plane with actual cost (not 0.0).
"""

import logging
from typing import Any

import httpx

log = logging.getLogger("ostiari.sidecar.llm.cost")


class CostReporter:
    """Reports LLM usage to the control plane's cost API.

    Calculates cost locally using the quota enforcer's pricing,
    then fires-and-forgets to the control plane.
    """

    def __init__(self, control_plane_url: str = "", sidecar_id: str = "", quota_enforcer: Any = None) -> None:
        self._url = control_plane_url.rstrip("/") if control_plane_url else ""
        self._sidecar_id = sidecar_id
        self._quota_enforcer = quota_enforcer
        self._client: httpx.AsyncClient | None = None
        self._buffer: list[dict[str, Any]] = []
        self._buffer_max = 20

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
    ) -> None:
        """Report a single usage record. Buffers and flushes in batches."""
        if not self.enabled:
            return

        # Calculate cost locally using quota enforcer's pricing
        cost_usd = 0.0
        if self._quota_enforcer:
            cost_usd = self._quota_enforcer.calculate_cost(model, input_tokens, output_tokens)
            self._quota_enforcer.record_spend(cost_usd)

        self._buffer.append({
            "sidecar_id": self._sidecar_id,
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
        """Send buffered records to the control plane."""
        if not self._buffer or not self.enabled:
            return

        records = self._buffer[:]
        self._buffer.clear()

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=5.0)

        try:
            await self._client.post(
                f"{self._url}/api/costs/record/batch",
                json=records,
            )
        except Exception as e:
            log.debug("Failed to report cost to control plane: %s", e)

    async def close(self) -> None:
        await self.flush()
        if self._client:
            await self._client.aclose()
            self._client = None
