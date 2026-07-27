"""Ostiari exception hierarchy."""

from __future__ import annotations


class OstiariError(Exception):
    """Base exception for all Ostiari errors."""


class ActionBlockedError(OstiariError):
    """Raised when an action is blocked by the evaluation pipeline.

    ``original_tier`` is the tier the action actually *scored*, before
    fail-closed handling. A blocked action normally scored "block", but an
    ``intervene`` with no resolver collapses to a block when ``fail_open`` is
    off — and the two are not the same decision. "This is forbidden" and "a
    human needs to look at this, and no human was reachable" call for different
    responses from a caller that *can* reach a human (the gateway's HITL gate
    pauses for approval instead of refusing outright), so the distinction has to
    survive the raise. ``signals`` rides along for the same reason: an
    explanation of *why* is what the approver reads.
    """

    def __init__(
        self,
        action: str,
        params: dict[str, object],
        score: int,
        rule_id: str | None,
        reason: str,
        original_tier: str = "block",
        signals: list[object] | None = None,
    ) -> None:
        self.action = action
        self.params = params
        self.score = score
        self.rule_id = rule_id
        self.reason = reason
        self.original_tier = original_tier
        self.signals = signals or []
        super().__init__(f"Action '{action}' blocked (score: {score}). Reason: {reason}")


class ActionInterventionTimeout(OstiariError):
    """Raised when a human intervention request times out."""

    def __init__(self, action: str, score: int, timeout_seconds: float) -> None:
        self.action = action
        self.score = score
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Intervention timeout for '{action}' (score: {score}) after {timeout_seconds}s"
        )


class BreakerTrippedError(OstiariError):
    """Raised when a circuit breaker is in open state."""

    def __init__(
        self,
        breaker_id: str,
        metric: str,
        current_value: float,
        threshold: float,
        recovery_mode: str,
    ) -> None:
        self.breaker_id = breaker_id
        self.metric = metric
        self.current_value = current_value
        self.threshold = threshold
        self.recovery_mode = recovery_mode
        super().__init__(
            f"Circuit breaker '{breaker_id}' is open. "
            f"{metric} ({current_value}) exceeded threshold ({threshold}). "
            f"Recovery mode: {recovery_mode}"
        )


class AgentTerminatedError(OstiariError):
    """Raised when a circuit breaker terminates the agent (unrecoverable)."""

    def __init__(self, breaker_id: str, reason: str) -> None:
        self.breaker_id = breaker_id
        self.reason = reason
        super().__init__(
            f"Agent terminated by circuit breaker '{breaker_id}'. "
            f"Reason: {reason}. Restart required."
        )


class PolicyValidationError(OstiariError):
    """Raised when a policy file fails validation."""

    def __init__(
        self,
        field: str,
        message: str,
        suggestion: str = "",
        line: int | None = None,
        additional_errors: list[PolicyValidationError] | None = None,
    ) -> None:
        self.field = field
        self.line = line
        self.message = message
        self.suggestion = suggestion
        self.additional_errors = additional_errors or []
        loc = f" (line {line})" if line else ""
        msg = f"Policy validation error at '{field}'{loc}: {message}"
        if suggestion:
            msg += f" {suggestion}"
        if self.additional_errors:
            msg += f" (+{len(self.additional_errors)} more errors)"
        super().__init__(msg)


class PolicyLoadError(OstiariError):
    """Raised when a policy file cannot be loaded."""

    def __init__(self, file_path: str, reason: str) -> None:
        self.file_path = file_path
        self.reason = reason
        super().__init__(f"Cannot load policy '{file_path}': {reason}")


class ConfigError(OstiariError):
    """Raised when configuration is invalid."""

    def __init__(self, field: str, reason: str, suggestion: str = "") -> None:
        self.field = field
        self.reason = reason
        self.suggestion = suggestion
        msg = f"Invalid config '{field}': {reason}."
        if suggestion:
            msg += f" {suggestion}"
        super().__init__(msg)


class StorageError(OstiariError):
    """Raised when a storage operation fails (only if fail_open=False)."""

    def __init__(self, operation: str, reason: str) -> None:
        self.operation = operation
        self.reason = reason
        super().__init__(f"Storage operation '{operation}' failed: {reason}")


class StorageMigrationError(OstiariError):
    """Raised when a schema migration fails."""

    def __init__(self, from_version: int, to_version: int, reason: str) -> None:
        self.from_version = from_version
        self.to_version = to_version
        self.reason = reason
        super().__init__(f"Migration from v{from_version} to v{to_version} failed: {reason}")


class AdapterNotInstalledError(OstiariError):
    """Raised when an adapter's optional dependency is not installed."""

    def __init__(self, adapter: str, install_command: str) -> None:
        self.adapter = adapter
        self.install_command = install_command
        super().__init__(
            f"Adapter '{adapter}' requires additional dependencies. Install with: {install_command}"
        )


class AdapterError(OstiariError):
    """Raised when an adapter encounters a runtime error."""

    def __init__(self, adapter: str, reason: str) -> None:
        self.adapter = adapter
        self.reason = reason
        super().__init__(f"Adapter '{adapter}' error: {reason}")


class AdapterValidationError(OstiariError):
    """Raised when an adapter does not implement all required protocol methods."""

    def __init__(self, adapter: str, missing: list[str]) -> None:
        self.adapter = adapter
        self.missing = missing
        super().__init__(
            f"Adapter '{adapter}' is missing required methods: {', '.join(missing)}. "
            f"See ostiari.adapters.FrameworkAdapter for the protocol definition."
        )


class CheckpointNotFoundError(OstiariError):
    """Raised when a checkpoint cannot be found for rollback."""

    def __init__(self, checkpoint_id: str) -> None:
        self.checkpoint_id = checkpoint_id
        super().__init__(
            f"Checkpoint '{checkpoint_id}' not found. "
            f"Use guard.checkpoints.list() to see available checkpoints."
        )
