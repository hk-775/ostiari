"""Strict payload parsing for the AgentCore invocation boundary."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from src.gateway.guardrail_engine import (
    ALLOWED_GUARDRAIL_ACTIONS,
    ALLOWED_GUARDRAIL_RULE_TYPES,
    ALLOWED_GUARDRAIL_TARGETS,
    compile_guardrail_regex,
)
from src.gateway.request_validator import RequestValidator

from .errors import AgentCoreAdapterError


class InvocationAction(Enum):
    CHAT = "chat"
    EMBEDDINGS = "embeddings"
    LIST_MODELS = "list_models"
    QUERY = "query"
    HEALTH = "health"
    READINESS = "readiness"
    GET_TENANT_CONFIG = "get_tenant_config"
    UPDATE_TENANT_CONFIG = "update_tenant_config"


CHAT_FIELDS = frozenset(
    {
        "model",
        "provider",
        "messages",
        "system",
        "temperature",
        "max_tokens",
        "top_p",
        "stop",
        "stream",
        "tools",
        "tool_choice",
    }
)
EMBEDDING_FIELDS = frozenset(
    {
        "model",
        "provider",
        "input",
        "encoding_format",
        "dimensions",
        "user",
    }
)
SUPPORTED_CHAT_PROVIDERS = frozenset(
    {
        "ai21",
        "anthropic",
        "azure_openai",
        "bedrock",
        "bedrock-mantle",
        "cohere",
        "fireworks",
        "google_ai",
        "groq",
        "openai",
        "together",
        "vertex_ai",
        "xai",
    }
)
QUERY_FIELDS = frozenset(
    {
        "datasource_id",
        "sql",
        "max_rows",
        "request_id",
    }
)
REHEARSAL_FIELD = "rehearsal"
REHEARSAL_SCHEMA = "axonllm.agentcore-launch-runtime-binding/v1"
REHEARSAL_OPERATIONS = frozenset(
    {
        "induce-initialization-timeout",
        "observe-runtime-replacement",
        "verify-replacement-ready",
        "reject-query-boundaries",
        "interrupt-query",
        "verify-terminal-reconciliation",
        "verify-deferred-accounting",
        "deliver-security-events",
        "verify-outbox-drained",
        "force-dead-letter",
        "verify-redelivery",
        "exercise-routing-strategies",
        "verify-routing-decisions",
        "inject-primary-provider-fault",
        "verify-provider-fallback",
        "verify-primary-provider-recovery",
        "verify-control-plane-fail-closed",
        "verify-control-plane-recovery",
    }
)
REHEARSAL_ROUTING_STRATEGIES = frozenset(
    {
        "cost-optimized",
        "ensemble",
        "least-latency",
        "round-robin",
        "smart",
        "weighted",
    }
)
REHEARSAL_DEPENDENCIES = frozenset(
    {
        "athena",
        "dynamodb",
        "secrets-manager",
        "security-event-outbox",
    }
)
TENANT_CONFIG_UPDATE_FIELDS = frozenset(
    {"expected_revision", "config"}
)
PROJECT_CONFIG_FIELDS = frozenset(
    {
        "name",
        "budget_limit",
        "alert_threshold",
        "allowed_models",
        "guardrail_rules",
        "cache_enabled",
        "cache_ttl_seconds",
        "semantic_cache_enabled",
        "semantic_cache_threshold",
        "log_level",
        "log_destination",
        "prompt_caching_enabled",
        "ltm_enabled",
        "retention_period_hours",
        "rate_limit_rpm",
    }
)
AUTHORITY_FIELDS = frozenset(
    {
        "user_id",
        "project_id",
        "tenant",
        "tenant_id",
        "roles",
        "scopes",
    }
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_SQL_BYTES = 64 * 1024
_MAX_QUERY_ROWS = 10_000
_QUERY_RESPONSE_FIELDS = frozenset(
    {
        "request_id",
        "datasource_id",
        "project_id",
        "query_execution_id",
        "columns",
        "rows",
        "row_count",
        "truncated",
        "statistics",
    }
)
_QUERY_STATISTICS_FIELDS = frozenset(
    {
        "data_scanned_bytes",
        "engine_execution_ms",
        "result_bytes",
    }
)


class QueryResponseValidationError(ValueError):
    """The query service returned a response outside its public contract."""


@dataclass(frozen=True)
class RehearsalInvocation:
    """Untrusted correlation data that can address only an active fenced ledger."""

    correlation_id: str
    owner_id: str
    release_commit: str
    fence_token: int
    expires_at_epoch: int
    operation: str
    routing_strategy: str | None = None
    dependency: str | None = None

    @classmethod
    def from_payload(cls, value: Any) -> RehearsalInvocation:
        required = {
            "schema",
            "correlation_id",
            "owner_id",
            "release_commit",
            "fence_token",
            "expires_at_epoch",
            "operation",
        }
        optional = {"routing_strategy", "dependency"}
        if (
            type(value) is not dict
            or not required.issubset(value)
            or not set(value).issubset(required | optional)
            or value.get("schema") != REHEARSAL_SCHEMA
        ):
            raise _invalid_payload("Field 'rehearsal' is malformed.")
        correlation_id = value["correlation_id"]
        owner_id = value["owner_id"]
        release_commit = value["release_commit"]
        fence_token = value["fence_token"]
        expires_at_epoch = value["expires_at_epoch"]
        operation = value["operation"]
        routing_strategy = value.get("routing_strategy")
        dependency = value.get("dependency")
        if (
            not isinstance(correlation_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", correlation_id) is None
            or not isinstance(owner_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", owner_id) is None
            or not isinstance(release_commit, str)
            or re.fullmatch(r"[0-9a-f]{40}", release_commit) is None
            or isinstance(fence_token, bool)
            or not isinstance(fence_token, int)
            or not 1 <= fence_token <= (1 << 63) - 1
            or isinstance(expires_at_epoch, bool)
            or not isinstance(expires_at_epoch, int)
            or not 1 <= expires_at_epoch <= 32_503_680_000
            or operation not in REHEARSAL_OPERATIONS
            or (
                routing_strategy is not None
                and routing_strategy not in REHEARSAL_ROUTING_STRATEGIES
            )
            or (
                dependency is not None
                and dependency not in REHEARSAL_DEPENDENCIES
            )
        ):
            raise _invalid_payload("Field 'rehearsal' is malformed.")
        return cls(
            correlation_id=correlation_id,
            owner_id=owner_id,
            release_commit=release_commit,
            fence_token=fence_token,
            expires_at_epoch=expires_at_epoch,
            operation=operation,
            routing_strategy=routing_strategy,
            dependency=dependency,
        )


def _required_string(
    value: Any,
    name: str,
    *,
    max_length: int,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_length
        or any(ord(character) < 32 for character in value)
    ):
        raise _invalid_payload(
            f"Field '{name}' must be a non-empty string without surrounding whitespace or control characters."
        )
    return value


def _identifier(value: Any, name: str) -> str:
    normalized = _required_string(value, name, max_length=128)
    if _IDENTIFIER.fullmatch(normalized) is None:
        raise _invalid_payload(f"Field '{name}' contains unsupported identifier characters.")
    return normalized


def _optional_request_id(value: Any) -> str | None:
    if value is None:
        return None
    return _required_string(value, "request_id", max_length=128)


def _optional_max_rows(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_QUERY_ROWS:
        raise _invalid_payload(f"Field 'max_rows' must be an integer between 1 and {_MAX_QUERY_ROWS}.")
    return value


def _query_sql(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise _invalid_payload(
            "Field 'sql' must be a non-empty string without surrounding whitespace or null characters."
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _invalid_payload("Field 'sql' must contain valid Unicode text.") from exc
    if len(encoded) > _MAX_SQL_BYTES:
        raise _invalid_payload("Field 'sql' exceeds 64 KiB.")
    return value


@dataclass(frozen=True)
class QueryInvocationRequest:
    """Validated, non-authoritative fields accepted by the query action."""

    datasource_id: str
    sql: str
    max_rows: int | None = None
    request_id: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> QueryInvocationRequest:
        missing = {"datasource_id", "sql"}.difference(payload)
        if missing:
            raise _invalid_payload("Query payload is missing required fields: " + ", ".join(sorted(missing)) + ".")
        return cls(
            datasource_id=_identifier(
                payload["datasource_id"],
                "datasource_id",
            ),
            sql=_query_sql(payload["sql"]),
            max_rows=_optional_max_rows(payload.get("max_rows")),
            request_id=_optional_request_id(payload.get("request_id")),
        )


def _optional_non_negative_number(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        raise _invalid_payload(
            f"Field '{name}' must be a finite non-negative number or null."
        )
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise _invalid_payload(
            f"Field '{name}' must be a finite non-negative number or null."
        ) from exc
    if not math.isfinite(normalized) or normalized < 0:
        raise _invalid_payload(
            f"Field '{name}' must be a finite non-negative number or null."
        )
    return normalized


def _optional_positive_integer(
    value: Any,
    name: str,
    *,
    maximum: int,
) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise _invalid_payload(
            f"Field '{name}' must be an integer between 1 and {maximum}, "
            "or null."
        )
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise _invalid_payload(f"Field '{name}' must be a boolean.")
    return value


def _allowed_models(value: Any) -> list[str] | None:
    if value is None:
        return None
    if (
        type(value) is not list
        or len(value) > 256
        or any(not isinstance(item, str) for item in value)
    ):
        raise _invalid_payload(
            "Field 'allowed_models' must be an array of model names or null."
        )
    models = [
        _required_string(item, "allowed_models item", max_length=256)
        for item in value
    ]
    if len(models) != len(set(models)):
        raise _invalid_payload(
            "Field 'allowed_models' must not contain duplicates."
        )
    return models


def _guardrail_rules(value: Any) -> list[dict[str, Any]]:
    if type(value) is not list or len(value) > 100:
        raise _invalid_payload(
            "Field 'guardrail_rules' must be an array of at most 100 rules."
        )
    rules: list[dict[str, Any]] = []
    fields = {"name", "rule_type", "pattern", "action", "applies_to"}
    for index, raw in enumerate(value):
        if type(raw) is not dict or set(raw) != fields:
            raise _invalid_payload(
                f"Guardrail rule {index} fields do not match the contract."
            )
        pattern = _required_string(
            raw["pattern"],
            f"guardrail_rules[{index}].pattern",
            max_length=4096,
        )
        rule_type = _required_string(
            raw["rule_type"],
            f"guardrail_rules[{index}].rule_type",
            max_length=64,
        )
        action = _required_string(
            raw["action"],
            f"guardrail_rules[{index}].action",
            max_length=64,
        )
        applies_to = _required_string(
            raw["applies_to"],
            f"guardrail_rules[{index}].applies_to",
            max_length=64,
        )
        if rule_type not in ALLOWED_GUARDRAIL_RULE_TYPES:
            raise _invalid_payload(
                f"guardrail_rules[{index}].rule_type is unsupported."
            )
        if action not in ALLOWED_GUARDRAIL_ACTIONS:
            raise _invalid_payload(
                f"guardrail_rules[{index}].action is unsupported."
            )
        if applies_to not in ALLOWED_GUARDRAIL_TARGETS:
            raise _invalid_payload(
                f"guardrail_rules[{index}].applies_to is unsupported."
            )
        if rule_type == "regex_match":
            try:
                compile_guardrail_regex(pattern)
            except ValueError as exc:
                raise _invalid_payload(
                    f"guardrail_rules[{index}].pattern is not a valid regex."
                ) from exc
        rules.append(
            {
                "name": _required_string(
                    raw["name"],
                    f"guardrail_rules[{index}].name",
                    max_length=128,
                ),
                "rule_type": rule_type,
                "pattern": pattern,
                "action": action,
                "applies_to": applies_to,
            }
        )
    return rules


def _project_config_updates(value: Any) -> dict[str, Any]:
    if type(value) is not dict or not value:
        raise _invalid_payload(
            "Field 'config' must be a non-empty JSON object."
        )
    unexpected = sorted(set(value).difference(PROJECT_CONFIG_FIELDS))
    if unexpected:
        raise _invalid_payload(
            "Field 'config' contains unsupported fields: "
            + ", ".join(unexpected)
            + "."
        )
    result: dict[str, Any] = {}
    for name, raw in value.items():
        if name == "name":
            result[name] = _required_string(raw, name, max_length=256)
        elif name in {"budget_limit", "alert_threshold"}:
            result[name] = _optional_non_negative_number(raw, name)
        elif name == "allowed_models":
            result[name] = _allowed_models(raw)
        elif name == "guardrail_rules":
            result[name] = _guardrail_rules(raw)
        elif name in {
            "cache_enabled",
            "semantic_cache_enabled",
            "prompt_caching_enabled",
            "ltm_enabled",
        }:
            result[name] = _boolean(raw, name)
        elif name == "cache_ttl_seconds":
            result[name] = _optional_positive_integer(
                raw,
                name,
                maximum=86_400,
            )
            if result[name] is None:
                raise _invalid_payload(
                    "Field 'cache_ttl_seconds' cannot be null."
                )
        elif name == "semantic_cache_threshold":
            if raw is None:
                result[name] = None
            else:
                try:
                    threshold = float(raw)
                except (OverflowError, TypeError, ValueError) as exc:
                    raise _invalid_payload(
                        "Field 'semantic_cache_threshold' must be in "
                        "(0.0, 1.0], or null."
                    ) from exc
                if (
                    isinstance(raw, bool)
                    or not isinstance(raw, (int, float))
                    or not math.isfinite(threshold)
                    or not 0 < threshold <= 1
                ):
                    raise _invalid_payload(
                        "Field 'semantic_cache_threshold' must be in "
                        "(0.0, 1.0], or null."
                    )
                result[name] = threshold
        elif name == "log_level":
            level = _required_string(raw, name, max_length=16).upper()
            if level not in {
                "CRITICAL",
                "ERROR",
                "WARNING",
                "INFO",
                "DEBUG",
            }:
                raise _invalid_payload(
                    "Field 'log_level' is not a supported level."
                )
            result[name] = level
        elif name == "log_destination":
            result[name] = (
                None
                if raw is None
                else _required_string(raw, name, max_length=2048)
            )
        elif name == "retention_period_hours":
            result[name] = _optional_positive_integer(
                raw,
                name,
                maximum=87_600,
            )
            if result[name] is None:
                raise _invalid_payload(
                    "Field 'retention_period_hours' cannot be null."
                )
        elif name == "rate_limit_rpm":
            result[name] = _optional_positive_integer(
                raw,
                name,
                maximum=1_000_000_000,
            )
    return result


@dataclass(frozen=True)
class TenantConfigUpdateRequest:
    """Validated CAS update for canonical tenant project configuration."""

    expected_revision: int
    updates: dict[str, Any]

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
    ) -> TenantConfigUpdateRequest:
        if "expected_revision" not in payload or "config" not in payload:
            raise _invalid_payload(
                "Tenant configuration update requires "
                "'expected_revision' and 'config'."
            )
        revision = payload["expected_revision"]
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
        ):
            raise _invalid_payload(
                "Field 'expected_revision' must be a non-negative integer."
            )
        return cls(
            expected_revision=revision,
            updates=_project_config_updates(payload["config"]),
        )


@dataclass(frozen=True)
class QueryColumn:
    """One validated query result column."""

    name: str
    athena_type: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "type": self.athena_type}


@dataclass(frozen=True)
class QueryStatistics:
    """Non-negative Athena execution statistics."""

    data_scanned_bytes: int
    engine_execution_ms: int
    result_bytes: int

    def to_dict(self) -> dict[str, int]:
        return {
            "data_scanned_bytes": self.data_scanned_bytes,
            "engine_execution_ms": self.engine_execution_ms,
            "result_bytes": self.result_bytes,
        }


def _response_object(
    value: Any,
    name: str,
    fields: frozenset[str],
) -> dict[str, Any]:
    if type(value) is not dict:
        raise QueryResponseValidationError(f"{name} must be an object")
    if set(value) != fields:
        raise QueryResponseValidationError(f"{name} fields do not match the response contract")
    return value


def _response_string(
    value: Any,
    name: str,
    *,
    max_length: int,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_length
        or any(ord(character) < 32 for character in value)
    ):
        raise QueryResponseValidationError(f"{name} is invalid")
    return value


def _response_identifier(value: Any, name: str) -> str:
    normalized = _response_string(value, name, max_length=128)
    if _IDENTIFIER.fullmatch(normalized) is None:
        raise QueryResponseValidationError(f"{name} is invalid")
    return normalized


def _column_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_SQL_BYTES:
        raise QueryResponseValidationError(f"{name} is invalid")
    return value


def _non_negative_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise QueryResponseValidationError(f"{name} must be a non-negative integer")
    return value


def _query_columns(value: Any) -> tuple[QueryColumn, ...]:
    if type(value) is not list:
        raise QueryResponseValidationError("columns must be an array")
    columns: list[QueryColumn] = []
    for raw_column in value:
        column = _response_object(
            raw_column,
            "column",
            frozenset({"name", "type"}),
        )
        columns.append(
            QueryColumn(
                name=_column_text(
                    column["name"],
                    "column name",
                ),
                athena_type=_column_text(
                    column["type"],
                    "column type",
                ),
            )
        )
    return tuple(columns)


def _query_rows(
    value: Any,
    *,
    column_count: int,
) -> tuple[tuple[str | None, ...], ...]:
    if type(value) is not list or len(value) > _MAX_QUERY_ROWS:
        raise QueryResponseValidationError("rows must be a bounded array")
    rows: list[tuple[str | None, ...]] = []
    for raw_row in value:
        if type(raw_row) is not list or len(raw_row) != column_count:
            raise QueryResponseValidationError("query result row width does not match columns")
        if any(item is not None and not isinstance(item, str) for item in raw_row):
            raise QueryResponseValidationError("query result values must be strings or null")
        rows.append(tuple(raw_row))
    return tuple(rows)


@dataclass(frozen=True)
class QueryInvocationResponse:
    """Validated AgentCore representation of a query service result."""

    request_id: str
    datasource_id: str
    project_id: str
    query_execution_id: str
    columns: tuple[QueryColumn, ...]
    rows: tuple[tuple[str | None, ...], ...]
    row_count: int
    truncated: bool
    statistics: QueryStatistics

    @classmethod
    def from_mapping(
        cls,
        raw: Any,
        *,
        expected_datasource_id: str,
        expected_project_id: str,
        expected_request_id: str | None = None,
    ) -> QueryInvocationResponse:
        value = _response_object(
            raw,
            "query response",
            _QUERY_RESPONSE_FIELDS,
        )
        request_id = _response_string(
            value["request_id"],
            "request_id",
            max_length=128,
        )
        datasource_id = _response_identifier(
            value["datasource_id"],
            "datasource_id",
        )
        project_id = _response_identifier(
            value["project_id"],
            "project_id",
        )
        if (
            datasource_id != expected_datasource_id
            or project_id != expected_project_id
            or (expected_request_id is not None and request_id != expected_request_id)
        ):
            raise QueryResponseValidationError("query response identity does not match the request")

        columns = _query_columns(value["columns"])
        rows = _query_rows(
            value["rows"],
            column_count=len(columns),
        )
        row_count = _non_negative_integer(
            value["row_count"],
            "row_count",
        )
        if row_count != len(rows):
            raise QueryResponseValidationError("row_count does not match query result rows")
        if not isinstance(value["truncated"], bool):
            raise QueryResponseValidationError("truncated must be a boolean")

        raw_statistics = _response_object(
            value["statistics"],
            "statistics",
            _QUERY_STATISTICS_FIELDS,
        )
        statistics = QueryStatistics(
            data_scanned_bytes=_non_negative_integer(
                raw_statistics["data_scanned_bytes"],
                "data_scanned_bytes",
            ),
            engine_execution_ms=_non_negative_integer(
                raw_statistics["engine_execution_ms"],
                "engine_execution_ms",
            ),
            result_bytes=_non_negative_integer(
                raw_statistics["result_bytes"],
                "result_bytes",
            ),
        )
        return cls(
            request_id=request_id,
            datasource_id=datasource_id,
            project_id=project_id,
            query_execution_id=_response_string(
                value["query_execution_id"],
                "query_execution_id",
                max_length=256,
            ),
            columns=columns,
            rows=rows,
            row_count=row_count,
            truncated=value["truncated"],
            statistics=statistics,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "datasource_id": self.datasource_id,
            "project_id": self.project_id,
            "query_execution_id": self.query_execution_id,
            "columns": [column.to_dict() for column in self.columns],
            "rows": [list(row) for row in self.rows],
            "row_count": self.row_count,
            "truncated": self.truncated,
            "statistics": self.statistics.to_dict(),
        }


@dataclass(frozen=True)
class ParsedInvocation:
    action: InvocationAction
    request_data: dict[str, Any] | None = None
    query_request: QueryInvocationRequest | None = None
    tenant_config_update: TenantConfigUpdateRequest | None = None
    preferred_provider: str | None = None
    rehearsal: RehearsalInvocation | None = None


def _invalid_payload(message: str) -> AgentCoreAdapterError:
    return AgentCoreAdapterError(400, "invalid_payload", message)


def _validate_provider_controls(
    provider: str | None,
    request_data: dict[str, Any],
) -> None:
    """Reject provider controls that cannot be honored deterministically."""
    if provider != "cohere" or not request_data.get("tools"):
        return
    tool_choice = request_data.get("tool_choice")
    if tool_choice == "required" or isinstance(tool_choice, dict):
        raise AgentCoreAdapterError(
            400,
            "unsupported_provider_feature",
            (
                "Cohere v1 does not support required or named tool "
                "selection. Use automatic tool selection instead."
            ),
        )


def parse_invocation_payload(
    payload: Any,
    *,
    validator: RequestValidator | None = None,
) -> ParsedInvocation:
    """Validate an action-specific JSON object without coercing field types."""
    if type(payload) is not dict:
        raise _invalid_payload("Invocation payload must be a JSON object.")
    if any(not isinstance(key, str) for key in payload):
        raise _invalid_payload("Invocation payload keys must be strings.")

    supplied_authority = sorted(AUTHORITY_FIELDS.intersection(payload))
    if supplied_authority:
        raise AgentCoreAdapterError(
            400,
            "untrusted_identity_fields",
            "Identity and authorization fields are not accepted in payloads.",
        )

    raw_action = payload.get("action", InvocationAction.CHAT.value)
    if not isinstance(raw_action, str):
        raise _invalid_payload("Field 'action' must be a string.")
    try:
        action = InvocationAction(raw_action)
    except ValueError as exc:
        raise _invalid_payload("Field 'action' is not supported.") from exc

    rehearsal = (
        RehearsalInvocation.from_payload(payload[REHEARSAL_FIELD])
        if REHEARSAL_FIELD in payload
        else None
    )
    allowed_fields = {"action", REHEARSAL_FIELD}
    if action is InvocationAction.CHAT:
        allowed_fields.update(CHAT_FIELDS)
    elif action is InvocationAction.EMBEDDINGS:
        allowed_fields.update(EMBEDDING_FIELDS)
    elif action is InvocationAction.QUERY:
        allowed_fields.update(QUERY_FIELDS)
    elif action is InvocationAction.UPDATE_TENANT_CONFIG:
        allowed_fields.update(TENANT_CONFIG_UPDATE_FIELDS)
    unexpected = sorted(set(payload).difference(allowed_fields))
    if unexpected:
        raise _invalid_payload("Invocation payload contains unsupported fields: " + ", ".join(unexpected) + ".")

    if action is InvocationAction.QUERY:
        return ParsedInvocation(
            action=action,
            query_request=QueryInvocationRequest.from_payload(payload),
            rehearsal=rehearsal,
        )
    if action is InvocationAction.UPDATE_TENANT_CONFIG:
        return ParsedInvocation(
            action=action,
            tenant_config_update=TenantConfigUpdateRequest.from_payload(
                payload
            ),
            rehearsal=rehearsal,
        )
    if action is InvocationAction.EMBEDDINGS:
        preferred_provider = payload.get("provider")
        if preferred_provider is not None:
            preferred_provider = _identifier(
                preferred_provider,
                "provider",
            )
            if preferred_provider not in SUPPORTED_CHAT_PROVIDERS:
                raise _invalid_payload(
                    "Field 'provider' is not a supported provider."
                )

        model = _required_string(
            payload.get("model"),
            "model",
            max_length=256,
        )
        input_value = payload.get("input")
        if isinstance(input_value, str):
            if not input_value:
                raise _invalid_payload(
                    "Field 'input' must not be empty."
                )
        elif isinstance(input_value, list):
            if not input_value or not all(
                isinstance(item, str) and item
                for item in input_value
            ):
                raise _invalid_payload(
                    "Field 'input' must be a non-empty list of non-empty strings."
                )
        else:
            raise _invalid_payload(
                "Field 'input' must be a string or a list of strings."
            )

        encoding_format = payload.get("encoding_format", "float")
        if encoding_format not in {"float", "base64"}:
            raise _invalid_payload(
                "Field 'encoding_format' must be 'float' or 'base64'."
            )
        dimensions = payload.get("dimensions")
        if dimensions is not None and (
            isinstance(dimensions, bool)
            or not isinstance(dimensions, int)
            or not 1 <= dimensions <= 65_536
        ):
            raise _invalid_payload(
                "Field 'dimensions' must be an integer between 1 and 65536."
            )
        user = payload.get("user")
        if user is not None:
            user = _required_string(user, "user", max_length=256)

        request_data: dict[str, Any] = {
            "model": model,
            "input": input_value,
            "encoding_format": encoding_format,
        }
        if dimensions is not None:
            request_data["dimensions"] = dimensions
        if user is not None:
            request_data["user"] = user
        return ParsedInvocation(
            action=action,
            request_data=request_data,
            preferred_provider=preferred_provider,
            rehearsal=rehearsal,
        )
    if action is not InvocationAction.CHAT:
        return ParsedInvocation(action=action, rehearsal=rehearsal)

    preferred_provider = payload.get("provider")
    if preferred_provider is not None:
        preferred_provider = _identifier(
            preferred_provider,
            "provider",
        )
        if preferred_provider not in SUPPORTED_CHAT_PROVIDERS:
            raise _invalid_payload(
                "Field 'provider' is not a supported provider."
            )
    request_data = {
        field_name: payload[field_name]
        for field_name in CHAT_FIELDS
        if field_name in payload and field_name != "provider"
    }
    request_data.setdefault("stream", False)
    errors = (validator or RequestValidator()).validate_payload(
        request_data,
        allow_empty_model=False,
        check_model=False,
    )
    if errors:
        raise _invalid_payload(errors[0].message)
    _validate_provider_controls(preferred_provider, request_data)
    return ParsedInvocation(
        action=action,
        request_data=request_data,
        preferred_provider=preferred_provider,
        rehearsal=rehearsal,
    )
