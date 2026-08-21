"""Region router — selects the target spoke for each request.

Decision logic:
1. Filter by data residency (if strict mode and user has residency constraint)
2. Filter by health (remove unhealthy/draining spokes)
3. Filter by model availability (spoke must support the requested model)
4. Select from survivors by weighted random or priority
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from src.gateway.multi_region.region_config import (
    HubConfig,
    SpokeConfig,
    SpokeRole,
    SpokeStatus,
)


@dataclass
class RoutingDecision:
    """Result of the region routing decision."""

    target_spoke: SpokeConfig
    reason: str
    candidates_considered: int
    fallback_used: bool = False


class RegionRouter:
    """Routes requests to the appropriate spoke based on policy and health."""

    def __init__(self, hub_config: HubConfig) -> None:
        self._config = hub_config

    @property
    def config(self) -> HubConfig:
        return self._config

    def route(
        self,
        model: str | None = None,
        data_residency_zone: str | None = None,
        preferred_region: str | None = None,
    ) -> RoutingDecision | None:
        """Select the best spoke for this request. Returns None if no spoke available."""
        candidates = list(self._config.spokes)

        # 1. Filter by health
        healthy = [s for s in candidates if s.status in (SpokeStatus.HEALTHY, SpokeStatus.DEGRADED)]
        if not healthy:
            return None

        # 2. Filter by data residency
        if data_residency_zone and self._config.data_residency_strict:
            residency_filtered = [
                s for s in healthy
                if not s.data_residency_zones or data_residency_zone in s.data_residency_zones
            ]
            if residency_filtered:
                healthy = residency_filtered
            else:
                return None

        # 3. Filter by model availability
        if model:
            model_filtered = [s for s in healthy if not s.models or model in s.models]
            if model_filtered:
                healthy = model_filtered

        # 4. Preferred region (hint, not hard requirement)
        if preferred_region:
            preferred = [s for s in healthy if s.region == preferred_region]
            if preferred:
                return RoutingDecision(
                    target_spoke=preferred[0],
                    reason="preferred_region",
                    candidates_considered=len(healthy),
                )

        # 5. Single region — always route there
        if len(healthy) == 1:
            return RoutingDecision(
                target_spoke=healthy[0],
                reason="single_available",
                candidates_considered=1,
            )

        # 6. Primary available — use it (active-passive mode)
        primary = next((s for s in healthy if s.role == SpokeRole.PRIMARY), None)
        if primary and self._is_failover_mode():
            return RoutingDecision(
                target_spoke=primary,
                reason="primary_healthy",
                candidates_considered=len(healthy),
            )

        # 7. Weighted selection (active-active mode)
        return self._weighted_select(healthy)

    def failover(self) -> RoutingDecision | None:
        """Force failover: select the best non-primary healthy spoke."""
        candidates = self._config.get_failover_candidates()
        if not candidates:
            return None

        return RoutingDecision(
            target_spoke=candidates[0],
            reason="failover",
            candidates_considered=len(candidates),
            fallback_used=True,
        )

    def _is_failover_mode(self) -> bool:
        """Check if config is active-passive (failover spokes have weight=0)."""
        non_primary = [s for s in self._config.spokes if s.role != SpokeRole.PRIMARY]
        return all(s.weight == 0 or s.role == SpokeRole.FAILOVER for s in non_primary)

    def _weighted_select(self, spokes: list[SpokeConfig]) -> RoutingDecision:
        """Select spoke using weighted random distribution."""
        total_weight = sum(s.weight for s in spokes)
        if total_weight == 0:
            selected = secrets.choice(spokes)
            return RoutingDecision(
                target_spoke=selected,
                reason="random_equal_weight",
                candidates_considered=len(spokes),
            )

        r = secrets.randbelow(total_weight) + 1
        cumulative = 0
        for spoke in spokes:
            cumulative += spoke.weight
            if r <= cumulative:
                return RoutingDecision(
                    target_spoke=spoke,
                    reason="weighted_selection",
                    candidates_considered=len(spokes),
                )

        return RoutingDecision(
            target_spoke=spokes[-1],
            reason="weighted_fallback",
            candidates_considered=len(spokes),
        )
