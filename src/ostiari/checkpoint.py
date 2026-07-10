"""CheckpointEngine — checkpoint creation, rollback, and retention."""

from __future__ import annotations

import logging
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from ostiari.exceptions import CheckpointNotFoundError
from ostiari.models import (
    Checkpoint,
    CheckpointID,
    CheckpointState,
    RetentionPolicy,
)

log = logging.getLogger("ostiari")


class CheckpointEngine:
    def __init__(
        self,
        storage: Any = None,
        retention: RetentionPolicy | None = None,
        persist_queue: deque[tuple[str, object]] | None = None,
    ) -> None:
        self._storage = storage
        self._retention = retention or RetentionPolicy()
        self._persist_queue = persist_queue
        self._recent: deque[Checkpoint] = deque(maxlen=(self._retention.keep_last or 100) * 2)
        self._sequence: int = 0
        self._seq_lock = threading.Lock()
        self._auto_enabled: bool = True

    def create(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        result: Any = None,
        name: str | None = None,
        state: dict[str, Any] | None = None,
    ) -> CheckpointID:
        with self._seq_lock:
            self._sequence += 1
            seq = self._sequence

        cp = Checkpoint(
            checkpoint_id=str(uuid.uuid4()),
            name=name,
            sequence_number=seq,
            timestamp=datetime.now(timezone.utc),
            state=state or {},
            action=action,
            params=params or {},
            result=result,
        )
        self._recent.append(cp)

        if self._persist_queue is not None:
            self._persist_queue.append(("checkpoint", cp))

        self._enforce_retention()
        return cp.checkpoint_id

    def rollback(self, to: str) -> CheckpointState:
        for cp in reversed(self._recent):
            if cp.checkpoint_id == to:
                return CheckpointState(checkpoint=cp, restored_at=datetime.now(timezone.utc))

        for cp in reversed(self._recent):
            if cp.name == to:
                return CheckpointState(checkpoint=cp, restored_at=datetime.now(timezone.utc))

        if self._storage is not None:
            try:
                cp = self._storage.get_checkpoint(to)
                return CheckpointState(checkpoint=cp, restored_at=datetime.now(timezone.utc))
            except Exception:
                pass

        raise CheckpointNotFoundError(to)

    def list(self, limit: int = 20) -> list[Checkpoint]:
        return list(self._recent)[-limit:]

    def get(self, checkpoint_id: CheckpointID) -> Checkpoint:
        for cp in reversed(self._recent):
            if cp.checkpoint_id == checkpoint_id:
                return cp

        if self._storage is not None:
            try:
                cp = self._storage.get_checkpoint(checkpoint_id)
                result: Checkpoint = cp
                return result
            except Exception:
                pass

        raise CheckpointNotFoundError(checkpoint_id)

    def configure(self, retention: RetentionPolicy) -> None:
        self._retention = retention

    def cleanup(self) -> None:
        self._enforce_retention()

    @property
    def auto_enabled(self) -> bool:
        return self._auto_enabled

    @auto_enabled.setter
    def auto_enabled(self, value: bool) -> None:
        self._auto_enabled = value

    @property
    def sequence(self) -> int:
        return self._sequence

    def _enforce_retention(self) -> None:
        if not self._recent:
            return

        to_delete_ids: set[str] = set()
        all_checkpoints = list(self._recent)

        unnamed = [cp for cp in all_checkpoints if cp.name is None]

        if self._retention.keep_last and len(unnamed) > self._retention.keep_last:
            excess = unnamed[: len(unnamed) - self._retention.keep_last]
            to_delete_ids.update(cp.checkpoint_id for cp in excess)

        if self._retention.max_age_hours:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=self._retention.max_age_hours)
            for cp in unnamed:
                if cp.timestamp < cutoff:
                    to_delete_ids.add(cp.checkpoint_id)

        if self._retention.keep_named:
            to_delete_ids -= {cp.checkpoint_id for cp in all_checkpoints if cp.name is not None}

        if to_delete_ids:
            self._recent = deque(
                (cp for cp in self._recent if cp.checkpoint_id not in to_delete_ids),
                maxlen=self._recent.maxlen,
            )

            if self._storage is not None:
                try:
                    self._storage.delete_checkpoints(list(to_delete_ids))
                except Exception as e:
                    log.warning("[Ostiari] Checkpoint cleanup failed: %s", e)
