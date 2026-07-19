"""Ostiari shared data models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

CheckpointID = str


class MetricType(str, Enum):
    TOKEN_COST = "token_cost"
    WALL_CLOCK_MS = "wall_clock_ms"
    ERROR_COUNT = "error_count"
    CONSECUTIVE_FAILURES = "consecutive_failures"
    TOTAL_ACTIONS = "total_actions"


# --- Core Decision Entities ---


class RiskSignal(BaseModel, frozen=True):
    source: str = Field(min_length=1)
    score_contribution: int = Field(ge=-100, le=100)
    description: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AnomalySignal(BaseModel, frozen=True):
    detector: str = Field(min_length=1)
    severity: Literal["low", "medium", "high", "critical"]
    score_contribution: int = Field(ge=0, le=100)
    description: str = Field(min_length=1)
    evidence: dict[str, Any] = Field(default_factory=dict)


class ThresholdConfig(BaseModel, frozen=True):
    allow_max: int = Field(default=30, ge=0, le=100)
    intervene_max: int = Field(default=70, ge=0, le=100)

    @model_validator(mode="after")
    def _check_ordering(self) -> ThresholdConfig:
        if self.allow_max >= self.intervene_max:
            raise ValueError(
                f"allow_max ({self.allow_max}) must be less than "
                f"intervene_max ({self.intervene_max})"
            )
        return self


class GatewayDecision(BaseModel, frozen=True):
    tier: Literal["allow", "intervene", "block"]
    score: int = Field(ge=0, le=100)
    signals: list[RiskSignal] = Field(default_factory=list)
    rule_triggered: str | None = None
    threshold_applied: ThresholdConfig


class ValidationResult(BaseModel, frozen=True):
    tier: Literal["allow", "intervene", "block"]
    # The gateway's raw tier before any in-process intervention handling. When
    # the Guard resolves an intervene internally (via a callback), `tier`
    # collapses to allow/block but `original_tier` preserves "intervene" so an
    # external caller (e.g. the sidecar's human-in-the-loop gate) can see it.
    original_tier: Literal["allow", "intervene", "block"] = "allow"
    score: int = Field(ge=0, le=100)
    signals: list[RiskSignal] = Field(default_factory=list)
    trace_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float = Field(ge=0)
    rule_triggered: str | None = None


# --- Policy Entities ---

RuleType = Literal["allow", "block", "risk_adjust", "threshold_override", "context_rule"]
ContextType = Literal["repetition", "escalation", "time_of_day"]


class ContextCondition(BaseModel, frozen=True):
    type: ContextType
    risk_adjust: int

    # Repetition fields
    count: int | None = None
    window_seconds: int | None = None

    # Escalation fields
    preceding_action: str | None = None
    preceding_resource: str | None = None

    # Time-of-day fields
    outside_hours: tuple[int, int] | None = None
    timezone: str = "UTC"

    @model_validator(mode="after")
    def _validate_context_fields(self) -> ContextCondition:
        if self.type == "repetition":
            if self.count is None or self.count < 2:
                raise ValueError("count must be >= 2 for repetition rules")
            if self.window_seconds is None or self.window_seconds <= 0:
                raise ValueError("window_seconds must be > 0 for repetition rules")
        elif self.type == "escalation":
            if not self.preceding_action:
                raise ValueError("preceding_action required for escalation rules")
        elif self.type == "time_of_day":
            if self.outside_hours is None:
                raise ValueError("outside_hours required for time_of_day rules")
            start, end = self.outside_hours
            if not (0 <= start <= 23 and 0 <= end <= 23):
                raise ValueError("hours must be 0-23")
            if start == end:
                raise ValueError("start and end hours must differ")
        return self


class Rule(BaseModel, frozen=True):
    type: RuleType
    action: str = Field(min_length=1)
    priority: int = 0
    description: str | None = None
    enabled: bool = True

    risk_adjust: int | None = None
    threshold_override: ThresholdConfig | None = None
    context: ContextCondition | None = None

    @model_validator(mode="after")
    def _validate_type_fields(self) -> Rule:
        if self.type == "risk_adjust":
            if self.risk_adjust is None:
                raise ValueError("risk_adjust field required for type='risk_adjust'")
            if self.risk_adjust == 0:
                raise ValueError("risk_adjust must be non-zero")
        if self.type == "threshold_override" and self.threshold_override is None:
            raise ValueError("threshold_override field required for type='threshold_override'")
        if self.type == "context_rule" and self.context is None:
            raise ValueError("context field required for type='context_rule'")
        return self


class ThresholdOverrides(BaseModel, frozen=True):
    global_thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    per_tool: dict[str, ThresholdConfig] = Field(default_factory=dict)


class PolicySet(BaseModel, frozen=True):
    rules: list[Rule] = Field(default_factory=list)
    thresholds: ThresholdOverrides = Field(default_factory=ThresholdOverrides)
    source: str = ""
    loaded_at: datetime | None = None


class RiskAdjustment(BaseModel, frozen=True):
    delta: int
    source_rule: Rule
    reason: str = Field(min_length=1)


class PolicyResult(BaseModel, frozen=True):
    decision: Literal["allow", "block", "evaluate"]
    matching_rules: list[Rule] = Field(default_factory=list)
    risk_adjustments: list[RiskAdjustment] = Field(default_factory=list)
    effective_thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    blocked_by: Rule | None = None


class EvalContext(BaseModel):
    history: list[TraceEntry] = Field(default_factory=list)
    current_time: datetime | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolicySource(BaseModel, frozen=True):
    origin: Literal["yaml", "decorator", "programmatic"]
    path: str | None = None
    priority: int = 0


# --- Trace Entities ---


class TraceEntry(BaseModel, frozen=True):
    trace_id: str = Field(min_length=1)
    correlation_id: str | None = None
    timestamp: datetime
    agent_id: str = ""              # calling agent — lets anomaly history be per-agent
    action: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    result: Any | None = None
    risk_score: int = Field(ge=0, le=100)
    tier: Literal["allow", "intervene", "block"]
    duration_ms: float = Field(ge=0)
    signals: list[RiskSignal] = Field(default_factory=list)
    anomalies: list[AnomalySignal] = Field(default_factory=list)
    breaker_state: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TraceFilters(BaseModel):
    start_time: datetime | None = None
    end_time: datetime | None = None
    action: str | None = None
    min_risk: int | None = Field(default=None, ge=0, le=100)
    max_risk: int | None = Field(default=None, ge=0, le=100)
    tier: Literal["allow", "intervene", "block"] | None = None
    correlation_id: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check_time_range(self) -> TraceFilters:
        if (
            self.start_time is not None
            and self.end_time is not None
            and self.start_time > self.end_time
        ):
            raise ValueError("start_time must be ≤ end_time")
        return self


class TraceStats(BaseModel, frozen=True):
    total_actions: int = Field(ge=0)
    allowed: int = Field(ge=0)
    intervened: int = Field(ge=0)
    blocked: int = Field(ge=0)
    avg_risk_score: float = Field(ge=0, le=100)
    total_duration_ms: float = Field(ge=0)
    unique_tools: int = Field(ge=0)
    period_start: datetime
    period_end: datetime

    @model_validator(mode="after")
    def _check_counts(self) -> TraceStats:
        if self.allowed + self.intervened + self.blocked != self.total_actions:
            raise ValueError("allowed + intervened + blocked must equal total_actions")
        return self


# --- Checkpoint Entities ---


class Checkpoint(BaseModel, frozen=True):
    checkpoint_id: CheckpointID = Field(min_length=1)
    name: str | None = None
    sequence_number: int = Field(ge=0)
    timestamp: datetime
    state: dict[str, Any] = Field(default_factory=dict)
    action: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    result: Any | None = None


class CheckpointState(BaseModel, frozen=True):
    checkpoint: Checkpoint
    restored_at: datetime


class RetentionPolicy(BaseModel, frozen=True):
    keep_last: int | None = Field(default=100, ge=1)
    max_age_hours: int | None = Field(default=None, ge=1)
    keep_named: bool = True


# --- Circuit Breaker Entities ---


class BreakerState(BaseModel, frozen=True):
    breaker_id: str = Field(min_length=1)
    state: Literal["closed", "open", "half_open"]
    tripped_at: datetime | None = None
    last_checked: datetime
    metrics: dict[str, float] = Field(default_factory=dict)
    recovery_mode: Literal["auto_retry", "notify", "terminate"]
    recovery_after_seconds: int | None = Field(default=None, ge=1)


class BreakerConfig(BaseModel, frozen=True):
    metric: MetricType
    threshold: float = Field(gt=0)
    recovery_mode: Literal["auto_retry", "notify", "terminate"] = "auto_retry"
    recovery_after_seconds: int = Field(default=60, ge=1)


class MetricSummary(BaseModel, frozen=True):
    metric: MetricType
    current_value: float = Field(ge=0)
    threshold: float | None = Field(default=None, ge=0)
    adaptive_threshold: float | None = Field(default=None, ge=0)
    baseline_mean: float | None = None
    baseline_stddev: float | None = Field(default=None, ge=0)
    sample_count: int = Field(default=0, ge=0)


# --- Adapter Entities ---


class AdapterContext(BaseModel, frozen=True):
    framework: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    framework_state: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime


# --- Configuration Entities ---


class OstiariConfig(BaseModel):
    policy_paths: list[str] = Field(default_factory=list)
    storage_backend: str = "sqlite"
    storage_path: str = "ostiari.db"
    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    auto_checkpoint: bool = True
    checkpoint_retention: RetentionPolicy = Field(default_factory=RetentionPolicy)
    breakers: dict[str, BreakerConfig] = Field(default_factory=dict)
    adaptive_enabled: bool = False
    adaptive_sensitivity: float = Field(default=2.0, gt=0)
    adaptive_min_samples: int = Field(default=10, ge=1)
    fail_open: bool = True
    log_level: str = "INFO"
    redact_patterns: list[str] = Field(default_factory=list)
