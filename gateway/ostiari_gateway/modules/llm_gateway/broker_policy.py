"""Gateway-side token-pool availability policy."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("ostiari.sidecar.llm.broker")


_PROVIDER_ALIASES = {
    "aws-bedrock": "bedrock",
    "bedrock-mantle": "bedrock",
    "google_ai": "google",
    "vertex": "google",
    "vertex_ai": "google",
}


def canonical_provider(provider: str) -> str:
    value = (provider or "").strip().lower()
    return _PROVIDER_ALIASES.get(value, value or "other")


def provider_for_model(model: str) -> str:
    """Best-effort provider for direct (non-Axon) model calls."""
    value = (model or "").lower()
    if value.startswith("bedrock/") or "bedrock-mantle" in value or "nova" in value:
        return "bedrock"
    if value.startswith("azure/"):
        return "azure"
    if value.startswith("vertex/") or "gemini" in value or "google_ai" in value:
        return "google"
    if "gpt" in value or value.startswith("openai/") or any(
        marker in value for marker in ("o1", "o3", "o4")
    ):
        return "openai"
    if "command" in value or "cohere" in value:
        return "cohere"
    if "claude" in value or value.startswith("anthropic"):
        return "anthropic"
    return "other"


class BrokerPoolDepletedError(RuntimeError):
    """Raised before a provider call when no funded route remains."""


class BrokerPoolPolicy:
    """Tracks control-plane pool state and filters depleted providers.

    A provider with no provisioned pool is intentionally allowed: broker mode is
    incremental, and the control plane's drawdown path is likewise a no-op for an
    unprovisioned provider. Only an explicit ``depleted`` state blocks routing.
    """

    def __init__(self) -> None:
        self._pools: dict[str, dict[str, Any]] = {}

    def configure(self, pools: list[dict[str, Any]] | None) -> None:
        previous = self.blocked_providers
        configured: dict[str, dict[str, Any]] = {}
        for raw in pools or []:
            if not isinstance(raw, dict):
                continue
            provider = canonical_provider(str(raw.get("provider", "")))
            if provider == "other" and not raw.get("provider"):
                continue
            configured[provider] = {**raw, "provider": provider}
        self._pools = configured

        if previous != self.blocked_providers:
            log.warning(
                "Broker pool routing updated: depleted=%s",
                sorted(self.blocked_providers),
            )

    @property
    def blocked_providers(self) -> frozenset[str]:
        return frozenset(
            provider
            for provider, pool in self._pools.items()
            if str(pool.get("status", "")).lower() == "depleted"
        )

    def is_provider_available(self, provider: str) -> bool:
        return canonical_provider(provider) not in self.blocked_providers

    def is_model_available(self, model: str) -> bool:
        return self.is_provider_available(provider_for_model(model))

    def available_models(self, models: list[str]) -> list[str]:
        """Filter a direct-call model/fallback list, preserving order."""
        available: list[str] = []
        seen: set[str] = set()
        for model in models:
            if not model or model in seen:
                continue
            seen.add(model)
            if self.is_model_available(model):
                available.append(model)
        return available

    def require_direct_route(self, models: list[str]) -> list[str]:
        available = self.available_models(models)
        if available:
            return available
        providers = sorted({provider_for_model(model) for model in models if model})
        raise BrokerPoolDepletedError(
            "No funded provider route is available"
            + (f" ({', '.join(providers)} depleted)" if providers else "")
        )

    def get_status(self) -> dict[str, Any]:
        return {
            "pools": list(self._pools.values()),
            "depleted_providers": sorted(self.blocked_providers),
        }
