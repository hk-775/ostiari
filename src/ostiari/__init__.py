"""Ostiari — Runtime safety and reliability layer for AI agents."""

from __future__ import annotations

import importlib

from ostiari.anomaly import AnomalyDetector, CustomDetector
from ostiari.breaker import CircuitBreaker
from ostiari.checkpoint import CheckpointEngine
from ostiari.config import ConfigLoader
from ostiari.decorators import protect
from ostiari.exceptions import (
    ActionBlockedError,
    AdapterNotInstalledError,
    AgentTerminatedError,
    BreakerTrippedError,
    CheckpointNotFoundError,
    ConfigError,
    OstiariError,
    StorageError,
)
from ostiari.explain import DecisionExplanation, Factor, explain
from ostiari.gateway import ActionGateway, SignalProvider
from ostiari.guard import Guard
from ostiari.health import HealthChecker
from ostiari.models import (
    AnomalySignal,
    BreakerConfig,
    BreakerState,
    Checkpoint,
    EvalContext,
    GatewayDecision,
    MetricType,
    OstiariConfig,
    PolicyResult,
    PolicySet,
    RetentionPolicy,
    RiskSignal,
    Rule,
    ThresholdConfig,
    TraceEntry,
    TraceFilters,
    TraceStats,
    ValidationResult,
)
from ostiari.policy import PolicyEngine
from ostiari.report import ReportGenerator
from ostiari.storage import SQLiteBackend, StorageBackend
from ostiari.tracer import ExecutionTracer

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "ActionGateway",
    "AnomalyDetector",
    "CheckpointEngine",
    "CircuitBreaker",
    "ConfigLoader",
    "HealthChecker",
    "ReportGenerator",
    "CustomDetector",
    "ExecutionTracer",
    "Guard",
    "PolicyEngine",
    "protect",
    "explain",
    "DecisionExplanation",
    "Factor",
    "SignalProvider",
    "SQLiteBackend",
    "StorageBackend",
    # Models
    "OstiariConfig",
    "AnomalySignal",
    "BreakerConfig",
    "BreakerState",
    "Checkpoint",
    "EvalContext",
    "GatewayDecision",
    "MetricType",
    "PolicyResult",
    "PolicySet",
    "RetentionPolicy",
    "RiskSignal",
    "Rule",
    "ThresholdConfig",
    "TraceEntry",
    "TraceFilters",
    "TraceStats",
    "ValidationResult",
    # Exceptions
    "ActionBlockedError",
    "AdapterNotInstalledError",
    "OstiariError",
    "AgentTerminatedError",
    "BreakerTrippedError",
    "CheckpointNotFoundError",
    "ConfigError",
    "StorageError",
]


def _import_adapter(name: str, extra: str) -> object:
    try:
        return importlib.import_module(f"ostiari.adapters.{name}")
    except ImportError as e:
        raise AdapterNotInstalledError(
            adapter=name,
            install_command=f"pip install ostiari[{extra}]",
        ) from e
