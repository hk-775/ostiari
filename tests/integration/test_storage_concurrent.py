"""Integration tests for concurrent storage access."""

import threading
from datetime import datetime

import pytest

from ostiari.models import TraceEntry, TraceFilters
from ostiari.storage.sqlite import SQLiteBackend


@pytest.fixture
def backend(tmp_path):
    db = SQLiteBackend(path=tmp_path / "concurrent.db")
    yield db
    db.close()


def _make_trace(trace_id: str) -> TraceEntry:
    return TraceEntry(
        trace_id=trace_id,
        timestamp=datetime(2024, 6, 15, 10, 0, 0),
        action="tool",
        params={},
        risk_score=25,
        tier="allow",
        duration_ms=1.0,
        signals=[],
        anomalies=[],
    )


class TestConcurrentWrites:
    def test_multiple_threads_write(self, backend):
        errors: list[Exception] = []
        num_threads = 5
        writes_per_thread = 20

        def writer(thread_id: int):
            try:
                for i in range(writes_per_thread):
                    trace = _make_trace(f"t{thread_id}_{i:03d}")
                    backend.save_trace(trace)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Errors during concurrent writes: {errors}"
        traces = backend.get_traces(TraceFilters(limit=1000))
        assert len(traces) == num_threads * writes_per_thread

    def test_concurrent_read_write(self, backend):
        # Pre-populate some data
        for i in range(10):
            backend.save_trace(_make_trace(f"pre_{i:03d}"))

        errors: list[Exception] = []
        read_results: list[int] = []

        def writer():
            try:
                for i in range(20):
                    backend.save_trace(_make_trace(f"write_{i:03d}"))
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(20):
                    traces = backend.get_traces(TraceFilters(limit=100))
                    read_results.append(len(traces))
            except Exception as e:
                errors.append(e)

        write_thread = threading.Thread(target=writer)
        read_threads = [threading.Thread(target=reader) for _ in range(3)]

        write_thread.start()
        for t in read_threads:
            t.start()
        write_thread.join()
        for t in read_threads:
            t.join()

        assert errors == []
        # All reads should succeed and return >= 10 (pre-populated)
        assert all(r >= 10 for r in read_results)


class TestMigrationLockContention:
    def test_multiple_inits_same_database(self, tmp_path):
        db_path = tmp_path / "shared.db"
        errors: list[Exception] = []
        backends: list[SQLiteBackend] = []
        lock = threading.Lock()

        def init_backend():
            try:
                b = SQLiteBackend(path=db_path)
                with lock:
                    backends.append(b)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=init_backend) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Errors during concurrent init: {errors}"
        # All should have initialized successfully
        assert len(backends) == 5
        # All should report same schema version
        versions = {b.schema_version() for b in backends}
        assert versions == {1}
        for b in backends:
            b.close()
