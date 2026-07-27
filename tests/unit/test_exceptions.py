"""Unit tests for ostiari.exceptions."""

from ostiari.exceptions import (
    ActionBlockedError,
    ActionInterventionTimeout,
    AdapterError,
    AdapterNotInstalledError,
    AgentTerminatedError,
    BreakerTrippedError,
    CheckpointNotFoundError,
    ConfigError,
    OstiariError,
    PolicyLoadError,
    PolicyValidationError,
    StorageError,
    StorageMigrationError,
)


class TestHierarchy:
    def test_all_inherit_from_base(self):
        exceptions = [
            ActionBlockedError("a", {}, 50, None, "reason"),
            ActionInterventionTimeout("a", 50, 30.0),
            BreakerTrippedError("b", "metric", 100.0, 50.0, "auto_retry"),
            AgentTerminatedError("b", "reason"),
            PolicyValidationError("rules[0].type", "invalid type"),
            PolicyLoadError("f.yaml", "not found"),
            ConfigError("field", "reason"),
            StorageError("op", "reason"),
            StorageMigrationError(0, 1, "reason"),
            AdapterNotInstalledError("claude", "pip install ostiari[claude]"),
            AdapterError("claude", "timeout"),
            CheckpointNotFoundError("abc123"),
        ]
        for exc in exceptions:
            assert isinstance(exc, OstiariError)
            assert isinstance(exc, Exception)


class TestActionBlockedError:
    def test_message_format(self):
        err = ActionBlockedError(
            action="delete_file",
            params={"path": "/etc"},
            score=95,
            rule_id="R1",
            reason="Dangerous",
        )
        assert "delete_file" in str(err)
        assert "95" in str(err)
        assert "Dangerous" in str(err)

    def test_fields_stored(self):
        err = ActionBlockedError("a", {"k": "v"}, 80, "rule1", "blocked")
        assert err.action == "a"
        assert err.params == {"k": "v"}
        assert err.score == 80
        assert err.rule_id == "rule1"
        assert err.reason == "blocked"


class TestActionInterventionTimeout:
    def test_message_format(self):
        err = ActionInterventionTimeout("send_email", 60, 30.0)
        assert "send_email" in str(err)
        assert "60" in str(err)
        assert "30.0" in str(err)


class TestBreakerTrippedError:
    def test_message_format(self):
        err = BreakerTrippedError("cost_breaker", "token_cost", 1500.0, 1000.0, "notify")
        msg = str(err)
        assert "cost_breaker" in msg
        assert "token_cost" in msg
        assert "1500.0" in msg
        assert "1000.0" in msg
        assert "notify" in msg


class TestAgentTerminatedError:
    def test_message_format(self):
        err = AgentTerminatedError("error_breaker", "Too many failures")
        assert "error_breaker" in str(err)
        assert "Too many failures" in str(err)
        assert "Restart required" in str(err)


class TestPolicyValidationError:
    def test_message_format(self):
        err = PolicyValidationError(
            field="rules[0].type",
            message="invalid type 'foo'",
            suggestion="Must be one of: allow, block, risk_adjust, threshold_override, context_rule",
            line=5,
        )
        msg = str(err)
        assert "rules[0].type" in msg
        assert "line 5" in msg
        assert "invalid type 'foo'" in msg

    def test_fields(self):
        err = PolicyValidationError(
            field="thresholds.global",
            message="ordering violated",
            line=10,
        )
        assert err.field == "thresholds.global"
        assert err.line == 10
        assert err.message == "ordering violated"

    def test_additional_errors(self):
        extra = PolicyValidationError(field="block[0]", message="empty pattern")
        err = PolicyValidationError(
            field="allow[0]",
            message="conflict",
            additional_errors=[extra],
        )
        assert "+1 more" in str(err)
        assert len(err.additional_errors) == 1


class TestConfigError:
    def test_with_suggestion(self):
        err = ConfigError(
            "log_level", "'TRACE' is not valid", "Must be one of: DEBUG, INFO, WARNING, ERROR"
        )
        msg = str(err)
        assert "log_level" in msg
        assert "TRACE" in msg
        assert "Must be one of" in msg

    def test_without_suggestion(self):
        err = ConfigError("field", "bad value")
        assert "bad value" in str(err)
        assert err.suggestion == ""


class TestStorageError:
    def test_message_format(self):
        err = StorageError("save_trace", "disk full")
        assert "save_trace" in str(err)
        assert "disk full" in str(err)


class TestStorageMigrationError:
    def test_message_format(self):
        err = StorageMigrationError(0, 1, "table already exists")
        assert "v0" in str(err)
        assert "v1" in str(err)
        assert "table already exists" in str(err)


class TestAdapterNotInstalledError:
    def test_message_format(self):
        err = AdapterNotInstalledError("claude", "pip install ostiari[claude]")
        msg = str(err)
        assert "claude" in msg
        assert "pip install ostiari[claude]" in msg


class TestCheckpointNotFoundError:
    def test_message_format(self):
        err = CheckpointNotFoundError("abc123")
        msg = str(err)
        assert "abc123" in msg
        assert "guard.checkpoints.list()" in msg
