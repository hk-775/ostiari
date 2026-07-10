"""SQLite storage backend implementation."""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from ostiari.exceptions import (
    CheckpointNotFoundError,
    StorageError,
    StorageMigrationError,
)
from ostiari.models import (
    AnomalySignal,
    BreakerState,
    Checkpoint,
    CheckpointID,
    RiskSignal,
    TraceEntry,
    TraceFilters,
)
from ostiari.storage.migrations import run_migrations
from ostiari.storage.redaction import RedactionFilter

logger = logging.getLogger("ostiari")

T = TypeVar("T")

RETRY_DELAYS = [0.1, 0.5, 2.0]
LOCK_TIMEOUT = 5.0
LOCK_POLL_INTERVAL = 0.1


# --- Platform-specific file locking ---

if sys.platform == "win32":
    import msvcrt

    def _lock_file(f: Any) -> None:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(f: Any) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(f: Any) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(f: Any) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# --- Migration Lock ---


class MigrationLock:
    """File-based lock covering the entire init sequence."""

    def __init__(self, lock_path: Path, timeout: float = LOCK_TIMEOUT) -> None:
        self._lock_path = lock_path
        self._timeout = timeout
        self._file: Any = None

    def __enter__(self) -> MigrationLock:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._lock_path, "w")
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                _lock_file(self._file)
                return self
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    self._file.close()
                    raise StorageMigrationError(
                        from_version=0,
                        to_version=0,
                        reason=f"Cannot acquire migration lock after {self._timeout}s",
                    ) from None
                time.sleep(LOCK_POLL_INTERVAL)

    def __exit__(self, *exc: Any) -> None:
        if self._file:
            _unlock_file(self._file)
            self._file.close()
            self._file = None


# --- Connection Manager ---


class ConnectionManager:
    """Manages per-thread SQLite connections with lazy initialization."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._local = threading.local()
        self._all_connections: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self._closed = False

    @property
    def connection(self) -> sqlite3.Connection:
        if self._closed:
            raise StorageError(operation="connect", reason="Backend is closed")
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self._path))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
            with self._lock:
                self._all_connections.append(conn)
        result: sqlite3.Connection = self._local.conn
        return result

    def close_all(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            for conn in self._all_connections:
                with contextlib.suppress(Exception):
                    conn.close()
            self._all_connections.clear()
        self._local.conn = None


# --- Retry Executor ---


class RetryExecutor:
    """Wraps storage I/O with retry logic and fail-open behavior."""

    def __init__(self, fail_open: bool, delays: list[float] | None = None) -> None:
        self._fail_open = fail_open
        self._delays = delays if delays is not None else RETRY_DELAYS

    def execute(self, operation: Callable[..., T], *args: Any, **kwargs: Any) -> T | None:
        last_error: Exception | None = None
        for attempt, delay in enumerate(self._delays):
            try:
                return operation(*args, **kwargs)
            except sqlite3.IntegrityError:
                raise
            except sqlite3.OperationalError as e:
                last_error = e
                logger.warning("Storage retry %d/%d: %s", attempt + 1, len(self._delays), e)
                time.sleep(delay)
        if self._fail_open:
            logger.error(
                "Storage operation failed after %d retries: %s",
                len(self._delays),
                last_error,
            )
            return None
        raise StorageError(operation="write", reason=str(last_error))


# --- SQLite Backend ---


class SQLiteBackend:
    """SQLite storage backend with WAL mode, thread-safety, and auto-migration."""

    def __init__(
        self,
        path: str | Path = "ostiari.db",
        fail_open: bool = True,
        redact_patterns: list[str] | None = None,
    ) -> None:
        self._path = Path(path).expanduser().resolve()
        self._ensure_path()
        self._connections = ConnectionManager(self._path)
        self._retry = RetryExecutor(fail_open=fail_open)
        self._redaction = RedactionFilter(patterns=redact_patterns)
        self._fail_open = fail_open
        self._init_schema()
        logger.info("Storage initialized at %s", self._path)

    def _ensure_path(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _init_schema(self) -> None:
        lock_path = self._path.with_suffix(".db.lock")
        with MigrationLock(lock_path):
            conn = self._connections.connection
            run_migrations(conn)

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._connections.connection

    # --- Trace Operations ---

    def save_trace(self, entry: TraceEntry) -> None:
        self._retry.execute(self._do_save_trace, entry)

    def _do_save_trace(self, entry: TraceEntry) -> None:
        self._conn.execute(
            "INSERT INTO traces "
            "(trace_id, correlation_id, timestamp, action, params, result, "
            "risk_score, tier, duration_ms, signals, anomalies, breaker_state, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._serialize_trace(entry),
        )
        self._conn.commit()

    def save_traces_batch(self, entries: list[TraceEntry]) -> None:
        self._retry.execute(self._do_save_traces_batch, entries)

    def _do_save_traces_batch(self, entries: list[TraceEntry]) -> None:
        conn = self._conn
        conn.execute("BEGIN")
        try:
            conn.executemany(
                "INSERT INTO traces "
                "(trace_id, correlation_id, timestamp, action, params, result, "
                "risk_score, tier, duration_ms, signals, anomalies, breaker_state, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [self._serialize_trace(e) for e in entries],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def get_traces(self, filters: TraceFilters) -> list[TraceEntry]:
        conditions: list[str] = []
        params: list[Any] = []

        if filters.start_time is not None:
            conditions.append("timestamp >= ?")
            params.append(filters.start_time.isoformat())
        if filters.end_time is not None:
            conditions.append("timestamp <= ?")
            params.append(filters.end_time.isoformat())
        if filters.action is not None:
            conditions.append("action LIKE ?")
            params.append(_glob_to_sql(filters.action))
        if filters.min_risk is not None:
            conditions.append("risk_score >= ?")
            params.append(filters.min_risk)
        if filters.max_risk is not None:
            conditions.append("risk_score <= ?")
            params.append(filters.max_risk)
        if filters.tier is not None:
            conditions.append("tier = ?")
            params.append(filters.tier)
        if filters.correlation_id is not None:
            conditions.append("correlation_id = ?")
            params.append(filters.correlation_id)

        sql = "SELECT * FROM traces"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.append(filters.limit)
        params.append(filters.offset)

        rows = self._conn.execute(sql, params).fetchall()
        return [self._deserialize_trace(row) for row in rows]

    def get_trace(self, trace_id: str) -> TraceEntry | None:
        row = self._conn.execute("SELECT * FROM traces WHERE trace_id = ?", (trace_id,)).fetchone()
        if row is None:
            return None
        return self._deserialize_trace(row)

    # --- Checkpoint Operations ---

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        self._retry.execute(self._do_save_checkpoint, checkpoint)

    def _do_save_checkpoint(self, checkpoint: Checkpoint) -> None:
        self._conn.execute(
            "INSERT INTO checkpoints "
            "(checkpoint_id, name, sequence_number, timestamp, state, action, params, result) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                checkpoint.checkpoint_id,
                checkpoint.name,
                checkpoint.sequence_number,
                checkpoint.timestamp.isoformat(),
                json.dumps(checkpoint.state),
                checkpoint.action,
                json.dumps(self._redaction.redact(checkpoint.params)),
                json.dumps(self._redaction.redact(checkpoint.result))
                if checkpoint.result is not None
                else None,
            ),
        )
        self._conn.commit()

    def get_checkpoint(self, checkpoint_id: CheckpointID) -> Checkpoint:
        row = self._conn.execute(
            "SELECT * FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
        ).fetchone()
        if row is None:
            raise CheckpointNotFoundError(checkpoint_id)
        return self._deserialize_checkpoint(row)

    def delete_checkpoints(self, ids: list[CheckpointID]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        self._conn.execute(f"DELETE FROM checkpoints WHERE checkpoint_id IN ({placeholders})", ids)
        self._conn.commit()

    # --- Breaker State Operations ---

    def save_breaker_state(self, state: BreakerState) -> None:
        self._retry.execute(self._do_save_breaker_state, state)

    def _do_save_breaker_state(self, state: BreakerState) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO breaker_states "
            "(breaker_id, state, tripped_at, last_checked, metrics, "
            "recovery_mode, recovery_after_seconds) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                state.breaker_id,
                state.state,
                state.tripped_at.isoformat() if state.tripped_at else None,
                state.last_checked.isoformat(),
                json.dumps(state.metrics),
                state.recovery_mode,
                state.recovery_after_seconds,
            ),
        )
        self._conn.commit()

    def get_breaker_state(self, breaker_id: str) -> BreakerState | None:
        row = self._conn.execute(
            "SELECT * FROM breaker_states WHERE breaker_id = ?", (breaker_id,)
        ).fetchone()
        if row is None:
            return None
        return self._deserialize_breaker_state(row)

    # --- Lifecycle ---

    def close(self) -> None:
        self._connections.close_all()

    def migrate(self) -> None:
        run_migrations(self._conn)

    def schema_version(self) -> int:
        row = self._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return row[0] if row[0] is not None else 0

    # --- Serialization Helpers ---

    def _serialize_trace(self, entry: TraceEntry) -> tuple[Any, ...]:
        return (
            entry.trace_id,
            entry.correlation_id,
            entry.timestamp.isoformat(),
            entry.action,
            json.dumps(self._redaction.redact(entry.params)),
            json.dumps(self._redaction.redact(entry.result)) if entry.result is not None else None,
            entry.risk_score,
            entry.tier,
            entry.duration_ms,
            json.dumps([s.model_dump() for s in entry.signals]),
            json.dumps([a.model_dump() for a in entry.anomalies]),
            entry.breaker_state,
            json.dumps(entry.metadata),
        )

    def _deserialize_trace(self, row: sqlite3.Row) -> TraceEntry:
        from datetime import datetime

        return TraceEntry(
            trace_id=row["trace_id"],
            correlation_id=row["correlation_id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            action=row["action"],
            params=json.loads(row["params"]),
            result=json.loads(row["result"]) if row["result"] else None,
            risk_score=row["risk_score"],
            tier=row["tier"],
            duration_ms=row["duration_ms"],
            signals=[RiskSignal(**s) for s in json.loads(row["signals"])],
            anomalies=[AnomalySignal(**a) for a in json.loads(row["anomalies"])],
            breaker_state=row["breaker_state"],
            metadata=json.loads(row["metadata"]),
        )

    def _deserialize_checkpoint(self, row: sqlite3.Row) -> Checkpoint:
        from datetime import datetime

        return Checkpoint(
            checkpoint_id=row["checkpoint_id"],
            name=row["name"],
            sequence_number=row["sequence_number"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            state=json.loads(row["state"]),
            action=row["action"],
            params=json.loads(row["params"]),
            result=json.loads(row["result"]) if row["result"] else None,
        )

    def _deserialize_breaker_state(self, row: sqlite3.Row) -> BreakerState:
        from datetime import datetime

        return BreakerState(
            breaker_id=row["breaker_id"],
            state=row["state"],
            tripped_at=datetime.fromisoformat(row["tripped_at"]) if row["tripped_at"] else None,
            last_checked=datetime.fromisoformat(row["last_checked"]),
            metrics=json.loads(row["metrics"]),
            recovery_mode=row["recovery_mode"],
            recovery_after_seconds=row["recovery_after_seconds"],
        )


def _glob_to_sql(pattern: str) -> str:
    result = pattern.replace("%", "\\%").replace("_", "\\_")
    result = result.replace("*", "%").replace("?", "_")
    return result
