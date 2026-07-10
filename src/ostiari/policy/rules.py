"""Rule evaluation logic — pattern matching, context conditions, threshold resolution."""

from __future__ import annotations

import fnmatch
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ostiari.models import (
    ContextCondition,
    EvalContext,
    RiskAdjustment,
    Rule,
    ThresholdConfig,
    ThresholdOverrides,
)


def match_rules(action: str, rules: list[Rule]) -> list[Rule]:
    """Return all rules whose action pattern matches the given action."""
    return [rule for rule in rules if rule.enabled and fnmatch.fnmatch(action, rule.action)]


def evaluate_context_condition(
    condition: ContextCondition,
    action: str,
    params: dict[str, Any],
    context: EvalContext,
) -> bool:
    """Determine if a context condition is met."""
    if condition.type == "repetition":
        return _check_repetition(condition, action, context)
    elif condition.type == "escalation":
        return _check_escalation(condition, action, params, context)
    elif condition.type == "time_of_day":
        return _check_time_of_day(condition, context)
    return False


def compute_risk_adjustments(
    matched_rules: list[Rule],
    action: str,
    params: dict[str, Any],
    context: EvalContext,
) -> list[RiskAdjustment]:
    """Compute risk adjustments from matched risk_adjust and context_rule rules."""
    adjustments: list[RiskAdjustment] = []

    for rule in matched_rules:
        if rule.type == "risk_adjust" and rule.risk_adjust is not None:
            adjustments.append(
                RiskAdjustment(
                    delta=rule.risk_adjust,
                    source_rule=rule,
                    reason=f"Static rule: {rule.description or rule.action}",
                )
            )
        elif rule.type == "context_rule" and rule.context is not None:
            met = evaluate_context_condition(rule.context, action, params, context)
            if met:
                adjustments.append(
                    RiskAdjustment(
                        delta=rule.context.risk_adjust,
                        source_rule=rule,
                        reason=_describe_context_match(rule.context),
                    )
                )

    return adjustments


def resolve_thresholds(
    action: str,
    overrides: ThresholdOverrides,
    default: ThresholdConfig,
) -> ThresholdConfig:
    """Resolve effective thresholds for an action (per-tool override or global)."""
    best_match: ThresholdConfig | None = None
    best_specificity = -1

    for pattern, config in overrides.per_tool.items():
        if fnmatch.fnmatch(action, pattern):
            specificity = len(pattern) - pattern.count("*") - pattern.count("?")
            if specificity > best_specificity:
                best_specificity = specificity
                best_match = config

    if best_match is not None:
        return best_match

    return (
        overrides.global_thresholds if overrides.global_thresholds != ThresholdConfig() else default
    )


def _check_repetition(condition: ContextCondition, action: str, context: EvalContext) -> bool:
    current_time = context.current_time or datetime.now(timezone.utc)
    window = condition.window_seconds or 60
    cutoff = current_time - timedelta(seconds=window)

    count = sum(
        1 for entry in context.history if entry.action == action and entry.timestamp >= cutoff
    )
    return count >= (condition.count or 2)


def _check_escalation(
    condition: ContextCondition,
    action: str,
    params: dict[str, Any],
    context: EvalContext,
) -> bool:
    if not condition.preceding_action:
        return False

    for entry in reversed(context.history):
        if fnmatch.fnmatch(entry.action, condition.preceding_action):
            if condition.preceding_resource is None:
                return True
            entry_resource = _extract_resource(entry.params)
            current_resource = _extract_resource(params)
            if (
                entry_resource
                and current_resource
                and fnmatch.fnmatch(entry_resource, condition.preceding_resource)
            ):
                return True
    return False


def _check_time_of_day(condition: ContextCondition, context: EvalContext) -> bool:
    current_time = context.current_time or datetime.now(timezone.utc)

    if condition.timezone and condition.timezone != "UTC":
        try:
            tz = ZoneInfo(condition.timezone)
            localized = current_time.astimezone(tz)
        except (KeyError, ValueError):
            localized = current_time
    else:
        localized = current_time

    current_hour = localized.hour

    if condition.outside_hours is None:
        return False

    start, end = condition.outside_hours

    if start < end:
        outside = current_hour < start or current_hour >= end
    else:
        outside = current_hour >= end and current_hour < start

    return outside


def _extract_resource(params: dict[str, Any]) -> str | None:
    return params.get("resource") or params.get("path") or params.get("target")


def _describe_context_match(condition: ContextCondition) -> str:
    if condition.type == "repetition":
        return (
            f"Repetition: action called >= {condition.count} times in {condition.window_seconds}s"
        )
    elif condition.type == "escalation":
        return f"Escalation: preceded by '{condition.preceding_action}'"
    elif condition.type == "time_of_day":
        start, end = condition.outside_hours or (0, 0)
        return f"Time-of-day: outside business hours ({start}:00-{end}:00)"
    return "Context condition met"
