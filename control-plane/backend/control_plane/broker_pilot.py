"""Token broker pilot layer — pool draw-down, depletion, billing seam.

This is the production-path logic beyond the reporting in token_broker.py:
  - map a model to its provider (whose pool it draws from),
  - draw consumed tokens down against that provider's pool,
  - a swappable billing Collector (simulated now, real rail later) — same
    pattern as the x402 Settler, so going live is dropping in a real collector
    without touching the pool/consumption code.

Reconciliation (computed-vs-invoiced) lives in the router, since it's a
DB-aggregation over the period.
"""

from __future__ import annotations

from typing import Protocol

# Which provider's pool a model draws from. Prefix match, first hit wins.
_MODEL_PROVIDER = [
    ("claude", "anthropic"),
    ("gpt", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("gemini", "google"),
    ("nova", "bedrock"),
    ("command", "cohere"),
]
DEFAULT_PROVIDER = "other"


def provider_for(model: str) -> str:
    """Map a model name to the provider pool it draws from."""
    m = (model or "").lower()
    for prefix, provider in _MODEL_PROVIDER:
        if prefix in m:
            return provider
    return DEFAULT_PROVIDER


# ─── Billing collector seam (simulated now, real rail later) ─────────────────

class Collector(Protocol):
    """Collects the customer charge for brokered usage.

    Simulated collector records intent only; a live Stripe/marketplace collector
    would call the metering API. The pool/consumption path is identical either
    way — this is the single swap point, like the x402 Settler.
    """

    @property
    def mode(self) -> str: ...

    async def collect(self, *, customer: str, amount_usd: float, model: str) -> dict:
        """Record/collect a charge. Returns {collected, ref, mode}."""
        ...


class SimulatedCollector:
    """Records billing intent without moving money. Pilot/demo default."""

    mode = "simulated"

    def __init__(self) -> None:
        self._n = 0

    async def collect(self, *, customer: str, amount_usd: float, model: str) -> dict:
        self._n += 1
        return {"collected": True, "ref": f"sim-bill-{customer}-{self._n}", "mode": self.mode}


class StripeCollector:
    """Live metered billing (stub) — wire a Stripe/AWS-Marketplace client here.

    Not implemented: this is the seam that turns the pilot into real collection.
    A live build would push a metered-usage event to Stripe (or the marketplace
    metering API) for `amount_usd` against the customer's subscription item.
    Requires a real account + API key + money-movement review — deliberately not
    wired so no live billing happens by accident.
    """

    mode = "live"

    def __init__(self, api_key: str = "", price_id: str = "") -> None:
        self._api_key = api_key
        self._price_id = price_id

    async def collect(self, *, customer: str, amount_usd: float, model: str) -> dict:
        raise NotImplementedError(
            "StripeCollector is a stub. Provide a Stripe client + price id to collect for real."
        )
