"""Process-host assembly seams shared by standalone and serverless adapters."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

AsyncCloseHook = Callable[[], Awaitable[None]]


class EventWorker(Protocol):
    """Minimal lifecycle used by the durable security-event dispatcher."""

    outbox_enabled: bool

    async def check_readiness(self) -> bool:
        """Return whether durable delivery can start."""

    async def start(self) -> None:
        """Start delivery."""

    async def stop(self) -> None:
        """Stop delivery."""


class ReconciliationMonitor(Protocol):
    """Minimal lifecycle used by topology and health reconciliation."""

    is_running: bool
    config: Any

    async def reconcile(self) -> None:
        """Reconcile desired workers with the current topology."""

    async def stop(self) -> None:
        """Stop all owned work."""


class PeriodicWorker(Protocol):
    """Minimal lifecycle used by bounded periodic workers."""

    async def start(self) -> None:
        """Start periodic work."""

    async def stop(self) -> None:
        """Stop periodic work."""


@dataclass
class WorkerAssembly:
    """Explicit owner for process background work and close hooks."""

    event_worker: EventWorker | None = None
    reconciliation_monitor: ReconciliationMonitor | None = None
    periodic_workers: tuple[PeriodicWorker, ...] = ()
    close_hooks: tuple[AsyncCloseHook, ...] = ()
    _started: bool = field(default=False, init=False)
    _closed: bool = field(default=False, init=False)

    @property
    def started(self) -> bool:
        return self._started

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        """Start configured workers; construction alone remains inert."""

        if self._closed:
            raise RuntimeError("worker assembly is closed")
        if self._started:
            return
        try:
            if self.event_worker is not None and self.event_worker.outbox_enabled:
                if not await self.event_worker.check_readiness():
                    raise RuntimeError("security event outbox is unavailable")
                await self.event_worker.start()
            if self.reconciliation_monitor is not None:
                await self.reconciliation_monitor.reconcile()
                if self.reconciliation_monitor.is_running:
                    spokes = getattr(
                        self.reconciliation_monitor.config,
                        "spokes",
                        (),
                    )
                    logger.info(
                        "Spoke health monitor started (%d spokes)",
                        len(spokes),
                    )
            for worker in self.periodic_workers:
                await worker.start()
        except BaseException:
            try:
                await self.close()
            except BaseException:
                logger.exception("worker cleanup failed during startup")
            raise
        self._started = True

    async def close(self) -> None:
        """Stop every owned component and report the first cleanup failure."""

        if self._closed:
            return
        self._closed = True
        failures: list[Exception] = []
        cancellation: asyncio.CancelledError | None = None

        for worker in reversed(self.periodic_workers):
            try:
                await worker.stop()
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
            except Exception as exc:
                failures.append(exc)
        if self.event_worker is not None:
            try:
                await self.event_worker.stop()
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
            except Exception as exc:
                failures.append(exc)
        if self.reconciliation_monitor is not None:
            try:
                await self.reconciliation_monitor.stop()
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
            except Exception as exc:
                failures.append(exc)
        for hook in reversed(self.close_hooks):
            try:
                await hook()
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
            except Exception as exc:
                failures.append(exc)

        if cancellation is not None:
            raise cancellation
        if failures:
            raise RuntimeError(
                f"{len(failures)} worker cleanup operation(s) failed"
            ) from failures[0]

    @contextlib.asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Start for one host lifespan and always close afterward."""

        await self.start()
        try:
            yield
        finally:
            await self.close()


def build_worker(
    *,
    event_worker: EventWorker | None = None,
    reconciliation_monitor: ReconciliationMonitor | None = None,
    periodic_workers: tuple[PeriodicWorker, ...] = (),
    close_hooks: tuple[AsyncCloseHook, ...] = (),
) -> WorkerAssembly:
    """Assemble background work without starting a task or external client."""

    return WorkerAssembly(
        event_worker=event_worker,
        reconciliation_monitor=reconciliation_monitor,
        periodic_workers=tuple(periodic_workers),
        close_hooks=tuple(close_hooks),
    )


__all__ = ["WorkerAssembly", "build_worker"]
