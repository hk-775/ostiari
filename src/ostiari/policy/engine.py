"""PolicyEngine — orchestrates policy loading, evaluation, reload, and merge."""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ostiari.exceptions import PolicyValidationError
from ostiari.models import (
    EvalContext,
    PolicyResult,
    PolicySet,
    Rule,
    ThresholdConfig,
    ThresholdOverrides,
)
from ostiari.policy.parser import parse_yaml
from ostiari.policy.rules import (
    compute_risk_adjustments,
    match_rules,
    resolve_thresholds,
)
from ostiari.policy.validator import validate_policy, validate_policy_collected

logger = logging.getLogger("ostiari")

_MAX_FILES = 10


@dataclass
class ReloadDiff:
    added: list[Rule] = field(default_factory=list)
    removed: list[Rule] = field(default_factory=list)
    modified: list[tuple[Rule, Rule]] = field(default_factory=list)

    @property
    def summary(self) -> str:
        parts = []
        if self.added:
            parts.append(f"+{len(self.added)} rules")
        if self.removed:
            parts.append(f"-{len(self.removed)} rules")
        if self.modified:
            parts.append(f"~{len(self.modified)} modified")
        return ", ".join(parts) if parts else "no changes"


@dataclass(frozen=True)
class PolicyVersion:
    """Tracks the version of the active policy for audit trail."""

    hash: str
    source: str
    loaded_at: datetime
    rule_count: int


class PolicyEngine:
    """Loads, validates, evaluates, and hot-reloads YAML policies."""

    def __init__(self, default_thresholds: ThresholdConfig | None = None) -> None:
        self._active_policy: PolicySet = PolicySet()
        self._decorator_policy: PolicySet | None = None
        self._loaded_paths: list[Path] = []
        self._reload_lock = threading.Lock()
        self._default_thresholds = default_thresholds or ThresholdConfig()
        self._reload_count = 0
        self._reload_failures = 0
        self._last_reload_time: datetime | None = None
        self._current_version: PolicyVersion = PolicyVersion(
            hash="00000000", source="none", loaded_at=datetime.now(timezone.utc), rule_count=0
        )

    def load(self, paths: list[str | Path]) -> None:
        """Load and validate policy files. Raises PolicyValidationError on failure."""
        resolved = [Path(p).expanduser().resolve() for p in paths]
        if len(resolved) > _MAX_FILES:
            raise PolicyValidationError(
                field="paths",
                message=f"Too many policy files ({len(resolved)}, max {_MAX_FILES})",
                suggestion="Reduce to 10 or fewer policy files.",
            )
        policy = self._load_from_paths(resolved)
        if self._decorator_policy:
            policy = self.merge(self._decorator_policy, policy)
        self._active_policy = policy
        self._loaded_paths = resolved

        content = b"".join(p.read_bytes() for p in resolved)
        self._current_version = PolicyVersion(
            hash=hashlib.sha256(content).hexdigest()[:8],
            source=str(resolved[0]) if len(resolved) == 1 else f"{len(resolved)} files",
            loaded_at=datetime.now(timezone.utc),
            rule_count=len(policy.rules),
        )
        logger.info(
            "Policy loaded from %d file(s): %d rules",
            len(resolved),
            len(policy.rules),
        )

    def evaluate(
        self, action: str, params: dict[str, Any], context: EvalContext | None = None
    ) -> PolicyResult:
        """Evaluate an action against the active policy."""
        if context is None:
            context = EvalContext()

        policy = self._active_policy
        matched = match_rules(action, policy.rules)

        for rule in matched:
            if rule.type == "block":
                return PolicyResult(
                    decision="block",
                    matching_rules=matched,
                    risk_adjustments=[],
                    effective_thresholds=resolve_thresholds(
                        action, policy.thresholds, self._default_thresholds
                    ),
                    blocked_by=rule,
                )

        for rule in matched:
            if rule.type == "allow":
                return PolicyResult(
                    decision="allow",
                    matching_rules=matched,
                    risk_adjustments=[],
                    effective_thresholds=resolve_thresholds(
                        action, policy.thresholds, self._default_thresholds
                    ),
                    blocked_by=None,
                )

        scoring_rules = [r for r in matched if r.type in ("risk_adjust", "context_rule")]
        adjustments = compute_risk_adjustments(scoring_rules, action, params, context)
        effective = resolve_thresholds(action, policy.thresholds, self._default_thresholds)

        return PolicyResult(
            decision="evaluate",
            matching_rules=matched,
            risk_adjustments=adjustments,
            effective_thresholds=effective,
            blocked_by=None,
        )

    def validate(self, path: str | Path) -> list[PolicyValidationError]:
        """Validate a policy file without loading it. Returns list of errors (empty = valid)."""
        resolved = Path(path).expanduser().resolve()
        try:
            parsed = parse_yaml(resolved)
        except PolicyValidationError as e:
            return [e]
        _, errors = validate_policy_collected(parsed)
        return errors

    def reload(self) -> bool:
        """Re-read policy files, validate, diff-log, and atomically swap. Returns success."""
        with self._reload_lock:
            self._reload_count += 1
            if not self._loaded_paths:
                logger.warning("Policy reload called but no paths loaded")
                self._reload_failures += 1
                return False
            try:
                new_policy = self._load_from_paths(self._loaded_paths)
                if self._decorator_policy:
                    new_policy = self.merge(self._decorator_policy, new_policy)
            except (PolicyValidationError, OSError) as e:
                logger.warning("Policy reload rejected: %s", e)
                self._reload_failures += 1
                return False

            diff = self._compute_diff(self._active_policy, new_policy)
            self._log_diff(diff)
            self._active_policy = new_policy
            self._last_reload_time = datetime.now(timezone.utc)
            return True

    def get_rules(self, action: str) -> list[Rule]:
        """Return all rules (including disabled) whose pattern matches the action."""
        from fnmatch import fnmatch

        return [r for r in self._active_policy.rules if fnmatch(action, r.action)]

    @staticmethod
    def merge(base: PolicySet, override: PolicySet) -> PolicySet:
        """Merge two PolicySets. Override replaces base at the rule level per action pattern."""
        base_allow = [r for r in base.rules if r.type == "allow"]
        base_block = [r for r in base.rules if r.type == "block"]
        base_other = [r for r in base.rules if r.type not in ("allow", "block")]

        override_allow = [r for r in override.rules if r.type == "allow"]
        override_block = [r for r in override.rules if r.type == "block"]
        override_other = [r for r in override.rules if r.type not in ("allow", "block")]

        merged_allow = override_allow if override_allow else base_allow
        merged_block = override_block if override_block else base_block

        override_patterns = {r.action for r in override_other}
        merged_other = [r for r in base_other if r.action not in override_patterns]
        merged_other.extend(override_other)

        all_rules = merged_block + merged_allow + merged_other
        type_order = {
            "block": 0,
            "allow": 1,
            "risk_adjust": 2,
            "threshold_override": 3,
            "context_rule": 4,
        }
        all_rules.sort(key=lambda r: (-r.priority, type_order.get(r.type, 99)))

        base_thresholds = base.thresholds
        override_thresholds = override.thresholds

        global_t = (
            override_thresholds.global_thresholds
            if override_thresholds.global_thresholds != ThresholdConfig()
            else base_thresholds.global_thresholds
        )
        per_tool = dict(base_thresholds.per_tool)
        per_tool.update(override_thresholds.per_tool)

        merged_thresholds = ThresholdOverrides(global_thresholds=global_t, per_tool=per_tool)

        return PolicySet(
            rules=all_rules,
            thresholds=merged_thresholds,
            source="merged",
            loaded_at=datetime.now(timezone.utc),
        )

    def register_decorator_rules(self, rules: list[Rule]) -> None:
        """Register rules from @protect decorators (lower precedence than YAML)."""
        self._decorator_policy = PolicySet(
            rules=rules,
            source="decorator",
            loaded_at=datetime.now(timezone.utc),
        )
        if self._loaded_paths:
            yaml_policy = self._load_from_paths(self._loaded_paths)
            self._active_policy = self.merge(self._decorator_policy, yaml_policy)
        else:
            self._active_policy = self._decorator_policy

    @property
    def active_rule_count(self) -> int:
        return len(self._active_policy.rules)

    @property
    def last_reload_time(self) -> datetime | None:
        return self._last_reload_time

    @property
    def reload_count(self) -> int:
        return self._reload_count

    @property
    def reload_failures(self) -> int:
        return self._reload_failures

    @property
    def current_version(self) -> PolicyVersion:
        return self._current_version

    def reload_from_content(self, content: bytes, source: str = "remote") -> bool:
        """Reload policy from raw YAML content (used by PolicyPoller for remote sources)."""
        with self._reload_lock:
            self._reload_count += 1
            try:
                import yaml

                from ostiari.policy.parser import ParsedYAML
                from ostiari.policy.validator import validate_policy

                data = yaml.safe_load(content)
                if data is None:
                    data = {}
                if not isinstance(data, dict):
                    raise ValueError("Policy content must be a YAML mapping")
                parsed = ParsedYAML(
                    allow=data.get("allow", []) or [],
                    block=data.get("block", []) or [],
                    rules=data.get("rules", []) or [],
                    thresholds=data.get("thresholds", {}) or {},
                )
                new_policy = validate_policy(parsed)
                if self._decorator_policy:
                    new_policy = self.merge(self._decorator_policy, new_policy)
            except Exception as e:
                logger.warning("Policy reload from content rejected: %s", e)
                self._reload_failures += 1
                return False

            diff = self._compute_diff(self._active_policy, new_policy)
            self._log_diff(diff)
            self._active_policy = new_policy
            self._last_reload_time = datetime.now(timezone.utc)
            self._current_version = PolicyVersion(
                hash=hashlib.sha256(content).hexdigest()[:8],
                source=source,
                loaded_at=self._last_reload_time,
                rule_count=len(new_policy.rules),
            )
            return True

    def _load_from_paths(self, paths: list[Path]) -> PolicySet:
        if not paths:
            return PolicySet()

        parsed = parse_yaml(paths[0])
        result = validate_policy(parsed)

        for path in paths[1:]:
            parsed = parse_yaml(path)
            override = validate_policy(parsed)
            result = self.merge(result, override)

        return result

    def _compute_diff(self, old: PolicySet, new: PolicySet) -> ReloadDiff:
        old_keys = {(r.action, r.type): r for r in old.rules}
        new_keys = {(r.action, r.type): r for r in new.rules}

        added = [new_keys[k] for k in new_keys if k not in old_keys]
        removed = [old_keys[k] for k in old_keys if k not in new_keys]
        modified = [
            (old_keys[k], new_keys[k])
            for k in old_keys
            if k in new_keys and old_keys[k] != new_keys[k]
        ]
        return ReloadDiff(added=added, removed=removed, modified=modified)

    def _log_diff(self, diff: ReloadDiff) -> None:
        if diff.summary == "no changes":
            logger.info("Policy reloaded: no changes")
            return

        logger.info("Policy reloaded: %s", diff.summary)
        for rule in diff.added:
            logger.debug("  + %s [%s] action='%s'", rule.type, rule.priority, rule.action)
        for rule in diff.removed:
            logger.debug("  - %s [%s] action='%s'", rule.type, rule.priority, rule.action)
        for old_rule, _new_rule in diff.modified:
            logger.debug("  ~ %s action='%s' (modified)", old_rule.type, old_rule.action)
