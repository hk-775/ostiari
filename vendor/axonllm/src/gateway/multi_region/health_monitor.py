"""Spoke health monitor — periodic checks with failover triggering.

Checks each spoke's health endpoint on an interval. After N consecutive
failures, marks the spoke as unhealthy and triggers failover if it's
the primary.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from src.gateway.multi_region.region_config import (
    HubConfig,
    SpokeConfig,
    SpokeStatus,
)

logger = logging.getLogger(__name__)


@dataclass
class HealthCheckResult:
    """Result of a single health check."""

    region: str
    healthy: bool
    latency_ms: float = 0.0
    status_code: int = 0
    error: str | None = None
    timestamp: float = field(default_factory=time.time)


class SpokeHealthMonitor:
    """Monitors spoke health and triggers status transitions.

    Runs as a background task checking each spoke periodically.
    """

    def __init__(self, hub_config: HubConfig) -> None:
        hub_config.validate()
        self._config = hub_config
        self._consecutive_failures: dict[str, int] = {}
        self._last_check: dict[str, HealthCheckResult] = {}
        self._failover_cooldown_until: dict[str, float] = {}
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def config(self) -> HubConfig:
        return self._config

    @property
    def last_checks(self) -> dict[str, HealthCheckResult]:
        return dict(self._last_check)

    @property
    def is_running(self) -> bool:
        return self._running

    async def check_spoke(self, spoke: SpokeConfig) -> HealthCheckResult:
        """Check a single spoke's health endpoint."""
        if not spoke.health_check_url:
            if spoke.region == self._config.hub_region:
                return HealthCheckResult(
                    region=spoke.region, healthy=True, latency_ms=0, status_code=200,
                )
            return HealthCheckResult(
                region=spoke.region, healthy=True, latency_ms=0,
                status_code=200, error="no_health_url_configured",
            )

        start = time.time()
        try:
            import httpx

            async with httpx.AsyncClient() as client:
                resp = await client.get(spoke.health_check_url, timeout=5.0)
                latency = (time.time() - start) * 1000
                healthy = resp.status_code == 200 and latency <= spoke.max_latency_ms
                return HealthCheckResult(
                    region=spoke.region,
                    healthy=healthy,
                    latency_ms=latency,
                    status_code=resp.status_code,
                )
        except ImportError:
            return HealthCheckResult(
                region=spoke.region, healthy=True, latency_ms=0,
                error="httpx_not_installed",
            )
        except Exception as e:
            latency = (time.time() - start) * 1000
            return HealthCheckResult(
                region=spoke.region,
                healthy=False,
                latency_ms=latency,
                error=str(e),
            )

    async def check_all(self) -> list[HealthCheckResult]:
        """Check all spokes and update their status."""
        results = []
        # A topology refresh can replace the spoke objects while a network check
        # is in flight. Always apply the result to the current object for that
        # region so a delayed check cannot update an orphaned pre-refresh copy.
        for spoke in list(self._config.spokes):
            result = await self.check_spoke(spoke)
            self._last_check[spoke.region] = result
            current = self._config.get_spoke(spoke.region)
            if (
                current is not None
                and current.health_check_url == spoke.health_check_url
                and current.max_latency_ms == spoke.max_latency_ms
            ):
                self._update_spoke_status(current, result)
            results.append(result)
        return results

    def _update_spoke_status(self, spoke: SpokeConfig, result: HealthCheckResult) -> None:
        """Update spoke status based on check result and consecutive failures."""
        region = spoke.region

        if result.healthy:
            self._consecutive_failures[region] = 0
            if spoke.status == SpokeStatus.UNHEALTHY:
                now = time.time()
                cooldown_until = self._failover_cooldown_until.get(region, 0)
                if now >= cooldown_until:
                    spoke.status = SpokeStatus.HEALTHY
                    logger.info("Spoke %s recovered", region)
            else:
                spoke.status = SpokeStatus.HEALTHY
        else:
            failures = self._consecutive_failures.get(region, 0) + 1
            self._consecutive_failures[region] = failures

            if failures >= self._config.failover_threshold_consecutive:
                if spoke.status != SpokeStatus.UNHEALTHY:
                    spoke.status = SpokeStatus.UNHEALTHY
                    self._failover_cooldown_until[region] = (
                        time.time() + self._config.failover_cooldown_seconds
                    )
                    logger.warning(
                        "Spoke %s marked UNHEALTHY after %d consecutive failures",
                        region, failures,
                    )

    def mark_unhealthy(self, region: str) -> None:
        """Manually mark a spoke as unhealthy (for admin override)."""
        spoke = self._config.get_spoke(region)
        if spoke:
            spoke.status = SpokeStatus.UNHEALTHY
            self._failover_cooldown_until[region] = (
                time.time() + self._config.failover_cooldown_seconds
            )

    def mark_healthy(self, region: str) -> None:
        """Manually mark a spoke as healthy (for admin override)."""
        spoke = self._config.get_spoke(region)
        if spoke:
            spoke.status = SpokeStatus.HEALTHY
            self._consecutive_failures[region] = 0

    def mark_draining(self, region: str) -> None:
        """Mark spoke as draining (no new traffic, finish existing)."""
        spoke = self._config.get_spoke(region)
        if spoke:
            spoke.status = SpokeStatus.DRAINING

    async def start(self) -> None:
        """Start the background health monitoring loop."""
        self._config.validate()
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        """Stop the background health monitoring loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def reconcile(self) -> None:
        """Run monitoring exactly while the live topology is multi-region."""
        self._config.validate()
        if self._config.is_single_region:
            await self.stop()
        else:
            await self.start()

    async def _monitor_loop(self) -> None:
        """Periodic health check loop."""
        while self._running:
            try:
                await self.check_all()
            except Exception:
                logger.error("Health check cycle failed", exc_info=True)
            await asyncio.sleep(self._config.health_check_interval_seconds)

    def get_status_summary(self) -> dict:
        """Return current status of all spokes."""
        return {
            "hub_region": self._config.hub_region,
            "spokes": [
                {
                    "region": s.region,
                    "role": s.role.value,
                    "status": s.status.value,
                    "weight": s.weight,
                    "consecutive_failures": self._consecutive_failures.get(s.region, 0),
                    "last_check": {
                        "healthy": self._last_check[s.region].healthy,
                        "latency_ms": self._last_check[s.region].latency_ms,
                        "error": self._last_check[s.region].error,
                    } if s.region in self._last_check else None,
                }
                for s in self._config.spokes
            ],
        }
