"""Shared test fixtures for Ostiari tests."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ostiari.anomaly.detector import AnomalyDetector
from ostiari.gateway import ActionGateway
from ostiari.guard import Guard
from ostiari.models import (
    OstiariConfig,
    Checkpoint,
    EvalContext,
    Rule,
    TraceEntry,
)
from ostiari.policy.engine import PolicyEngine
from ostiari.storage.sqlite import SQLiteBackend
from ostiari.tracer import ExecutionTracer


@pytest.fixture
def tmp_db(tmp_path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def storage(tmp_db) -> SQLiteBackend:
    backend = SQLiteBackend(path=tmp_db)
    yield backend
    backend.close()


@pytest.fixture
def config_factory():
    def _make(**kwargs) -> OstiariConfig:
        return OstiariConfig(**kwargs)

    return _make


@pytest.fixture
def trace_factory():
    _counter = [0]

    def _make(**kwargs) -> TraceEntry:
        _counter[0] += 1
        defaults = {
            "trace_id": f"trace_{_counter[0]:04d}",
            "timestamp": datetime(2024, 6, 15, 10, 0, _counter[0] % 60),
            "action": "test_tool",
            "params": {},
            "risk_score": 25,
            "tier": "allow",
            "duration_ms": 1.0,
            "signals": [],
            "anomalies": [],
        }
        defaults.update(kwargs)
        return TraceEntry(**defaults)

    return _make


@pytest.fixture
def checkpoint_factory():
    _counter = [0]

    def _make(**kwargs) -> Checkpoint:
        _counter[0] += 1
        defaults = {
            "checkpoint_id": f"cp_{_counter[0]:04d}",
            "sequence_number": _counter[0],
            "timestamp": datetime(2024, 6, 15, 10, 0, _counter[0] % 60),
            "state": {"step": _counter[0]},
            "action": "test_tool",
            "params": {},
        }
        defaults.update(kwargs)
        return Checkpoint(**defaults)

    return _make


@pytest.fixture
def policy_engine() -> PolicyEngine:
    return PolicyEngine()


@pytest.fixture
def sample_policy_yaml(tmp_path):
    f = tmp_path / "policy.yaml"
    f.write_text("""
allow:
  - safe_read
  - safe_list
block:
  - rm_rf
  - drop_table
rules:
  - type: risk_adjust
    action: send_*
    risk_adjust: 20
  - type: context_rule
    action: "*"
    context:
      type: repetition
      count: 5
      window_seconds: 60
      risk_adjust: 40
thresholds:
  global:
    allow_max: 30
    intervene_max: 70
  per_tool:
    send_email:
      allow_max: 10
      intervene_max: 25
""")
    return f


@pytest.fixture
def sample_eval_context():
    return EvalContext()


@pytest.fixture
def rule_factory():
    def _make(**kwargs) -> Rule:
        defaults = {
            "type": "risk_adjust",
            "action": "*",
            "risk_adjust": 10,
        }
        defaults.update(kwargs)
        return Rule(**defaults)

    return _make


@pytest.fixture
def anomaly_detector() -> AnomalyDetector:
    return AnomalyDetector()


@pytest.fixture
def tool_inventory():
    return {
        "read_file": None,
        "write_file": None,
        "search": None,
        "analyze": None,
        "list_files": None,
    }


@pytest.fixture
def loop_history_factory():
    def _make(action="tool_a", params=None, count=5):
        return [
            TraceEntry(
                trace_id=f"t{i}",
                timestamp=datetime(2026, 5, 9, 10, 0, i % 60, tzinfo=timezone.utc),
                action=action,
                params=params or {},
                risk_score=0,
                tier="allow",
                duration_ms=1.0,
            )
            for i in range(count)
        ]

    return _make


# --- Unit 4: Action Pipeline Fixtures ---


@pytest.fixture
def mock_storage_backend():
    storage = MagicMock()
    storage.get_traces.return_value = []
    storage.save_traces_batch.return_value = None
    return storage


@pytest.fixture
def gateway() -> ActionGateway:
    return ActionGateway()


@pytest.fixture
def tracer(mock_storage_backend) -> ExecutionTracer:
    return ExecutionTracer(storage=mock_storage_backend)


@pytest.fixture
def guard_instance(mock_storage_backend) -> Guard:
    return Guard(storage=mock_storage_backend)


@pytest.fixture
def intervention_callback():
    def _make(approve: bool = True):
        def callback(action, params, score):
            return approve

        return callback

    return _make


# --- Unit 5: Reliability Fixtures ---


@pytest.fixture
def mock_clock():
    time_ref = [0.0]

    class Clock:
        def __call__(self):
            return time_ref[0]

        def advance(self, seconds: float):
            time_ref[0] += seconds

    return Clock()


@pytest.fixture
def breaker_factory(mock_clock):
    from ostiari.breaker import CircuitBreaker
    from ostiari.models import BreakerConfig

    def _make(configs: list[BreakerConfig], **kwargs):
        return CircuitBreaker(configs=configs, clock=mock_clock, **kwargs)

    return _make


@pytest.fixture
def checkpoint_engine():
    from ostiari.checkpoint import CheckpointEngine

    return CheckpointEngine()
