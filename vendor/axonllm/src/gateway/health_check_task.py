"""Background health check task for proactive provider monitoring."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from src.gateway.health_tracker import ProviderHealthTracker

logger = logging.getLogger(__name__)


class HealthCheckTask:
    """Periodically checks provider health and updates the HealthTracker.

    Accepts an injectable ``check_fn`` so tests can supply a mock without
    making real provider calls.
    """

    def __init__(
        self,
        health_tracker: ProviderHealthTracker,
        providers: list[str],
        check_fn: Callable[[str], Awaitable[bool]],
        interval_seconds: float = 30.0,
        cooldown_seconds: int = 60,
    ) -> None:
        self.health_tracker = health_tracker
        self.providers = list(providers)
        self.check_fn = check_fn
        self.interval_seconds = interval_seconds
        self.cooldown_seconds = cooldown_seconds
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the background check loop as an asyncio.Task."""
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Cancel the background task and wait for cleanup."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        """Periodically call check_fn for each provider."""
        while True:
            for provider in self.providers:
                try:
                    healthy = await self.check_fn(provider)
                    if healthy:
                        # Ensure provider is marked healthy (clear any cooldown)
                        self.health_tracker._unhealthy.pop(provider, None)
                    else:
                        self.health_tracker.mark_unhealthy(
                            provider, self.cooldown_seconds
                        )
                except Exception:
                    logger.warning(
                        "Health check failed for provider '%s'",
                        provider,
                        exc_info=True,
                    )
            await asyncio.sleep(self.interval_seconds)
