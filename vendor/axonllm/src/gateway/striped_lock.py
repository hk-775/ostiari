"""Striped async locks — per-key locking to avoid one global lock serializing
the whole hot path.

A single ``asyncio.Lock`` guarding rate-limit / quota state forces every request
(across all projects and users) through one critical section. Sharding the lock
by key lets requests for different keys proceed concurrently while still making
each key's read-modify-write atomic.

``multi()`` acquires several keys' locks in a canonical (sorted) order so callers
that need two keys at once (e.g. the rate limiter touches both a user bucket and
a project bucket) can never deadlock against each other.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


class StripedLock:
    """A registry of per-key ``asyncio.Lock`` objects, created on demand."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        # Guards creation of per-key locks so two coroutines racing on a new key
        # get the SAME Lock instance (dict.setdefault isn't enough because Lock()
        # construction + insert must be seen atomically by both).
        self._registry_lock = asyncio.Lock()

    async def _get(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            async with self._registry_lock:
                lock = self._locks.get(key)
                if lock is None:
                    lock = asyncio.Lock()
                    self._locks[key] = lock
        return lock

    @asynccontextmanager
    async def acquire(self, key: str):
        """Hold the lock for a single key."""
        lock = await self._get(key)
        async with lock:
            yield

    @asynccontextmanager
    async def multi(self, *keys: str):
        """Hold locks for several keys, acquired in sorted order (deadlock-free).

        Duplicate keys are collapsed to a single acquisition.
        """
        ordered = sorted(set(k for k in keys if k))
        acquired: list[asyncio.Lock] = []
        try:
            for key in ordered:
                lock = await self._get(key)
                await lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()
