"""Validated query-plane configuration without stored credentials."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ATHENA_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}$")
_AWS_REGION = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-[0-9]+$")
_ROLE_ARN = re.compile(
    r"^arn:(?:aws|aws-us-gov|aws-cn):iam::[0-9]{12}:"
    r"role/[A-Za-z0-9+=,.@_/-]{1,512}$"
)
_MAX_BINDINGS_CHARACTERS = 2_048


class QueryConfigurationError(ValueError):
    """Raised when query-plane configuration is unsafe or ambiguous."""


def _required_string(
    value: Any,
    name: str,
    *,
    max_length: int = 512,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_length
        or any(ord(character) < 32 for character in value)
    ):
        raise QueryConfigurationError(
            f"{name} must be a non-empty string without surrounding "
            "whitespace or control characters"
        )
    return value


def _identifier(value: Any, name: str) -> str:
    normalized = _required_string(value, name, max_length=128)
    if _IDENTIFIER.fullmatch(normalized) is None:
        raise QueryConfigurationError(
            f"{name} must start with a letter or number and contain only "
            "letters, numbers, dots, underscores, or hyphens"
        )
    return normalized


def _athena_name(value: Any, name: str) -> str:
    normalized = _required_string(value, name, max_length=255)
    if _ATHENA_NAME.fullmatch(normalized) is None:
        raise QueryConfigurationError(
            f"{name} contains unsupported Athena identifier characters"
        )
    return normalized


def _role_arn(value: Any, name: str) -> str:
    normalized = _required_string(value, name, max_length=600)
    if "*" in normalized or _ROLE_ARN.fullmatch(normalized) is None:
        raise QueryConfigurationError(
            f"{name} must be a concrete IAM role ARN"
        )
    return normalized


def _region(value: Any, name: str) -> str:
    normalized = _required_string(value, name, max_length=32)
    if _AWS_REGION.fullmatch(normalized) is None:
        raise QueryConfigurationError(f"{name} must be an AWS region")
    return normalized


def _strict_object(
    value: Any,
    name: str,
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise QueryConfigurationError(f"{name} must be an object")
    optional = optional or set()
    missing = required.difference(value)
    unknown = set(value).difference(required | optional)
    if missing:
        raise QueryConfigurationError(
            f"{name} is missing required fields: {', '.join(sorted(missing))}"
        )
    if unknown:
        raise QueryConfigurationError(
            f"{name} contains unsupported fields: {', '.join(sorted(unknown))}"
        )
    return value


def _revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise QueryConfigurationError(
            "datasource revision must be a non-negative integer"
        )
    return value


def _timestamp(value: Any, name: str) -> str:
    normalized = _required_string(value, name, max_length=64)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise QueryConfigurationError(
            f"{name} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise QueryConfigurationError(f"{name} must include a timezone")
    return normalized


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class AthenaRoleBinding:
    """One platform-approved IAM role bound to a canonical project."""

    tenant_id: str
    project_id: str
    role_arn: str

    @classmethod
    def from_mapping(cls, raw: Any) -> AthenaRoleBinding:
        value = _strict_object(
            raw,
            "Athena role binding",
            required={"tenant_id", "project_id", "role_arn"},
        )
        return cls(
            tenant_id=_identifier(value["tenant_id"], "tenant_id"),
            project_id=_identifier(value["project_id"], "project_id"),
            role_arn=_role_arn(value["role_arn"], "role_arn"),
        )


class AthenaRoleBindings:
    """Immutable deployment-time allowlist for per-project query roles."""

    def __init__(
        self,
        bindings: tuple[AthenaRoleBinding, ...] = (),
    ) -> None:
        seen: set[tuple[str, str, str]] = set()
        by_project: dict[tuple[str, str], set[str]] = {}
        for binding in bindings:
            identity = (
                binding.tenant_id,
                binding.project_id,
                binding.role_arn,
            )
            if identity in seen:
                raise QueryConfigurationError(
                    "Athena role bindings must not contain duplicates"
                )
            seen.add(identity)
            by_project.setdefault(
                (binding.tenant_id, binding.project_id),
                set(),
            ).add(binding.role_arn)
        self._bindings = tuple(bindings)
        self._by_project = {
            key: frozenset(value) for key, value in by_project.items()
        }

    @classmethod
    def from_json(cls, raw: str | None) -> AthenaRoleBindings:
        if raw in (None, ""):
            return cls()
        if not isinstance(raw, str):
            raise QueryConfigurationError(
                "AXON_ATHENA_QUERY_BINDINGS must be JSON text"
            )
        if len(raw) > _MAX_BINDINGS_CHARACTERS:
            raise QueryConfigurationError(
                "AXON_ATHENA_QUERY_BINDINGS exceeds the AgentCore "
                "2,048-character environment value limit"
            )

        def _reject_duplicates(
            pairs: list[tuple[str, Any]],
        ) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise QueryConfigurationError(
                        f"Athena role binding contains duplicate field {key!r}"
                    )
                value[key] = item
            return value

        try:
            payload = json.loads(raw, object_pairs_hook=_reject_duplicates)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QueryConfigurationError(
                "AXON_ATHENA_QUERY_BINDINGS must be valid UTF-8 JSON"
            ) from exc
        if not isinstance(payload, list):
            raise QueryConfigurationError(
                "AXON_ATHENA_QUERY_BINDINGS must be a JSON array"
            )
        return cls(
            tuple(AthenaRoleBinding.from_mapping(item) for item in payload)
        )

    @property
    def role_arns(self) -> frozenset[str]:
        return frozenset(binding.role_arn for binding in self._bindings)

    @property
    def empty(self) -> bool:
        return not self._bindings

    def allows(
        self,
        tenant_id: str,
        project_id: str,
        role_arn: str,
    ) -> bool:
        return role_arn in self._by_project.get(
            (tenant_id, project_id),
            frozenset(),
        )

    def to_list(self) -> list[dict[str, str]]:
        return [asdict(binding) for binding in self._bindings]


@dataclass(frozen=True)
class AthenaDatasource:
    """Tenant-owned Athena metadata bound to a platform-approved IAM role."""

    datasource_id: str
    tenant_id: str
    project_id: str
    name: str
    role_arn: str
    region: str
    catalog: str
    database: str
    workgroup: str
    enabled: bool = True
    revision: int = 0
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "datasource_id",
            _identifier(self.datasource_id, "datasource_id"),
        )
        object.__setattr__(
            self,
            "tenant_id",
            _identifier(self.tenant_id, "tenant_id"),
        )
        object.__setattr__(
            self,
            "project_id",
            _identifier(self.project_id, "project_id"),
        )
        object.__setattr__(
            self,
            "name",
            _required_string(self.name, "name", max_length=256),
        )
        object.__setattr__(
            self,
            "role_arn",
            _role_arn(self.role_arn, "role_arn"),
        )
        object.__setattr__(
            self,
            "region",
            _region(self.region, "region"),
        )
        object.__setattr__(
            self,
            "catalog",
            _athena_name(self.catalog, "catalog"),
        )
        object.__setattr__(
            self,
            "database",
            _athena_name(self.database, "database"),
        )
        object.__setattr__(
            self,
            "workgroup",
            _athena_name(self.workgroup, "workgroup"),
        )
        if not isinstance(self.enabled, bool):
            raise QueryConfigurationError("enabled must be a boolean")
        object.__setattr__(self, "revision", _revision(self.revision))
        created_at = self.created_at or _utcnow()
        updated_at = self.updated_at or created_at
        object.__setattr__(
            self,
            "created_at",
            _timestamp(created_at, "created_at"),
        )
        object.__setattr__(
            self,
            "updated_at",
            _timestamp(updated_at, "updated_at"),
        )

    @classmethod
    def from_mapping(
        cls,
        raw: Any,
        *,
        tenant_id: str,
        project_id: str,
        datasource_id: str | None = None,
        revision: int = 0,
        created_at: str = "",
        updated_at: str = "",
    ) -> AthenaDatasource:
        value = _strict_object(
            raw,
            "datasource",
            required={
                "name",
                "role_arn",
                "region",
                "catalog",
                "database",
                "workgroup",
            },
            optional={
                "datasource_id",
                "enabled",
            },
        )
        resolved_id = (
            datasource_id
            if datasource_id is not None
            else value.get("datasource_id")
        )
        return cls(
            datasource_id=_identifier(
                resolved_id,
                "datasource_id",
            ),
            tenant_id=tenant_id,
            project_id=project_id,
            name=value["name"],
            role_arn=value["role_arn"],
            region=value["region"],
            catalog=value["catalog"],
            database=value["database"],
            workgroup=value["workgroup"],
            enabled=value.get("enabled", True),
            revision=revision,
            created_at=created_at,
            updated_at=updated_at,
        )

    def to_dict(self, *, include_role: bool = True) -> dict[str, Any]:
        value = asdict(self)
        if not include_role:
            value.pop("role_arn")
            value["role_configured"] = True
        return value
