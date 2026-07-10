"""Test helpers for Ostiari extension authors."""

from __future__ import annotations

import time
from typing import Any

from ostiari.adapters.protocol import AdapterContext
from ostiari.models import (
    AnomalySignal,
    BreakerState,
    Checkpoint,
    CheckpointID,
    TraceEntry,
    TraceFilters,
)


class MockStorage:
    """In-memory storage backend implementing the StorageBackend protocol."""

    def __init__(self) -> None:
        self._traces: list[TraceEntry] = []
        self._checkpoints: dict[str, Checkpoint] = {}
        self._breakers: dict[str, BreakerState] = {}
        self._schema_ver = 1

    def save_trace(self, entry: TraceEntry) -> None:
        self._traces.append(entry)

    def save_traces_batch(self, entries: list[TraceEntry]) -> None:
        self._traces.extend(entries)

    def get_traces(self, filters: TraceFilters) -> list[TraceEntry]:
        results = self._traces[:]
        if filters.start_time:
            results = [t for t in results if t.timestamp >= filters.start_time]
        if filters.end_time:
            results = [t for t in results if t.timestamp <= filters.end_time]
        if filters.action:
            results = [t for t in results if filters.action in t.action]
        if filters.tier:
            results = [t for t in results if t.tier == filters.tier]
        if filters.limit:
            results = results[: filters.limit]
        return results

    def get_trace(self, trace_id: str) -> TraceEntry | None:
        for t in self._traces:
            if t.trace_id == trace_id:
                return t
        return None

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        self._checkpoints[checkpoint.checkpoint_id] = checkpoint

    def get_checkpoint(self, checkpoint_id: CheckpointID) -> Checkpoint:
        cp = self._checkpoints.get(str(checkpoint_id))
        if cp is None:
            from ostiari.exceptions import CheckpointNotFoundError

            raise CheckpointNotFoundError(str(checkpoint_id))
        return cp

    def delete_checkpoints(self, ids: list[CheckpointID]) -> None:
        for cid in ids:
            self._checkpoints.pop(str(cid), None)

    def save_breaker_state(self, state: BreakerState) -> None:
        self._breakers[state.breaker_id] = state

    def get_breaker_state(self, breaker_id: str) -> BreakerState | None:
        return self._breakers.get(breaker_id)

    def close(self) -> None:
        pass

    def migrate(self) -> None:
        pass

    def schema_version(self) -> int:
        return self._schema_ver


class MockDetector:
    """Configurable anomaly detector for testing."""

    def __init__(self, signals: list[AnomalySignal] | None = None) -> None:
        self._signals = list(signals) if signals else []
        self._call_count = 0

    @property
    def name(self) -> str:
        return "mock"

    def detect(
        self,
        action: str,
        params: dict[str, Any],
        history: list[TraceEntry],
    ) -> AnomalySignal | None:
        self._call_count += 1
        return self._signals.pop(0) if self._signals else None

    @property
    def call_count(self) -> int:
        return self._call_count


class MockAdapter:
    """Minimal adapter for testing adapter composition."""

    def __init__(self, name: str = "mock") -> None:
        self._name = name
        self.pre_hook_calls: list[tuple[str, dict[str, Any]]] = []
        self.post_hook_calls: list[tuple[AdapterContext, Any]] = []
        self.error_hook_calls: list[tuple[AdapterContext, Exception]] = []

    @property
    def name(self) -> str:
        return self._name

    def wrap_tool_call(self, tool: str, params: dict[str, Any]) -> AdapterContext:
        self.pre_hook_calls.append((tool, params))
        return AdapterContext(
            action=tool,
            params=params,
            framework_meta={"adapter": self._name},
            start_time=time.monotonic(),
        )

    def on_result(self, context: AdapterContext, result: Any) -> None:
        self.post_hook_calls.append((context, result))

    def on_error(self, context: AdapterContext, error: Exception) -> None:
        self.error_hook_calls.append((context, error))

    def get_framework_state(self) -> dict[str, Any]:
        return {"adapter": self._name, "mock": True}
