"""Disabled-by-default durable controls for isolated launch rehearsals.

This module is deliberately not wired into request handling. A trusted
rehearsal worker may claim a correlation-scoped ledger and install one of the
fixed controls below. Future production hooks can then read a control or append
an observation without allowing a ledger failure to affect normal behavior.
Evidence collection uses the separate fail-closed API.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Protocol


TABLE_ENV = "AXON_LAUNCH_REHEARSAL_TABLE"
LEDGER_KEY_ATTRIBUTE = "ledger_key"
TTL_ATTRIBUTE = "expires_at_epoch"

SCHEMA_VERSION = 1
MAX_LEDGER_TTL_SECONDS = 48 * 60 * 60
MAX_LEDGER_BYTES = 64 * 1024
MAX_OBSERVATION_BYTES = 1024
MAX_OBSERVATIONS = 64
MAX_CAS_ATTEMPTS = 2
MAX_FENCE_TOKEN = (1 << 63) - 1

FAULTS = frozenset(
    {
        "dependency-unavailable",
        "provider-unavailable",
        "startup-delay",
    }
)
CHECKPOINTS = frozenset(
    {
        "query-after-reservation",
        "startup-before-ready",
    }
)
OBSERVATIONS = frozenset(
    {
        "dependency-call",
        "provider-attempt",
        "query-lifecycle",
        "routing-decision",
        "startup-attempt",
    }
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CORRELATION_ID = re.compile(r"^[0-9a-f]{32}$")
_OWNER_ID = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SAFE_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_DEPENDENCIES = frozenset(
    {
        "athena",
        "dynamodb",
        "secrets-manager",
        "security-event-outbox",
    }
)
_PROVIDERS = frozenset(
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
_ROUTING_STRATEGIES = frozenset(
    {
        "cost-optimized",
        "ensemble",
        "least-latency",
        "round-robin",
        "smart",
        "weighted",
    }
)
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_CONTROL_PARAMETER_FIELDS = {
    ("checkpoint", "query-after-reservation"): frozenset({"hold_seconds"}),
    ("checkpoint", "startup-before-ready"): frozenset({"hold_seconds"}),
    ("fault", "dependency-unavailable"): frozenset({"dependency"}),
    ("fault", "provider-unavailable"): frozenset({"provider", "status_code"}),
    ("fault", "startup-delay"): frozenset({"delay_seconds"}),
}
_OBSERVATION_SCHEMAS = {
    "dependency-call": (
        frozenset({"dependency", "outcome", "request_id"}),
        frozenset({"status_code"}),
    ),
    "provider-attempt": (
        frozenset({"attempt", "outcome", "provider", "request_id"}),
        frozenset({"status_code"}),
    ),
    "query-lifecycle": (
        frozenset({"phase", "request_id"}),
        frozenset({"execution_id", "reservation_units", "terminal_state"}),
    ),
    "routing-decision": (
        frozenset({"provider", "request_id", "strategy"}),
        frozenset({"candidate_count"}),
    ),
    "startup-attempt": (
        frozenset({"boot_id", "phase"}),
        frozenset({"exit_code", "runtime_id"}),
    ),
}
_ENUM_FIELDS = {
    "dependency": _DEPENDENCIES,
    "outcome": frozenset(
        {
            "available",
            "non-retryable-failure",
            "retryable-failure",
            "success",
            "unavailable",
        }
    ),
    "phase": frozenset(
        {
            "deferred",
            "interrupted",
            "ready",
            "reconciled",
            "reserved",
            "started",
            "submitted",
            "timed-out",
        }
    ),
    "provider": _PROVIDERS,
    "strategy": _ROUTING_STRATEGIES,
    "terminal_state": frozenset({"CANCELLED", "DEFERRED", "FAILED", "SUCCEEDED"}),
}
_INTEGER_FIELDS = {
    "attempt": (1, 100),
    "candidate_count": (1, 100),
    "delay_seconds": (1, 300),
    "exit_code": (0, 255),
    "hold_seconds": (1, 300),
    "reservation_units": (0, 1_000_000_000),
    "status_code": (100, 599),
}
_EVENT_ID_FIELDS = frozenset({"execution_id", "request_id", "runtime_id"})
_PHASES_BY_OBSERVATION = {
    "query-lifecycle": frozenset({"deferred", "interrupted", "reconciled", "reserved", "submitted"}),
    "startup-attempt": frozenset({"ready", "started", "timed-out"}),
}

_RECORD_FIELDS = frozenset(
    {
        LEDGER_KEY_ATTRIBUTE,
        "checkpoints",
        "correlation_id",
        "entity_type",
        TTL_ATTRIBUTE,
        "faults",
        "fence_token",
        "observations",
        "owner_id",
        "project_id",
        "release_commit",
        "revision",
        "schema_version",
        "tenant_id",
        "updated_at_epoch",
    }
)
_CONTROL_FIELDS = frozenset({"active", "parameters"})
_OBSERVATION_FIELDS = frozenset({"kind", "observed_at_epoch", "payload", "sequence"})


class _DynamoTable(Protocol):
    def get_item(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def put_item(self, **kwargs: Any) -> Mapping[str, Any] | None: ...


class RehearsalEvidenceUnavailable(RuntimeError):
    """Evidence cannot be trusted or read from the durable ledger."""


@dataclass(frozen=True)
class RehearsalBinding:
    """Authority attached to every row in one rehearsal ledger."""

    tenant_id: str
    project_id: str
    correlation_id: str
    owner_id: str
    release_commit: str
    fence_token: int
    expires_at_epoch: int

    def __post_init__(self) -> None:
        if not _canonical_identifier(self.tenant_id):
            raise ValueError("tenant_id is not canonical")
        if not _canonical_identifier(self.project_id):
            raise ValueError("project_id is not canonical")
        if _CORRELATION_ID.fullmatch(self.correlation_id) is None:
            raise ValueError("correlation_id must be 32 lowercase hex characters")
        if _OWNER_ID.fullmatch(self.owner_id) is None:
            raise ValueError("owner_id must be 64 lowercase hex characters")
        if _RELEASE_COMMIT.fullmatch(self.release_commit) is None:
            raise ValueError("release_commit must be 40 lowercase hex characters")
        if not _positive_integer(
            self.fence_token,
            maximum=MAX_FENCE_TOKEN,
        ):
            raise ValueError("fence_token must be a positive 63-bit integer")
        if not _positive_integer(
            self.expires_at_epoch,
            maximum=32_503_680_000,
        ):
            raise ValueError("expires_at_epoch must be an absolute epoch second")

    @classmethod
    def from_authenticated_request(
        cls,
        *,
        tenant_id: object,
        project_id: object,
        correlation_id: object,
        owner_id: object,
        release_commit: object,
        fence_token: object,
        expires_at_epoch: object,
    ) -> RehearsalBinding | None:
        """Build a binding after authentication, returning no hook on bad input."""

        parsed = parse_rehearsal_correlation_id(correlation_id)
        if parsed is None:
            return None
        try:
            return cls(
                tenant_id=tenant_id,  # type: ignore[arg-type]
                project_id=project_id,  # type: ignore[arg-type]
                correlation_id=parsed,
                owner_id=owner_id,  # type: ignore[arg-type]
                release_commit=release_commit,  # type: ignore[arg-type]
                fence_token=fence_token,  # type: ignore[arg-type]
                expires_at_epoch=expires_at_epoch,  # type: ignore[arg-type]
            )
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class ActiveRehearsalControl:
    """One validated active fault or checkpoint."""

    control_type: str
    name: str
    parameters: Mapping[str, Any]
    revision: int
    expires_at_epoch: int


@dataclass(frozen=True)
class RehearsalObservation:
    """One validated, correlation-scoped observation."""

    sequence: int
    kind: str
    observed_at_epoch: int
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class _Record:
    binding: RehearsalBinding
    revision: int
    updated_at_epoch: int
    faults: Mapping[str, Mapping[str, Any]]
    checkpoints: Mapping[str, Mapping[str, Any]]
    observations: tuple[RehearsalObservation, ...]


def parse_rehearsal_correlation_id(value: object) -> str | None:
    """Parse the optional opaque request correlation without raising."""

    if not isinstance(value, str) or _CORRELATION_ID.fullmatch(value) is None:
        return None
    return value


def _canonical_identifier(value: object) -> bool:
    return isinstance(value, str) and value == value.strip() and _IDENTIFIER.fullmatch(value) is not None


def _positive_integer(value: object, *, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= maximum


def _dynamo_integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    return None


def _safe_event_id(value: object) -> str | None:
    if isinstance(value, str) and _SAFE_EVENT_ID.fullmatch(value) is not None:
        return value
    return None


def _exact_mapping(
    value: object,
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    if type(value) is not dict:
        return None
    if not required.issubset(value) or not set(value).issubset(required | optional):
        return None
    return dict(value)


def _bounded_integer(
    value: object,
    *,
    minimum: int,
    maximum: int,
) -> int | None:
    normalized = _dynamo_integer(value)
    if normalized is None or not minimum <= normalized <= maximum:
        return None
    return normalized


def _ledger_key(binding: RehearsalBinding) -> str:
    material = json.dumps(
        {
            "correlation_id": binding.correlation_id,
            "project_id": binding.project_id,
            "tenant_id": binding.tenant_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return f"rehearsal#{hashlib.sha256(material).hexdigest()}"


def _binding_is_current(
    binding: RehearsalBinding,
    *,
    now_epoch: int,
) -> bool:
    remaining = binding.expires_at_epoch - now_epoch
    return 0 < remaining <= MAX_LEDGER_TTL_SECONDS


def _normalize_control_parameters(
    control_type: object,
    name: object,
    parameters: object,
) -> dict[str, Any] | None:
    if not isinstance(control_type, str) or not isinstance(name, str):
        return None
    required = _CONTROL_PARAMETER_FIELDS.get((control_type, name))
    value = _exact_mapping(parameters, required=required) if required is not None else None
    if value is None:
        return None
    normalized = _normalize_fields(value)
    if normalized is None or (
        name == "provider-unavailable" and normalized["status_code"] not in _RETRYABLE_STATUS_CODES
    ):
        return None
    return normalized


def _normalize_fields(value: Mapping[str, Any]) -> dict[str, Any] | None:
    normalized: dict[str, Any] = {}
    for field, raw in value.items():
        if field == "boot_id":
            item = parse_rehearsal_correlation_id(raw)
        elif field in _EVENT_ID_FIELDS:
            item = _safe_event_id(raw)
        elif field in _ENUM_FIELDS:
            item = raw if isinstance(raw, str) and raw in _ENUM_FIELDS[field] else None
        elif field in _INTEGER_FIELDS:
            minimum, maximum = _INTEGER_FIELDS[field]
            item = _bounded_integer(
                raw,
                minimum=minimum,
                maximum=maximum,
            )
        else:
            return None
        if item is None:
            return None
        normalized[field] = item
    return normalized


def _normalize_observation_payload(
    kind: object,
    payload: object,
) -> dict[str, Any] | None:
    if not isinstance(kind, str):
        return None
    schema = _OBSERVATION_SCHEMAS.get(kind)
    if schema is None:
        return None
    value = _exact_mapping(
        payload,
        required=schema[0],
        optional=schema[1],
    )
    normalized = _normalize_fields(value) if value is not None else None
    if normalized is None:
        return None
    phases = _PHASES_BY_OBSERVATION.get(kind)
    if phases is not None and normalized["phase"] not in phases:
        return None
    if kind == "startup-attempt" and normalized["phase"] == "timed-out" and normalized.get("exit_code") != 124:
        return None
    if kind == "dependency-call" and normalized["outcome"] not in {"available", "unavailable"}:
        return None
    if kind == "provider-attempt" and normalized["outcome"] not in {
        "non-retryable-failure",
        "retryable-failure",
        "success",
    }:
        return None
    return normalized


def _canonical_size(value: object) -> int | None:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError):
        return None
    return len(raw)


def _empty_record(
    binding: RehearsalBinding,
    *,
    now_epoch: int,
    revision: int,
) -> _Record:
    return _Record(
        binding=binding,
        revision=revision,
        updated_at_epoch=now_epoch,
        faults={},
        checkpoints={},
        observations=(),
    )


def _record_item(record: _Record) -> dict[str, Any]:
    return {
        LEDGER_KEY_ATTRIBUTE: _ledger_key(record.binding),
        "checkpoints": {
            name: {
                "active": value["active"],
                "parameters": dict(value["parameters"]),
            }
            for name, value in record.checkpoints.items()
        },
        "correlation_id": record.binding.correlation_id,
        "entity_type": "launch_rehearsal_control",
        TTL_ATTRIBUTE: record.binding.expires_at_epoch,
        "faults": {
            name: {
                "active": value["active"],
                "parameters": dict(value["parameters"]),
            }
            for name, value in record.faults.items()
        },
        "fence_token": record.binding.fence_token,
        "observations": [
            {
                "kind": observation.kind,
                "observed_at_epoch": observation.observed_at_epoch,
                "payload": dict(observation.payload),
                "sequence": observation.sequence,
            }
            for observation in record.observations
        ],
        "owner_id": record.binding.owner_id,
        "project_id": record.binding.project_id,
        "release_commit": record.binding.release_commit,
        "revision": record.revision,
        "schema_version": SCHEMA_VERSION,
        "tenant_id": record.binding.tenant_id,
        "updated_at_epoch": record.updated_at_epoch,
    }


def _decode_controls(
    value: object,
    *,
    control_type: str,
) -> dict[str, dict[str, Any]] | None:
    if type(value) is not dict:
        return None
    allowed = FAULTS if control_type == "fault" else CHECKPOINTS
    if not set(value).issubset(allowed):
        return None
    controls: dict[str, dict[str, Any]] = {}
    for name, raw_control in value.items():
        control = _exact_mapping(
            raw_control,
            required=_CONTROL_FIELDS,
        )
        if control is None or type(control["active"]) is not bool:
            return None
        parameters = _normalize_control_parameters(
            control_type,
            name,
            control["parameters"],
        )
        if parameters is None:
            return None
        controls[name] = {
            "active": control["active"],
            "parameters": parameters,
        }
    return controls


def _decode_record(
    item: object,
    *,
    now_epoch: int,
    require_active: bool,
) -> _Record | None:
    if type(item) is not dict or set(item) != _RECORD_FIELDS:
        return None
    if (
        item.get("entity_type") != "launch_rehearsal_control"
        or _dynamo_integer(item.get("schema_version")) != SCHEMA_VERSION
    ):
        return None
    fence = _dynamo_integer(item.get("fence_token"))
    expiry = _dynamo_integer(item.get(TTL_ATTRIBUTE))
    revision = _dynamo_integer(item.get("revision"))
    updated = _dynamo_integer(item.get("updated_at_epoch"))
    try:
        binding = RehearsalBinding(
            tenant_id=item.get("tenant_id"),
            project_id=item.get("project_id"),
            correlation_id=item.get("correlation_id"),
            owner_id=item.get("owner_id"),
            release_commit=item.get("release_commit"),
            fence_token=fence,
            expires_at_epoch=expiry,
        )
    except (TypeError, ValueError):
        return None
    if (
        item.get(LEDGER_KEY_ATTRIBUTE) != _ledger_key(binding)
        or not _positive_integer(revision, maximum=MAX_FENCE_TOKEN)
        or updated is None
        or updated < 0
        or updated < binding.expires_at_epoch - MAX_LEDGER_TTL_SECONDS
        or updated > binding.expires_at_epoch
        or updated > now_epoch + 300
        or (require_active and not _binding_is_current(binding, now_epoch=now_epoch))
    ):
        return None
    faults = _decode_controls(item.get("faults"), control_type="fault")
    checkpoints = _decode_controls(
        item.get("checkpoints"),
        control_type="checkpoint",
    )
    raw_observations = item.get("observations")
    if (
        faults is None
        or checkpoints is None
        or type(raw_observations) is not list
        or len(raw_observations) > MAX_OBSERVATIONS
    ):
        return None
    observations: list[RehearsalObservation] = []
    for expected_sequence, raw_observation in enumerate(
        raw_observations,
        start=1,
    ):
        observation = _exact_mapping(
            raw_observation,
            required=_OBSERVATION_FIELDS,
        )
        if observation is None:
            return None
        sequence = _dynamo_integer(observation["sequence"])
        observed_at = _dynamo_integer(observation["observed_at_epoch"])
        payload = _normalize_observation_payload(
            observation["kind"],
            observation["payload"],
        )
        if (
            sequence != expected_sequence
            or observed_at is None
            or observed_at < binding.expires_at_epoch - MAX_LEDGER_TTL_SECONDS
            or observed_at > binding.expires_at_epoch
            or observed_at > now_epoch + 300
            or payload is None
            or (_canonical_size(payload) or MAX_OBSERVATION_BYTES + 1) > MAX_OBSERVATION_BYTES
        ):
            return None
        observations.append(
            RehearsalObservation(
                sequence=sequence,
                kind=observation["kind"],
                observed_at_epoch=observed_at,
                payload=MappingProxyType(payload),
            )
        )
    record = _Record(
        binding=binding,
        revision=revision,
        updated_at_epoch=updated,
        faults=faults,
        checkpoints=checkpoints,
        observations=tuple(observations),
    )
    size = _canonical_size(_record_item(record))
    return record if size is not None and size <= MAX_LEDGER_BYTES else None


def _same_binding(left: RehearsalBinding, right: RehearsalBinding) -> bool:
    return left == right


def _same_claim_without_fence(
    left: RehearsalBinding,
    right: RehearsalBinding,
) -> bool:
    return (
        left.tenant_id == right.tenant_id
        and left.project_id == right.project_id
        and left.correlation_id == right.correlation_id
        and left.owner_id == right.owner_id
        and left.release_commit == right.release_commit
        and left.expires_at_epoch == right.expires_at_epoch
    )


def _conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error")
    return isinstance(error, dict) and error.get("Code") == "ConditionalCheckFailedException"


def _required_observation_kinds(
    values: Iterable[str],
) -> frozenset[str] | None:
    required: set[str] = set()
    try:
        for index, value in enumerate(values):
            if index >= len(OBSERVATIONS) or value not in OBSERVATIONS:
                return None
            required.add(value)
    except Exception:
        return None
    return frozenset(required)


class RehearsalControlLedger:
    """Fenced DynamoDB ledger with fail-open production hook operations."""

    def __init__(
        self,
        *,
        table: _DynamoTable | None = None,
        environ: Mapping[str, str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        environment = os.environ if environ is None else environ
        raw_name = environment.get(TABLE_ENV)
        self._table_name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
        self._table = table if self._table_name is not None else None
        self._now = now or (lambda: datetime.now(timezone.utc))

    @property
    def enabled(self) -> bool:
        return self._table_name is not None

    def _now_epoch(self) -> int | None:
        try:
            current = self._now()
            if not isinstance(current, datetime) or current.tzinfo is None:
                return None
            return int(current.astimezone(timezone.utc).timestamp())
        except Exception:
            return None

    def _resolve_table(self) -> _DynamoTable:
        if self._table is None:
            import boto3
            from botocore.config import Config

            resource = boto3.resource(
                "dynamodb",
                config=Config(
                    connect_timeout=1,
                    read_timeout=2,
                    retries={"mode": "standard", "total_max_attempts": 2},
                ),
            )
            self._table = resource.Table(self._table_name)
        return self._table

    def _read(
        self,
        binding: RehearsalBinding,
        *,
        now_epoch: int,
        require_active: bool = True,
    ) -> tuple[bool, _Record | None]:
        try:
            response = self._resolve_table().get_item(
                Key={LEDGER_KEY_ATTRIBUTE: _ledger_key(binding)},
                ConsistentRead=True,
            )
        except Exception:
            return False, None
        if not isinstance(response, Mapping):
            return False, None
        item = response.get("Item")
        if item is None:
            return True, None
        record = _decode_record(
            item,
            now_epoch=now_epoch,
            require_active=require_active,
        )
        return (record is not None), record

    def _put_new(self, record: _Record) -> str:
        item = _record_item(record)
        if (_canonical_size(item) or MAX_LEDGER_BYTES + 1) > MAX_LEDGER_BYTES:
            return "failed"
        try:
            self._resolve_table().put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(#ledger_key)",
                ExpressionAttributeNames={
                    "#ledger_key": LEDGER_KEY_ATTRIBUTE,
                },
            )
            return "written"
        except Exception as exc:
            return "conflict" if _conditional_failure(exc) else "failed"

    def _replace(self, previous: _Record, replacement: _Record) -> str:
        item = _record_item(replacement)
        if (_canonical_size(item) or MAX_LEDGER_BYTES + 1) > MAX_LEDGER_BYTES:
            return "failed"
        condition = (
            "#revision = :revision AND #tenant = :tenant AND "
            "#project = :project AND #correlation = :correlation AND "
            "#owner = :owner AND #release = :release AND "
            "#fence = :fence AND #expiry = :expiry"
        )
        try:
            self._resolve_table().put_item(
                Item=item,
                ConditionExpression=condition,
                ExpressionAttributeNames={
                    "#correlation": "correlation_id",
                    "#expiry": TTL_ATTRIBUTE,
                    "#fence": "fence_token",
                    "#owner": "owner_id",
                    "#project": "project_id",
                    "#release": "release_commit",
                    "#revision": "revision",
                    "#tenant": "tenant_id",
                },
                ExpressionAttributeValues={
                    ":correlation": previous.binding.correlation_id,
                    ":expiry": previous.binding.expires_at_epoch,
                    ":fence": previous.binding.fence_token,
                    ":owner": previous.binding.owner_id,
                    ":project": previous.binding.project_id,
                    ":release": previous.binding.release_commit,
                    ":revision": previous.revision,
                    ":tenant": previous.binding.tenant_id,
                },
            )
            return "written"
        except Exception as exc:
            return "conflict" if _conditional_failure(exc) else "failed"

    def claim(self, binding: RehearsalBinding) -> int | None:
        """Claim or monotonically re-fence a ledger for a trusted worker."""

        if not self.enabled:
            return None
        now_epoch = self._now_epoch()
        if now_epoch is None or not _binding_is_current(
            binding,
            now_epoch=now_epoch,
        ):
            return None
        for _ in range(MAX_CAS_ATTEMPTS):
            readable, current = self._read(
                binding,
                now_epoch=now_epoch,
                require_active=False,
            )
            if not readable:
                return None
            if current is None:
                created = _empty_record(
                    binding,
                    now_epoch=now_epoch,
                    revision=1,
                )
                status = self._put_new(created)
                if status == "written":
                    return created.revision
                if status == "failed":
                    return None
                continue
            if _same_binding(current.binding, binding):
                return current.revision
            if (
                not _same_claim_without_fence(current.binding, binding)
                or binding.fence_token <= current.binding.fence_token
            ):
                return None
            replacement = _empty_record(
                binding,
                now_epoch=now_epoch,
                revision=current.revision + 1,
            )
            status = self._replace(current, replacement)
            if status == "written":
                return replacement.revision
            if status == "failed":
                return None
        return None

    def write_control(
        self,
        binding: RehearsalBinding,
        *,
        control_type: str,
        name: str,
        parameters: Mapping[str, Any],
        active: bool,
        expected_revision: int,
    ) -> int | None:
        """CAS-install a fixed control for a trusted rehearsal worker."""

        normalized = _normalize_control_parameters(
            control_type,
            name,
            parameters,
        )
        if (
            not self.enabled
            or normalized is None
            or type(active) is not bool
            or not _positive_integer(
                expected_revision,
                maximum=MAX_FENCE_TOKEN,
            )
        ):
            return None
        now_epoch = self._now_epoch()
        if now_epoch is None or not _binding_is_current(
            binding,
            now_epoch=now_epoch,
        ):
            return None
        readable, current = self._read(binding, now_epoch=now_epoch)
        if (
            not readable
            or current is None
            or not _same_binding(current.binding, binding)
            or current.revision != expected_revision
        ):
            return None
        faults = {
            key: {
                "active": value["active"],
                "parameters": dict(value["parameters"]),
            }
            for key, value in current.faults.items()
        }
        checkpoints = {
            key: {
                "active": value["active"],
                "parameters": dict(value["parameters"]),
            }
            for key, value in current.checkpoints.items()
        }
        destination = faults if control_type == "fault" else checkpoints
        destination[name] = {
            "active": active,
            "parameters": normalized,
        }
        replacement = _Record(
            binding=binding,
            revision=current.revision + 1,
            updated_at_epoch=now_epoch,
            faults=faults,
            checkpoints=checkpoints,
            observations=current.observations,
        )
        if self._replace(current, replacement) != "written":
            return None
        return replacement.revision

    def read_active_fault(
        self,
        binding: RehearsalBinding,
        name: str,
    ) -> ActiveRehearsalControl | None:
        """Read an active approved fault, failing open on any ledger problem."""

        return self._read_active_control(binding, "fault", name)

    def read_active_checkpoint(
        self,
        binding: RehearsalBinding,
        name: str,
    ) -> ActiveRehearsalControl | None:
        """Read an active checkpoint, failing open on any ledger problem."""

        return self._read_active_control(binding, "checkpoint", name)

    def _read_active_control(
        self,
        binding: RehearsalBinding,
        control_type: str,
        name: str,
    ) -> ActiveRehearsalControl | None:
        allowed = FAULTS if control_type == "fault" else CHECKPOINTS
        if not self.enabled or not isinstance(name, str) or name not in allowed:
            return None
        now_epoch = self._now_epoch()
        if now_epoch is None:
            return None
        readable, record = self._read(binding, now_epoch=now_epoch)
        if not readable or record is None or not _same_binding(record.binding, binding):
            return None
        controls = record.faults if control_type == "fault" else record.checkpoints
        control = controls.get(name)
        if control is None or control["active"] is not True:
            return None
        return ActiveRehearsalControl(
            control_type=control_type,
            name=name,
            parameters=MappingProxyType(dict(control["parameters"])),
            revision=record.revision,
            expires_at_epoch=binding.expires_at_epoch,
        )

    def append_observation(
        self,
        binding: RehearsalBinding,
        kind: str,
        payload: Mapping[str, Any],
    ) -> bool:
        """Append one bounded observation without affecting normal behavior."""

        normalized = _normalize_observation_payload(kind, payload)
        payload_size = _canonical_size(normalized) if normalized is not None else None
        if not self.enabled or normalized is None or payload_size is None or payload_size > MAX_OBSERVATION_BYTES:
            return False
        now_epoch = self._now_epoch()
        if now_epoch is None or not _binding_is_current(
            binding,
            now_epoch=now_epoch,
        ):
            return False
        for _ in range(MAX_CAS_ATTEMPTS):
            readable, current = self._read(binding, now_epoch=now_epoch)
            if (
                not readable
                or current is None
                or not _same_binding(current.binding, binding)
                or len(current.observations) >= MAX_OBSERVATIONS
            ):
                return False
            observation = RehearsalObservation(
                sequence=len(current.observations) + 1,
                kind=kind,
                observed_at_epoch=now_epoch,
                payload=MappingProxyType(normalized),
            )
            replacement = _Record(
                binding=binding,
                revision=current.revision + 1,
                updated_at_epoch=now_epoch,
                faults=current.faults,
                checkpoints=current.checkpoints,
                observations=(*current.observations, observation),
            )
            status = self._replace(current, replacement)
            if status == "written":
                return True
            if status == "failed":
                return False
        return False

    def collect_observations(
        self,
        binding: RehearsalBinding,
        *,
        required_kinds: Iterable[str] = (),
    ) -> tuple[RehearsalObservation, ...]:
        """Read evidence strongly and fail closed with a sanitized error."""

        required = _required_observation_kinds(required_kinds)
        if required is None:
            raise RehearsalEvidenceUnavailable("rehearsal evidence is unavailable") from None
        now_epoch = self._now_epoch()
        if not self.enabled or now_epoch is None:
            raise RehearsalEvidenceUnavailable("rehearsal evidence is unavailable") from None
        readable, record = self._read(binding, now_epoch=now_epoch)
        if (
            not readable
            or record is None
            or not _same_binding(record.binding, binding)
            or not required.issubset({observation.kind for observation in record.observations})
        ):
            raise RehearsalEvidenceUnavailable("rehearsal evidence is unavailable") from None
        return record.observations
