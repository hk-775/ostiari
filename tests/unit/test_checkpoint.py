"""Unit tests for ostiari.checkpoint."""

from __future__ import annotations

from collections import deque
from unittest.mock import MagicMock

import pytest

from ostiari.checkpoint import CheckpointEngine
from ostiari.exceptions import CheckpointNotFoundError
from ostiari.models import RetentionPolicy


class TestCreate:
    def test_returns_checkpoint_id(self):
        engine = CheckpointEngine()
        cp_id = engine.create(action="test_tool", params={"x": 1})
        assert isinstance(cp_id, str)
        assert len(cp_id) > 0

    def test_sequence_increments(self):
        engine = CheckpointEngine()
        engine.create(action="a", params={})
        engine.create(action="b", params={})
        checkpoints = engine.list()
        assert checkpoints[0].sequence_number == 1
        assert checkpoints[1].sequence_number == 2

    def test_named_checkpoint(self):
        engine = CheckpointEngine()
        engine.create(action="tool", params={}, name="before_danger")
        checkpoints = engine.list()
        assert checkpoints[0].name == "before_danger"

    def test_state_stored(self):
        engine = CheckpointEngine()
        engine.create(action="tool", params={}, state={"key": "value"})
        checkpoints = engine.list()
        assert checkpoints[0].state == {"key": "value"}

    def test_persist_enqueued(self):
        pq = deque()
        engine = CheckpointEngine(persist_queue=pq)
        engine.create(action="tool", params={})
        assert len(pq) == 1
        assert pq[0][0] == "checkpoint"


class TestRollback:
    def test_rollback_by_id(self):
        engine = CheckpointEngine()
        cp_id = engine.create(action="tool", params={"step": 1})
        engine.create(action="tool", params={"step": 2})
        result = engine.rollback(cp_id)
        assert result.checkpoint.checkpoint_id == cp_id

    def test_rollback_by_name(self):
        engine = CheckpointEngine()
        engine.create(action="tool", params={}, name="safe_point")
        engine.create(action="tool", params={})
        result = engine.rollback("safe_point")
        assert result.checkpoint.name == "safe_point"

    def test_rollback_by_name_returns_most_recent(self):
        engine = CheckpointEngine()
        engine.create(action="tool", params={"v": 1}, name="mark")
        engine.create(action="tool", params={"v": 2}, name="mark")
        result = engine.rollback("mark")
        assert result.checkpoint.params == {"v": 2}

    def test_rollback_not_found_raises(self):
        engine = CheckpointEngine()
        engine.create(action="tool", params={})
        with pytest.raises(CheckpointNotFoundError):
            engine.rollback("nonexistent")

    def test_rollback_searches_storage(self):
        from datetime import datetime, timezone

        from ostiari.models import Checkpoint

        storage = MagicMock()
        cp = Checkpoint(
            checkpoint_id="stored_id",
            sequence_number=1,
            timestamp=datetime.now(timezone.utc),
            state={},
            action="tool",
            params={},
        )
        storage.get_checkpoint.return_value = cp
        engine = CheckpointEngine(storage=storage)
        result = engine.rollback("stored_id")
        assert result.checkpoint.checkpoint_id == "stored_id"

    def test_rollback_restored_at_set(self):
        engine = CheckpointEngine()
        cp_id = engine.create(action="tool", params={})
        result = engine.rollback(cp_id)
        assert result.restored_at is not None


class TestList:
    def test_list_returns_recent(self):
        engine = CheckpointEngine()
        for i in range(5):
            engine.create(action=f"tool_{i}", params={})
        checkpoints = engine.list(limit=3)
        assert len(checkpoints) == 3
        assert checkpoints[-1].action == "tool_4"

    def test_list_empty(self):
        engine = CheckpointEngine()
        assert engine.list() == []


class TestGet:
    def test_get_by_id(self):
        engine = CheckpointEngine()
        cp_id = engine.create(action="tool", params={"key": "val"})
        cp = engine.get(cp_id)
        assert cp.params == {"key": "val"}

    def test_get_not_found_raises(self):
        engine = CheckpointEngine()
        with pytest.raises(CheckpointNotFoundError):
            engine.get("nonexistent")


class TestRetention:
    def test_keep_last_evicts_oldest(self):
        engine = CheckpointEngine(retention=RetentionPolicy(keep_last=3))
        for i in range(5):
            engine.create(action=f"tool_{i}", params={})
        checkpoints = engine.list(limit=100)
        assert len(checkpoints) == 3
        assert checkpoints[0].action == "tool_2"

    def test_keep_named_protects(self):
        engine = CheckpointEngine(retention=RetentionPolicy(keep_last=2, keep_named=True))
        engine.create(action="tool_0", params={}, name="protected")
        engine.create(action="tool_1", params={})
        engine.create(action="tool_2", params={})
        engine.create(action="tool_3", params={})
        checkpoints = engine.list(limit=100)
        named = [cp for cp in checkpoints if cp.name == "protected"]
        assert len(named) == 1

    def test_max_age_evicts_old(self):
        from datetime import datetime, timedelta, timezone

        engine = CheckpointEngine(retention=RetentionPolicy(max_age_hours=1))
        engine.create(action="tool", params={})

        old_cp = engine.list()[0]
        old_timestamp = datetime.now(timezone.utc) - timedelta(hours=2)
        object.__setattr__(old_cp, "timestamp", old_timestamp)

        engine.create(action="tool_new", params={})
        engine._enforce_retention()
        checkpoints = engine.list(limit=100)
        old_ids = [
            cp.checkpoint_id
            for cp in checkpoints
            if cp.action == "tool" and cp.timestamp == old_timestamp
        ]
        assert len(old_ids) == 0

    def test_cleanup_storage_failure_non_blocking(self):
        storage = MagicMock()
        storage.delete_checkpoints.side_effect = RuntimeError("db error")
        engine = CheckpointEngine(
            storage=storage,
            retention=RetentionPolicy(keep_last=1),
        )
        engine.create(action="tool_0", params={})
        engine.create(action="tool_1", params={})


class TestStorageFailure:
    def test_create_succeeds_when_persist_fails(self):
        pq = deque()
        engine = CheckpointEngine(persist_queue=pq)
        cp_id = engine.create(action="tool", params={})
        assert cp_id is not None
        assert len(engine.list()) == 1


class TestAutoEnabled:
    def test_default_enabled(self):
        engine = CheckpointEngine()
        assert engine.auto_enabled is True

    def test_can_disable(self):
        engine = CheckpointEngine()
        engine.auto_enabled = False
        assert engine.auto_enabled is False


class TestConfigure:
    def test_update_retention(self):
        engine = CheckpointEngine()
        engine.configure(RetentionPolicy(keep_last=5))
        assert engine._retention.keep_last == 5
