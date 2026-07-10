"""Trace poller — background task for discovering new traces."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone

from ostiari.dashboard.storage_async import AsyncStorageWrapper
from ostiari.dashboard.websocket import WebSocketManager
from ostiari.models import TraceFilters

logger = logging.getLogger("ostiari.dashboard")


class TracePoller:
    """Background asyncio task polling storage for new traces."""

    def __init__(
        self,
        storage: AsyncStorageWrapper,
        ws_manager: WebSocketManager,
        interval: float = 0.5,
    ) -> None:
        self._storage = storage
        self._ws_manager = ws_manager
        self._interval = interval
        self._last_seen: datetime | None = None
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._last_seen = datetime.now(timezone.utc)
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                filters = TraceFilters(start_time=self._last_seen, limit=50)
                new_traces = await self._storage.get_traces(filters)
                if new_traces:
                    self._last_seen = new_traces[-1].timestamp
                    serialized = [t.model_dump(mode="json") for t in new_traces]
                    await self._ws_manager.publish_traces(serialized)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Trace poll failed: %s", e)
            await asyncio.sleep(self._interval)
