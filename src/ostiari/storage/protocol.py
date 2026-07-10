"""StorageBackend protocol definition."""

from __future__ import annotations

from typing import Protocol

from ostiari.models import (
    BreakerState,
    Checkpoint,
    CheckpointID,
    TraceEntry,
    TraceFilters,
)


class StorageBackend(Protocol):
    """Protocol for Ostiari storage implementations."""

    def save_trace(self, entry: TraceEntry) -> None: ...

    def save_traces_batch(self, entries: list[TraceEntry]) -> None: ...

    def get_traces(self, filters: TraceFilters) -> list[TraceEntry]: ...

    def get_trace(self, trace_id: str) -> TraceEntry | None: ...

    def save_checkpoint(self, checkpoint: Checkpoint) -> None: ...

    def get_checkpoint(self, checkpoint_id: CheckpointID) -> Checkpoint: ...

    def delete_checkpoints(self, ids: list[CheckpointID]) -> None: ...

    def save_breaker_state(self, state: BreakerState) -> None: ...

    def get_breaker_state(self, breaker_id: str) -> BreakerState | None: ...

    def close(self) -> None: ...

    def migrate(self) -> None: ...

    def schema_version(self) -> int: ...
