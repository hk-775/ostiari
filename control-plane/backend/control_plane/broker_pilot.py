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

from decimal import ROUND_HALF_UP, Decimal
from hashlib import sha256
from typing import Any, Protocol

import httpx

# Which provider's pool a model draws from. Prefix match, first hit wins.
_MODEL_PROVIDER = [
    ("bedrock/", "bedrock"),
    ("bedrock-mantle", "bedrock"),
    ("azure/", "azure"),
    ("vertex/", "google"),
    ("google_ai", "google"),
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


_PROVIDER_ALIASES = {
    "aws-bedrock": "bedrock",
    "bedrock-mantle": "bedrock",
    "google_ai": "google",
    "vertex": "google",
    "vertex_ai": "google",
}


def canonical_provider(provider: str) -> str:
    """Normalize runtime provider names to broker pool identities."""
    value = (provider or "").strip().lower()
    return _PROVIDER_ALIASES.get(value, value or DEFAULT_PROVIDER)


def provider_for(model: str) -> str:
    """Map a model name to the provider pool it draws from."""
    m = (model or "").lower()
    for prefix, provider in _MODEL_PROVIDER:
        if prefix in m:
            return canonical_provider(provider)
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

    async def collect(
        self,
        *,
        customer: str,
        amount_usd: float,
        model: str,
        idempotency_key: str = "",
    ) -> dict:
        """Record/collect a charge. Returns {collected, ref, mode}."""
        ...


class SimulatedCollector:
    """Records billing intent without moving money. Pilot/demo default."""

    mode = "simulated"

    def __init__(self) -> None:
        self._n = 0
        self._refs: dict[str, str] = {}

    async def collect(
        self,
        *,
        customer: str,
        amount_usd: float,
        model: str,
        idempotency_key: str = "",
    ) -> dict:
        if idempotency_key and idempotency_key in self._refs:
            ref = self._refs[idempotency_key]
        else:
            self._n += 1
            ref = f"sim-bill-{customer}-{self._n}"
            if idempotency_key:
                self._refs[idempotency_key] = ref
        return {"collected": True, "ref": ref, "mode": self.mode}

    def status(self) -> dict[str, Any]:
        return {"mode": self.mode}


class StripeCollector:
    """Stripe Billing Meter Events collector.

    Each Ostiari usage event becomes one Stripe meter event. Values are integer
    micro-USD, so the corresponding Stripe meter price must bill one dollar per
    1,000,000 units. Both Stripe's ``identifier`` and HTTP idempotency key use
    the gateway event id, making retries safe across process restarts.
    """

    mode = "live"

    def __init__(
        self,
        api_key: str = "",
        meter_event_name: str = "ostiari_broker_usage",
        *,
        customer_map: dict[str, str] | None = None,
        default_customer: str = "",
        api_base: str = "https://api.stripe.com",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._meter_event_name = meter_event_name.strip()
        self._customer_map = {
            str(org): str(customer_id)
            for org, customer_id in (customer_map or {}).items()
            if str(org) and str(customer_id)
        }
        self._default_customer = default_customer.strip()
        self._api_base = api_base.rstrip("/")
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(
            self._api_key
            and self._meter_event_name
            and (self._default_customer or self._customer_map)
        )

    async def collect(
        self,
        *,
        customer: str,
        amount_usd: float,
        model: str,
        idempotency_key: str = "",
    ) -> dict:
        if not self._api_key:
            raise RuntimeError("STRIPE_API_KEY is required for live broker billing")
        if not self._meter_event_name:
            raise RuntimeError(
                "STRIPE_METER_EVENT_NAME is required for live broker billing"
            )
        stripe_customer = self._customer_map.get(customer) or self._default_customer
        if not stripe_customer and customer.startswith("cus_"):
            stripe_customer = customer
        if not stripe_customer:
            raise RuntimeError(
                f"no Stripe customer is configured for Ostiari organization '{customer}'"
            )

        amount = Decimal(str(amount_usd))
        if amount < 0:
            raise ValueError("broker charge cannot be negative")
        micro_usd = int(
            (amount * Decimal("1000000")).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )
        identifier = self._identifier(idempotency_key or f"{customer}:{model}:{micro_usd}")
        data = {
            "event_name": self._meter_event_name,
            "identifier": identifier,
            "payload[stripe_customer_id]": stripe_customer,
            "payload[value]": str(micro_usd),
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Idempotency-Key": identifier,
        }

        if self._client is not None:
            response = await self._client.post(
                f"{self._api_base}/v1/billing/meter_events",
                data=data,
                headers=headers,
            )
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{self._api_base}/v1/billing/meter_events",
                    data=data,
                    headers=headers,
                )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            message = self._stripe_error(response)
            raise RuntimeError(
                f"Stripe meter event failed with HTTP {response.status_code}: {message}"
            ) from exc

        payload = response.json()
        ref = str(payload.get("identifier") or payload.get("id") or identifier)
        return {
            "collected": True,
            "ref": ref,
            "mode": self.mode,
            "quantity_microusd": micro_usd,
        }

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "configured": self.configured,
            "meter_event_name": self._meter_event_name,
            "customer_mappings": len(self._customer_map),
            "default_customer": bool(self._default_customer),
        }

    @staticmethod
    def _identifier(value: str) -> str:
        # Stripe accepts caller-provided meter event identifiers. Hash the
        # tenant/gateway-scoped identity so it is bounded, safe, and does not
        # expose internal organization names in Stripe.
        return f"ostiari-{sha256(value.encode()).hexdigest()}"

    @staticmethod
    def _stripe_error(response: httpx.Response) -> str:
        try:
            payload = response.json()
            error = payload.get("error", {})
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])
        except Exception:  # noqa: BLE001 - fallback below is intentionally safe
            pass
        return "request rejected"
