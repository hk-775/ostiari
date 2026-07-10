"""Async wrapper for synchronous StorageBackend."""

from __future__ import annotations

import asyncio

from ostiari.models import BreakerState, TraceEntry, TraceFilters
from ostiari.storage.protocol import StorageBackend


class AsyncStorageWrapper:
    """Bridges synchronous StorageBackend for use in async dashboard context."""

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage

    async def get_traces(self, filters: TraceFilters) -> list[TraceEntry]:
        return await asyncio.to_thread(self._storage.get_traces, filters)

    async def get_trace(self, trace_id: str) -> TraceEntry | None:
        return await asyncio.to_thread(self._storage.get_trace, trace_id)

    async def get_breaker_state(self, breaker_id: str) -> BreakerState | None:
        return await asyncio.to_thread(self._storage.get_breaker_state, breaker_id)

    async def schema_version(self) -> int:
        return await asyncio.to_thread(self._storage.schema_version)

    def close(self) -> None:
        self._storage.close()
