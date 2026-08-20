"""Routing strategies for distributing requests across healthy providers."""

from __future__ import annotations

import random
from abc import ABC, abstractmethod

from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.models import ProviderModelMapping, TokenPricing


class NoHealthyProviderError(Exception):
    """Raised when no healthy providers are available for routing."""


class RoutingStrategyBase(ABC):
    """Base class for all routing strategies."""

    @abstractmethod
    def select(
        self,
        providers: list[ProviderModelMapping],
        health_tracker: ProviderHealthTracker,
    ) -> ProviderModelMapping:
        """Select a provider from the list of healthy providers.

        Raises ``NoHealthyProviderError`` if no healthy providers exist.
        """
        ...

    def _healthy_providers(
        self,
        providers: list[ProviderModelMapping],
        health_tracker: ProviderHealthTracker,
    ) -> list[ProviderModelMapping]:
        return [p for p in providers if health_tracker.is_healthy(p.provider)]


class RoundRobinStrategy(RoutingStrategyBase):
    """Cycles through healthy providers sequentially.

    A single instance is shared across every model (see ``Router._strategies``),
    so the cursor is tracked per provider set rather than globally. Otherwise
    two models routing round-robin would advance the same counter and each would
    skip providers instead of cycling through its own mappings in order.
    """

    def __init__(self) -> None:
        self._indices: dict[str, int] = {}

    def _provider_key(self, providers: list[ProviderModelMapping]) -> str:
        """Stable key for a provider set, independent of mapping order.

        Includes ``model_id`` so two models fronted by the same providers still
        get independent cursors.
        """
        return ",".join(sorted(f"{p.provider}:{p.model_id}" for p in providers))

    def select(
        self,
        providers: list[ProviderModelMapping],
        health_tracker: ProviderHealthTracker,
    ) -> ProviderModelMapping:
        healthy = self._healthy_providers(providers, health_tracker)
        if not healthy:
            raise NoHealthyProviderError("No healthy providers available")
        key = self._provider_key(providers)
        index = self._indices.get(key, 0)
        selected = healthy[index % len(healthy)]
        self._indices[key] = index + 1
        return selected


class WeightedStrategy(RoutingStrategyBase):
    """Distributes requests proportionally to configured weights among healthy providers."""

    def select(
        self,
        providers: list[ProviderModelMapping],
        health_tracker: ProviderHealthTracker,
    ) -> ProviderModelMapping:
        healthy = self._healthy_providers(providers, health_tracker)
        if not healthy:
            raise NoHealthyProviderError("No healthy providers available")
        weights = [p.weight for p in healthy]
        return random.choices(healthy, weights=weights, k=1)[0]


class LeastLatencyStrategy(RoutingStrategyBase):
    """Routes to the provider with the lowest average latency in a sliding window."""

    def __init__(self, window_seconds: int = 60) -> None:
        self.window_seconds = window_seconds

    def select(
        self,
        providers: list[ProviderModelMapping],
        health_tracker: ProviderHealthTracker,
    ) -> ProviderModelMapping:
        healthy = self._healthy_providers(providers, health_tracker)
        if not healthy:
            raise NoHealthyProviderError("No healthy providers available")
        return min(
            healthy,
            key=lambda p: health_tracker.get_average_latency(
                p.provider, self.window_seconds
            ),
        )


class CostOptimizedStrategy(RoutingStrategyBase):
    """Routes to the cheapest healthy provider based on per-token pricing."""

    def __init__(
        self,
        pricing_config: dict[str, dict[str, TokenPricing]] | None = None,
    ) -> None:
        self.pricing_config = (
            pricing_config if pricing_config is not None else {}
        )

    def select(
        self,
        providers: list[ProviderModelMapping],
        health_tracker: ProviderHealthTracker,
    ) -> ProviderModelMapping:
        healthy = self._healthy_providers(providers, health_tracker)
        if not healthy:
            raise NoHealthyProviderError("No healthy providers available")
        return min(healthy, key=self._cost_key)

    def _cost_key(self, p: ProviderModelMapping) -> float:
        pricing = p.pricing
        if pricing is None or not pricing.is_billable:
            pricing = self.pricing_config.get(p.provider, {}).get(p.model_id)
        if pricing is None or not pricing.is_billable:
            return float("inf")
        return pricing.prompt_token_cost + pricing.completion_token_cost
