"""Scheduled budget-period resets for a gateway."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

Schedule = Literal["manual", "daily", "weekly", "monthly"]
ResetCallback = Callable[[datetime], Awaitable[Any] | Any]

log = logging.getLogger("ostiari.sidecar.budget_reset")


def _as_utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def next_reset_at(schedule: Schedule, now: datetime | None = None) -> datetime | None:
    """Return the next UTC boundary for a reset schedule."""
    current = _as_utc(now) or datetime.now(timezone.utc)
    if schedule == "manual":
        return None
    if schedule == "daily":
        return (current + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    if schedule == "weekly":
        days_until_monday = (7 - current.weekday()) % 7 or 7
        return (current + timedelta(days=days_until_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    if current.month == 12:
        return current.replace(
            year=current.year + 1,
            month=1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    return current.replace(
        month=current.month + 1,
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def latest_reset_boundary(
    schedule: Schedule, now: datetime | None = None
) -> datetime | None:
    """Return the most recent UTC boundary for a reset schedule."""
    current = _as_utc(now) or datetime.now(timezone.utc)
    if schedule == "manual":
        return None
    if schedule == "daily":
        return current.replace(hour=0, minute=0, second=0, microsecond=0)
    if schedule == "weekly":
        return (current - timedelta(days=current.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    return current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class BudgetResetScheduler:
    """Runs gateway and per-agent budget resets at UTC period boundaries."""

    def __init__(
        self,
        callback: ResetCallback,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.schedule: Schedule = "manual"
        self.last_reset_at: datetime | None = None
        self.configured_at: datetime | None = None
        self.next_reset: datetime | None = None
        self._callback = callback
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._task: asyncio.Task[None] | None = None

    def configure(self, config: dict[str, Any]) -> dict[str, Any]:
        """Apply a schedule and start (or stop) its background task."""
        schedule = str(config.get("schedule", "manual"))
        if schedule not in {"manual", "daily", "weekly", "monthly"}:
            raise ValueError("schedule must be manual, daily, weekly, or monthly")

        self.schedule = schedule  # type: ignore[assignment]
        self.last_reset_at = _as_utc(config.get("last_reset_at"))
        self.configured_at = _as_utc(config.get("configured_at"))
        self._cancel_task()

        if self.schedule == "manual":
            self.next_reset = None
            return self.status()

        now = _as_utc(self._now()) or datetime.now(timezone.utc)
        latest = latest_reset_boundary(self.schedule, now)
        # A persisted last-reset marker lets a gateway that was offline at a
        # boundary catch up immediately on startup. New schedules receive a
        # marker from the control plane, so enabling one does not erase spend.
        anchor = max(
            (
                value
                for value in (self.last_reset_at, self.configured_at)
                if value is not None
            ),
            default=None,
        )
        if latest is not None and anchor is not None and anchor < latest:
            self.next_reset = latest
        else:
            self.next_reset = next_reset_at(self.schedule, now)

        try:
            self._task = asyncio.create_task(self._run_loop())
        except RuntimeError:
            # Configuration can be constructed in a synchronous test or script.
            # The HTTP/lifespan paths always have a loop and start the task.
            self._task = None
        return self.status()

    async def trigger_now(self) -> dict[str, Any]:
        """Reset immediately and advance the schedule."""
        reset_at = _as_utc(self._now()) or datetime.now(timezone.utc)
        result = self._callback(reset_at)
        if inspect.isawaitable(result):
            await result
        self.last_reset_at = reset_at
        self.next_reset = next_reset_at(self.schedule, reset_at)
        return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "schedule": self.schedule,
            "last_reset_at": (
                self.last_reset_at.isoformat() if self.last_reset_at else None
            ),
            "configured_at": (
                self.configured_at.isoformat() if self.configured_at else None
            ),
            "next_reset": self.next_reset.isoformat() if self.next_reset else None,
        }

    async def _run_loop(self) -> None:
        while self.next_reset is not None:
            try:
                now = _as_utc(self._now()) or datetime.now(timezone.utc)
                delay = max(0.0, (self.next_reset - now).total_seconds())
                if delay:
                    await asyncio.sleep(delay)
                await self.trigger_now()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                log.error("Budget reset failed: %s", exc)
                await asyncio.sleep(60)

    def _cancel_task(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def close(self) -> None:
        task = self._task
        self._cancel_task()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
