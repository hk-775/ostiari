"""Budget auto-reset scheduler."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx

from control_plane.services.push_service import gateway_config_headers

logger = logging.getLogger(__name__)

Schedule = Literal["manual", "daily", "weekly", "monthly"]


class BudgetResetScheduler:
    """Schedules automatic budget resets for gateways."""

    def __init__(self) -> None:
        self.schedule: Schedule = "manual"
        self.next_reset: datetime | None = None
        self._task: asyncio.Task | None = None
        self._gateway_endpoints: list[str] = []

    def configure(self, schedule: Schedule, gateway_endpoints: list[str] | None = None) -> None:
        """Configure the reset schedule and restart the background task."""
        self.schedule = schedule
        if gateway_endpoints is not None:
            self._gateway_endpoints = gateway_endpoints

        # Cancel existing task
        if self._task and not self._task.done():
            self._task.cancel()

        if schedule == "manual":
            self.next_reset = None
            return

        self.next_reset = self._compute_next_reset()
        self._task = asyncio.create_task(self._run_loop())

    def _compute_next_reset(self) -> datetime:
        """Compute the next reset time based on schedule."""
        now = datetime.now(timezone.utc)
        if self.schedule == "daily":
            return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif self.schedule == "weekly":
            days_until_monday = (7 - now.weekday()) % 7 or 7
            return (now + timedelta(days=days_until_monday)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        elif self.schedule == "monthly":
            if now.month == 12:
                return now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            return now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return now + timedelta(days=1)

    async def _run_loop(self) -> None:
        """Background loop that triggers resets on schedule."""
        while True:
            try:
                if not self.next_reset:
                    return
                now = datetime.now(timezone.utc)
                delay = (self.next_reset - now).total_seconds()
                if delay > 0:
                    await asyncio.sleep(delay)

                await self._trigger_reset()
                self.next_reset = self._compute_next_reset()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"Budget reset error: {e}")
                await asyncio.sleep(60)

    async def _trigger_reset(self) -> None:
        """Call each gateway's quota reset endpoint."""
        async with httpx.AsyncClient(
            timeout=10.0, headers=gateway_config_headers()
        ) as client:
            for endpoint in self._gateway_endpoints:
                try:
                    url = f"{endpoint.rstrip('/')}/config/quota/reset-spend"
                    resp = await client.post(url)
                    logger.info(f"Budget reset for {endpoint}: {resp.status_code}")
                except Exception as e:
                    logger.warning(f"Failed to reset budget for {endpoint}: {e}")

    def stop(self) -> None:
        """Stop the background scheduler."""
        if self._task and not self._task.done():
            self._task.cancel()
