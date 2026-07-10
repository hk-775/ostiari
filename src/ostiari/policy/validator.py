"""Policy validation — schema, semantic, and capacity checks."""

from __future__ import annotations

import logging
from typing import Any
from zoneinfo import available_timezones

from ostiari.exceptions import PolicyValidationError
from ostiari.models import (
    ContextCondition,
    PolicySet,
    Rule,
    ThresholdConfig,
    ThresholdOverrides,
)
from ostiari.policy.parser import ParsedYAML

logger = logging.getLogger("ostiari")

_VALID_RULE_TYPES = {"allow", "block", "risk_adjust", "threshold_override", "context_rule"}
_VALID_CONTEXT_TYPES = {"repetition", "escalation", "time_of_day"}
_MAX_RULES_PER_FILE = 500
_MAX_PATTERN_LENGTH = 256
_AVAILABLE_TIMEZONES = available_timezones()


def validate_policy(parsed: ParsedYAML) -> PolicySet:
    """Validate a ParsedYAML and return a PolicySet. Raises on any error."""
    errors = _collect_errors(parsed)
    if errors:
        raise (
            errors[0]
            if len(errors) == 1
            else PolicyValidationError(
                field=errors[0].field,
                message=errors[0].message,
                suggestion=errors[0].suggestion,
                line=errors[0].line,
                additional_errors=errors[1:],
            )
        )
    return _build_policy_set(parsed)


def validate_policy_collected(
    parsed: ParsedYAML,
) -> tuple[PolicySet | None, list[PolicyValidationError]]:
    """Validate without raising. Returns (PolicySet, []) on success or (None, errors) on failure."""
    errors = _collect_errors(parsed)
    if errors:
        return None, errors
    return _build_policy_set(parsed), []


def _collect_errors(parsed: ParsedYAML) -> list[PolicyValidationError]:
    errors: list[PolicyValidationError] = []
    errors.extend(_validate_allow_block(parsed))
    errors.extend(_validate_rules(parsed))
    errors.extend(_validate_thresholds(parsed))
    errors.extend(_validate_capacity(parsed))
    errors.extend(_validate_cross_rules(parsed))
    return errors


def _validate_allow_block(parsed: ParsedYAML) -> list[PolicyValidationError]:
    errors: list[PolicyValidationError] = []
    for i, pattern in enumerate(parsed.allow):
        err = _validate_pattern(pattern, f"allow[{i}]", parsed.line_map.get(f"allow[{i}]"))
        if err:
            errors.append(err)

    for i, pattern in enumerate(parsed.block):
        err = _validate_pattern(pattern, f"block[{i}]", parsed.line_map.get(f"block[{i}]"))
        if err:
            errors.append(err)

    allow_set = set(parsed.allow)
    block_set = set(parsed.block)
    conflicts = allow_set & block_set
    for pattern in sorted(conflicts):
        errors.append(
            PolicyValidationError(
                field="allow/block conflict",
                message=f"Action '{pattern}' appears in both allow and block lists",
                suggestion="Remove from one list — an action cannot be both allowed and blocked.",
            )
        )
    return errors


def _validate_rules(parsed: ParsedYAML) -> list[PolicyValidationError]:
    errors: list[PolicyValidationError] = []
    for i, raw_rule in enumerate(parsed.rules):
        prefix = f"rules[{i}]"
        line = parsed.line_map.get(prefix)

        if not isinstance(raw_rule, dict):
            errors.append(
                PolicyValidationError(
                    field=prefix,
                    line=line,
                    message="Rule must be a mapping",
                    suggestion="Each rule should have 'type' and 'action' fields.",
                )
            )
            continue

        rule_type = raw_rule.get("type")
        if rule_type not in _VALID_RULE_TYPES:
            errors.append(
                PolicyValidationError(
                    field=f"{prefix}.type",
                    line=parsed.line_map.get(f"{prefix}.type", line),
                    message=f"Invalid rule type '{rule_type}'",
                    suggestion=f"Must be one of: {', '.join(sorted(_VALID_RULE_TYPES))}",
                )
            )
            continue

        action = raw_rule.get("action")
        if not action or not isinstance(action, str):
            errors.append(
                PolicyValidationError(
                    field=f"{prefix}.action",
                    line=parsed.line_map.get(f"{prefix}.action", line),
                    message="'action' field is required and must be a non-empty string",
                    suggestion="Set 'action' to a glob pattern (e.g., 'send_*', '*').",
                )
            )
            continue

        err = _validate_pattern(
            action, f"{prefix}.action", parsed.line_map.get(f"{prefix}.action", line)
        )
        if err:
            errors.append(err)
            continue

        if rule_type == "risk_adjust":
            risk_val = raw_rule.get("risk_adjust")
            if risk_val is None or not isinstance(risk_val, int):
                errors.append(
                    PolicyValidationError(
                        field=f"{prefix}.risk_adjust",
                        line=parsed.line_map.get(f"{prefix}.risk_adjust", line),
                        message="'risk_adjust' must be a non-zero integer",
                        suggestion="Set to a positive or negative integer (e.g., 20 or -10).",
                    )
                )
            elif risk_val == 0:
                errors.append(
                    PolicyValidationError(
                        field=f"{prefix}.risk_adjust",
                        line=parsed.line_map.get(f"{prefix}.risk_adjust", line),
                        message="risk_adjust of 0 has no effect",
                        suggestion="Remove the rule or set a non-zero value.",
                    )
                )

        elif rule_type == "threshold_override":
            threshold_data = raw_rule.get("threshold_override")
            if not isinstance(threshold_data, dict):
                errors.append(
                    PolicyValidationError(
                        field=f"{prefix}.threshold_override",
                        line=parsed.line_map.get(f"{prefix}.threshold_override", line),
                        message="'threshold_override' must be a mapping with 'allow_max' and 'intervene_max'",
                        suggestion="Set threshold_override: {allow_max: 30, intervene_max: 70}",
                    )
                )
            else:
                err = _validate_threshold_data(threshold_data, f"{prefix}.threshold_override", line)
                if err:
                    errors.append(err)

        elif rule_type == "context_rule":
            context_data = raw_rule.get("context")
            if not isinstance(context_data, dict):
                errors.append(
                    PolicyValidationError(
                        field=f"{prefix}.context",
                        line=parsed.line_map.get(f"{prefix}.context", line),
                        message="'context' field required for context_rule type",
                        suggestion="Add context: {type: repetition, count: 5, window_seconds: 60, risk_adjust: 20}",
                    )
                )
            else:
                ctx_errors = _validate_context_data(context_data, f"{prefix}.context", line)
                errors.extend(ctx_errors)

    return errors


def _validate_thresholds(parsed: ParsedYAML) -> list[PolicyValidationError]:
    errors: list[PolicyValidationError] = []
    if not parsed.thresholds:
        return errors

    global_data = parsed.thresholds.get("global")
    if global_data is not None:
        if not isinstance(global_data, dict):
            errors.append(
                PolicyValidationError(
                    field="thresholds.global",
                    line=parsed.line_map.get("thresholds.global"),
                    message="'global' must be a mapping",
                    suggestion="Set thresholds.global: {allow_max: 30, intervene_max: 70}",
                )
            )
        else:
            err = _validate_threshold_data(
                global_data, "thresholds.global", parsed.line_map.get("thresholds.global")
            )
            if err:
                errors.append(err)

    per_tool = parsed.thresholds.get("per_tool")
    if per_tool is not None:
        if not isinstance(per_tool, dict):
            errors.append(
                PolicyValidationError(
                    field="thresholds.per_tool",
                    line=parsed.line_map.get("thresholds.per_tool"),
                    message="'per_tool' must be a mapping of action patterns to threshold configs",
                    suggestion="Set thresholds.per_tool: {send_email: {allow_max: 10, intervene_max: 30}}",
                )
            )
        else:
            for key, val in per_tool.items():
                err = _validate_pattern(
                    key,
                    f"thresholds.per_tool.{key}",
                    parsed.line_map.get(f"thresholds.per_tool.{key}"),
                )
                if err:
                    errors.append(err)
                if isinstance(val, dict):
                    err = _validate_threshold_data(
                        val,
                        f"thresholds.per_tool.{key}",
                        parsed.line_map.get(f"thresholds.per_tool.{key}"),
                    )
                    if err:
                        errors.append(err)
    return errors


def _validate_capacity(parsed: ParsedYAML) -> list[PolicyValidationError]:
    total = len(parsed.allow) + len(parsed.block) + len(parsed.rules)
    if total > _MAX_RULES_PER_FILE:
        return [
            PolicyValidationError(
                field="rules",
                message=f"Policy file contains {total} rules (max {_MAX_RULES_PER_FILE})",
                suggestion="Split into multiple policy files and load with multiple paths.",
            )
        ]
    return []


def _validate_cross_rules(parsed: ParsedYAML) -> list[PolicyValidationError]:
    seen: dict[tuple[str, str], int] = {}
    for i, raw_rule in enumerate(parsed.rules):
        if not isinstance(raw_rule, dict):
            continue
        key = (raw_rule.get("action", ""), raw_rule.get("type", ""))
        if key in seen:
            logger.warning(
                "Duplicate rule for action '%s' type '%s' at index %d (first at %d)",
                key[0],
                key[1],
                i,
                seen[key],
            )
        else:
            seen[key] = i
    return []


def _validate_pattern(pattern: Any, field: str, line: int | None) -> PolicyValidationError | None:
    if not isinstance(pattern, str) or not pattern:
        return PolicyValidationError(
            field=field,
            line=line,
            message="Pattern must be a non-empty string",
            suggestion="Set to a glob pattern (e.g., 'send_*', 'db.*', '*').",
        )
    if len(pattern) > _MAX_PATTERN_LENGTH:
        return PolicyValidationError(
            field=field,
            line=line,
            message=f"Pattern exceeds {_MAX_PATTERN_LENGTH} characters",
            suggestion="Shorten the pattern or use a broader glob.",
        )
    if "\x00" in pattern:
        return PolicyValidationError(
            field=field,
            line=line,
            message="Pattern contains null byte",
            suggestion="Remove null bytes from the pattern.",
        )
    return None


def _validate_threshold_data(
    data: dict[str, Any], field: str, line: int | None
) -> PolicyValidationError | None:
    allow_max = data.get("allow_max", 30)
    intervene_max = data.get("intervene_max", 70)
    if not isinstance(allow_max, int) or not isinstance(intervene_max, int):
        return PolicyValidationError(
            field=field,
            line=line,
            message="allow_max and intervene_max must be integers",
            suggestion="Set integer values between 0 and 100.",
        )
    if allow_max >= intervene_max:
        return PolicyValidationError(
            field=field,
            line=line,
            message=f"allow_max ({allow_max}) must be less than intervene_max ({intervene_max})",
            suggestion="Ensure allow_max < intervene_max (e.g., allow_max: 30, intervene_max: 70).",
        )
    return None


def _validate_context_data(
    data: dict[str, Any], field: str, line: int | None
) -> list[PolicyValidationError]:
    errors: list[PolicyValidationError] = []
    ctx_type = data.get("type")
    if ctx_type not in _VALID_CONTEXT_TYPES:
        errors.append(
            PolicyValidationError(
                field=f"{field}.type",
                line=line,
                message=f"Invalid context type '{ctx_type}'",
                suggestion=f"Must be one of: {', '.join(sorted(_VALID_CONTEXT_TYPES))}",
            )
        )
        return errors

    risk_adj = data.get("risk_adjust")
    if risk_adj is None or not isinstance(risk_adj, int) or risk_adj == 0:
        errors.append(
            PolicyValidationError(
                field=f"{field}.risk_adjust",
                line=line,
                message="'risk_adjust' must be a non-zero integer",
                suggestion="Set the risk score delta (e.g., 40 or -20).",
            )
        )

    if ctx_type == "repetition":
        count = data.get("count")
        if count is None or not isinstance(count, int) or count < 2:
            errors.append(
                PolicyValidationError(
                    field=f"{field}.count",
                    line=line,
                    message="'count' must be an integer >= 2",
                    suggestion="Set the repetition threshold (e.g., count: 5).",
                )
            )
        window = data.get("window_seconds")
        if window is None or not isinstance(window, int) or window <= 0:
            errors.append(
                PolicyValidationError(
                    field=f"{field}.window_seconds",
                    line=line,
                    message="'window_seconds' must be a positive integer",
                    suggestion="Set the time window in seconds (e.g., window_seconds: 60).",
                )
            )
    elif ctx_type == "escalation":
        preceding = data.get("preceding_action")
        if not preceding or not isinstance(preceding, str):
            errors.append(
                PolicyValidationError(
                    field=f"{field}.preceding_action",
                    line=line,
                    message="'preceding_action' is required for escalation rules",
                    suggestion="Set the glob pattern for the preceding action (e.g., 'read_*').",
                )
            )
    elif ctx_type == "time_of_day":
        hours = data.get("outside_hours")
        if not isinstance(hours, (list, tuple)) or len(hours) != 2:
            errors.append(
                PolicyValidationError(
                    field=f"{field}.outside_hours",
                    line=line,
                    message="'outside_hours' must be [start_hour, end_hour]",
                    suggestion="Set business hours (e.g., outside_hours: [9, 17]).",
                )
            )
        else:
            start, end = hours
            if not (
                isinstance(start, int)
                and isinstance(end, int)
                and 0 <= start <= 23
                and 0 <= end <= 23
            ):
                errors.append(
                    PolicyValidationError(
                        field=f"{field}.outside_hours",
                        line=line,
                        message="Hours must be integers 0-23",
                        suggestion="Use 24-hour format (e.g., [9, 17] for 9am-5pm).",
                    )
                )
            elif start == end:
                errors.append(
                    PolicyValidationError(
                        field=f"{field}.outside_hours",
                        line=line,
                        message="Start and end hours must differ",
                        suggestion="Set different values (e.g., [9, 17]).",
                    )
                )

        tz = data.get("timezone", "UTC")
        if tz not in _AVAILABLE_TIMEZONES:
            errors.append(
                PolicyValidationError(
                    field=f"{field}.timezone",
                    line=line,
                    message=f"Unknown timezone '{tz}'",
                    suggestion="Use IANA timezone names (e.g., 'America/New_York', 'UTC').",
                )
            )

    return errors


def _build_policy_set(parsed: ParsedYAML) -> PolicySet:
    """Convert validated ParsedYAML into a PolicySet."""
    from datetime import datetime, timezone

    rules: list[Rule] = []

    for pattern in parsed.block:
        rules.append(Rule(type="block", action=pattern))

    for pattern in parsed.allow:
        rules.append(Rule(type="allow", action=pattern))

    for raw_rule in parsed.rules:
        rules.append(_build_rule(raw_rule))

    type_order = {
        "block": 0,
        "allow": 1,
        "risk_adjust": 2,
        "threshold_override": 3,
        "context_rule": 4,
    }
    rules.sort(key=lambda r: (-r.priority, type_order.get(r.type, 99)))

    thresholds = _build_thresholds(parsed.thresholds)

    return PolicySet(
        rules=rules,
        thresholds=thresholds,
        source=str(parsed.source_path),
        loaded_at=datetime.now(timezone.utc),
    )


def _build_rule(raw: dict[str, Any]) -> Rule:
    kwargs: dict[str, Any] = {
        "type": raw["type"],
        "action": raw["action"],
        "priority": raw.get("priority", 0),
        "description": raw.get("description"),
        "enabled": raw.get("enabled", True),
    }

    if raw["type"] == "risk_adjust":
        kwargs["risk_adjust"] = raw["risk_adjust"]
    elif raw["type"] == "threshold_override":
        td = raw["threshold_override"]
        kwargs["threshold_override"] = ThresholdConfig(
            allow_max=td.get("allow_max", 30),
            intervene_max=td.get("intervene_max", 70),
        )
    elif raw["type"] == "context_rule":
        ctx = raw["context"]
        ctx_kwargs: dict[str, Any] = {
            "type": ctx["type"],
            "risk_adjust": ctx["risk_adjust"],
        }
        if ctx["type"] == "repetition":
            ctx_kwargs["count"] = ctx["count"]
            ctx_kwargs["window_seconds"] = ctx["window_seconds"]
        elif ctx["type"] == "escalation":
            ctx_kwargs["preceding_action"] = ctx["preceding_action"]
            ctx_kwargs["preceding_resource"] = ctx.get("preceding_resource")
        elif ctx["type"] == "time_of_day":
            ctx_kwargs["outside_hours"] = tuple(ctx["outside_hours"])
            ctx_kwargs["timezone"] = ctx.get("timezone", "UTC")
        kwargs["context"] = ContextCondition(**ctx_kwargs)

    return Rule(**kwargs)


def _build_thresholds(data: dict[str, Any]) -> ThresholdOverrides:
    if not data:
        return ThresholdOverrides()

    global_data = data.get("global")
    global_config = (
        ThresholdConfig(
            allow_max=global_data.get("allow_max", 30),
            intervene_max=global_data.get("intervene_max", 70),
        )
        if isinstance(global_data, dict)
        else ThresholdConfig()
    )

    per_tool: dict[str, ThresholdConfig] = {}
    per_tool_data = data.get("per_tool")
    if isinstance(per_tool_data, dict):
        for key, val in per_tool_data.items():
            if isinstance(val, dict):
                per_tool[key] = ThresholdConfig(
                    allow_max=val.get("allow_max", 30),
                    intervene_max=val.get("intervene_max", 70),
                )

    return ThresholdOverrides(global_thresholds=global_config, per_tool=per_tool)
