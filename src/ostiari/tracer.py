"""ExecutionTracer — non-blocking trace recording with background persistence."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from ostiari.models import TraceEntry, TraceFilters, TraceStats

log = logging.getLogger("ostiari")


class ExecutionTracer:
    def __init__(
        self,
        storage: Any,
        queue_max: int = 1000,
        history_max: int = 100,
        batch_size: int = 50,
        flush_interval: float = 1.0,
        persist_queue: deque[tuple[str, object]] | None = None,
    ) -> None:
        self._storage = storage
        self._queue: deque[TraceEntry] = deque(maxlen=queue_max)
        self._history: deque[TraceEntry] = deque(maxlen=history_max)
        self._persist_queue: deque[tuple[str, object]] = (
            persist_queue if persist_queue is not None else deque()
        )
        self._queue_max = queue_max
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._correlation_id: str | None = None
        self._running = False
        self._writer_thread: threading.Thread | None = None

    def record(self, entry: TraceEntry) -> None:
        self._queue.append(entry)
        self._history.append(entry)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._writer_thread = threading.Thread(
            target=self._writer_loop, daemon=True, name="ostiari-tracer"
        )
        self._writer_thread.start()

    def shutdown(self) -> None:
        self._running = False
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=5.0)
            self._writer_thread = None
        self._flush_all()

    def query(self, filters: TraceFilters | None = None) -> list[TraceEntry]:
        result: list[TraceEntry] = self._storage.get_traces(filters or TraceFilters())
        return result

    def export(self, format: str = "json", filters: TraceFilters | None = None) -> bytes:
        traces = self.query(filters)
        if format == "json":
            return self._json_serialize(traces)
        raise ValueError(f"Unsupported export format: {format}")

    def get_stats(self, period: timedelta) -> TraceStats:
        now = datetime.now(timezone.utc)
        filters = TraceFilters(start_time=now - period, end_time=now, limit=1000)
        traces = self.query(filters)
        return self._compute_stats(traces, now - period, now)

    def set_correlation_id(self, correlation_id: str | None) -> None:
        self._correlation_id = correlation_id

    @property
    def correlation_id(self) -> str | None:
        return self._correlation_id

    def recent_history(self, limit: int = 20) -> list[TraceEntry]:
        return list(self._history)[-limit:]

    @property
    def queue_depth(self) -> int:
        return len(self._queue)

    @property
    def persist_queue(self) -> deque[tuple[str, object]]:
        return self._persist_queue

    def _writer_loop(self) -> None:
        while self._running:
            queue_depth = len(self._queue)

            if queue_depth > self._queue_max // 2:
                interval = self._flush_interval / 10
            elif queue_depth > self._queue_max // 4:
                interval = self._flush_interval / 2
            else:
                interval = self._flush_interval

            self._flush_batch()
            self._flush_persist_ops()
            time.sleep(interval)

    def _flush_batch(self) -> None:
        batch: list[TraceEntry] = []
        for _ in range(min(self._batch_size, len(self._queue))):
            if self._queue:
                batch.append(self._queue.popleft())
        if batch:
            try:
                self._storage.save_traces_batch(batch)
            except Exception as e:
                log.warning("Trace flush failed: %s (batch of %d dropped)", e, len(batch))

    def _flush_persist_ops(self) -> None:
        for _ in range(min(50, len(self._persist_queue))):
            if not self._persist_queue:
                break
            op_type, payload = self._persist_queue.popleft()
            try:
                if op_type == "breaker":
                    self._storage.save_breaker_state(payload)
                elif op_type == "checkpoint":
                    self._storage.save_checkpoint(payload)
            except Exception as e:
                log.warning("[Ostiari] Persist op failed (%s): %s", op_type, e)

    def _flush_all(self) -> None:
        while self._queue:
            self._flush_batch()
        self._flush_persist_ops()

    def _json_serialize(self, traces: list[TraceEntry]) -> bytes:
        data = [json.loads(t.model_dump_json()) for t in traces]
        return json.dumps(data, indent=2, default=str).encode("utf-8")

    def _compute_stats(
        self, traces: list[TraceEntry], period_start: datetime, period_end: datetime
    ) -> TraceStats:
        total = len(traces)
        allowed = sum(1 for t in traces if t.tier == "allow")
        intervened = sum(1 for t in traces if t.tier == "intervene")
        blocked = sum(1 for t in traces if t.tier == "block")
        avg_risk = sum(t.risk_score for t in traces) / total if total > 0 else 0.0
        total_duration = sum(t.duration_ms for t in traces)
        unique_tools = len({t.action for t in traces})

        return TraceStats(
            total_actions=total,
            allowed=allowed,
            intervened=intervened,
            blocked=blocked,
            avg_risk_score=avg_risk,
            total_duration_ms=total_duration,
            unique_tools=unique_tools,
            period_start=period_start,
            period_end=period_end,
        )
