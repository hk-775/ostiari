"""Deterministic, non-mutating AgentCore park and resume planning."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping


_SCHEMA_PACKAGE = "src.gateway.deployment.schemas"
_CONTEXT_SCHEMA_NAME = "runtime-lifecycle-context-v1.schema.json"
_PLAN_SCHEMA_NAME = "runtime-lifecycle-plan-v1.schema.json"
_PLAN_SCHEMA = "urn:axonllm:runtime-lifecycle-plan:v1"
_MAX_INPUT_BYTES = 16 * 1024 * 1024
_RETAINED_KINDS = frozenset(
    {
        "application-state",
        "control-plane",
        "identity",
        "workers",
    }
)
_PROTECTED_RESOURCE_TYPES = frozenset(
    {
        "AWS::Backup::BackupPlan",
        "AWS::Backup::BackupVault",
        "AWS::Cognito::UserPool",
        "AWS::Cognito::UserPoolClient",
        "AWS::DynamoDB::Table",
        "AWS::KMS::Alias",
        "AWS::KMS::Key",
        "AWS::S3::Bucket",
        "AWS::SecretsManager::Secret",
        "AWS::SNS::Topic",
        "AWS::SQS::Queue",
    }
)
_SENTINEL = (
    "ParkedSentinel",
    "AWS::CloudFormation::WaitConditionHandle",
)


class RuntimeLifecycleError(ValueError):
    """Raised when park or resume evidence is incomplete or unsafe."""


def runtime_lifecycle_context_schema() -> dict[str, Any]:
    """Return a fresh copy of the packaged lifecycle input schema."""

    return _load_schema(_CONTEXT_SCHEMA_NAME)


def runtime_lifecycle_plan_schema() -> dict[str, Any]:
    """Return a fresh copy of the packaged lifecycle output schema."""

    return _load_schema(_PLAN_SCHEMA_NAME)


def load_runtime_lifecycle_context(
    path: str | Path,
) -> dict[str, Any]:
    """Load one strict non-secret park or resume context."""

    value, _ = _read_json(Path(path), "runtime lifecycle context")
    return validate_runtime_lifecycle_context(value)


def load_runtime_lifecycle_plan(
    path: str | Path,
) -> dict[str, Any]:
    """Load one content-addressed runtime lifecycle plan."""

    value, _ = _read_json(Path(path), "runtime lifecycle plan")
    return validate_runtime_lifecycle_plan(value)


def validate_runtime_lifecycle_context(
    value: object,
) -> dict[str, Any]:
    """Validate lifecycle structure and cross-stack safety rules."""

    if not isinstance(value, dict):
        raise RuntimeLifecycleError("runtime lifecycle context root must be an object")
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import best_match
    except ImportError as exc:  # pragma: no cover - clean install guard
        raise RuntimeLifecycleError("runtime lifecycle planning requires the 'deployment' package extra") from exc

    schema = runtime_lifecycle_context_schema()
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        error = best_match(errors)
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise RuntimeLifecycleError(f"{location}: {error.message}")

    context = copy.deepcopy(value)
    operation = context["operation"]
    expected_states = {
        "park": ("active", "parked"),
        "resume": ("parked", "active"),
    }[operation]
    if (
        context["current_state"],
        context["desired_state"],
    ) != expected_states:
        raise RuntimeLifecycleError(f"{operation} has an illegal runtime state transition")
    runtime = context["runtime"]
    if runtime["external_state_mode"] is not True:
        raise RuntimeLifecycleError("runtime lifecycle requires external application state")
    if runtime["active_template_sha256"] == runtime["parked_template_sha256"]:
        raise RuntimeLifecycleError("active and parked runtime templates must differ")
    _validate_change_set_binding(
        context,
        runtime,
        expected_stack_prefix="AxonLLMAgentCoreStack",
        required_resource_type="AWS::BedrockAgentCore::Runtime",
    )
    _validate_image_reference(
        runtime["image_reference"],
        account_id=context["account_id"],
        partition=context["partition"],
        region=context["region"],
    )

    managed_network = context["managed_network"]
    if context["network_mode"] == "managed":
        if managed_network is None:
            raise RuntimeLifecycleError("managed network mode requires a lifecycle change set")
        if managed_network["active_template_sha256"] == managed_network["parked_template_sha256"]:
            raise RuntimeLifecycleError("active and parked managed-network templates must differ")
        _validate_change_set_binding(
            context,
            managed_network,
            expected_stack_prefix="AxonLLMManagedNetworkStack",
            required_resource_type="AWS::EC2::VPC",
        )
    elif managed_network is not None:
        raise RuntimeLifecycleError("existing or public network mode cannot mutate a managed-network stack")

    retained = context["retained_stacks"]
    retained_kinds = {item["kind"] for item in retained}
    if retained_kinds != _RETAINED_KINDS:
        missing = sorted(_RETAINED_KINDS - retained_kinds)
        extra = sorted(retained_kinds - _RETAINED_KINDS)
        raise RuntimeLifecycleError(f"retained stack kinds do not match: missing={missing}, extra={extra}")
    if len(retained_kinds) != len(retained):
        raise RuntimeLifecycleError("retained stack kinds must be unique")
    retained_names = [item["stack_name"] for item in retained]
    if len(set(retained_names)) != len(retained_names):
        raise RuntimeLifecycleError("retained stack names must be unique")
    affected_names = {runtime["stack_name"]}
    if managed_network is not None:
        affected_names.add(managed_network["stack_name"])
    overlap = sorted(affected_names.intersection(retained_names))
    if overlap:
        raise RuntimeLifecycleError("runtime lifecycle cannot mutate retained stacks: " + ", ".join(overlap))
    return context


def validate_runtime_lifecycle_plan(
    value: object,
) -> dict[str, Any]:
    """Validate one generated lifecycle plan and its content hash."""

    if not isinstance(value, dict):
        raise RuntimeLifecycleError("runtime lifecycle plan root must be an object")
    plan = copy.deepcopy(value)
    _validate_generated_plan(plan)
    return plan


def build_runtime_lifecycle_plan(
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a hash-bound park or resume plan without AWS access."""

    normalized = validate_runtime_lifecycle_context(dict(context))
    desired_active = normalized["desired_state"] == "active"
    affected = [
        _affected_stack(
            normalized["runtime"],
            component="agentcore-runtime",
        )
    ]
    managed_network = normalized["managed_network"]
    if managed_network is not None:
        affected.append(
            _affected_stack(
                managed_network,
                component="managed-network",
            )
        )
    expected_network = (
        "ready"
        if desired_active and managed_network is not None
        else "absent"
        if managed_network is not None
        else "customer-owned"
        if normalized["network_mode"] == "existing"
        else "public"
    )
    execution_order = ["agentcore-runtime"]
    if managed_network is not None:
        execution_order = (
            ["agentcore-runtime", "managed-network"]
            if normalized["operation"] == "park"
            else ["managed-network", "agentcore-runtime"]
        )
    body = {
        "schema": _PLAN_SCHEMA,
        "schema_version": 1,
        "operation": normalized["operation"],
        "mutating": False,
        "approval_required": True,
        "account_id": normalized["account_id"],
        "partition": normalized["partition"],
        "region": normalized["region"],
        "source_revision": normalized["source_revision"],
        "deployment_plan_id": normalized["deployment_plan_id"],
        "deployment_descriptor_id": (normalized["deployment_descriptor_id"]),
        "current_state": normalized["current_state"],
        "desired_state": normalized["desired_state"],
        "network_mode": normalized["network_mode"],
        "execution_order": execution_order,
        "inputs": {
            "agentcore_image": normalized["runtime"]["image_reference"],
            "last_known_good_configuration_sha256": (normalized["last_known_good_configuration_sha256"]),
            "release_evidence_ids": sorted(normalized["release_evidence_ids"]),
        },
        "affected_stacks": sorted(
            affected,
            key=lambda item: item["component"],
        ),
        "retained_stacks": sorted(
            normalized["retained_stacks"],
            key=lambda item: item["kind"],
        ),
        "expected_state": {
            "agentcore_runtime": ("ready" if desired_active else "absent"),
            "runtime_endpoint": ("ready" if desired_active else "absent"),
            "managed_network": expected_network,
            "control_plane": "available",
            "administration": "available",
            "durable_state": "retained",
        },
        "rollback": {
            "operation": ("park" if desired_active else "resume"),
            "window_hours": normalized["rollback_window_hours"],
            "runtime_template_sha256": (
                normalized["runtime"]["parked_template_sha256"]
                if desired_active
                else normalized["runtime"]["active_template_sha256"]
            ),
            "managed_network_template_sha256": (
                None
                if managed_network is None
                else (
                    managed_network["parked_template_sha256"]
                    if desired_active
                    else managed_network["active_template_sha256"]
                )
            ),
        },
        "gates": [
            {
                "name": "external_state_mode",
                "passed": True,
            },
            {
                "name": "retained_stacks_unchanged",
                "passed": True,
            },
            {
                "name": "protected_resources_absent",
                "passed": True,
            },
            {
                "name": "change_sets_available",
                "passed": True,
            },
            {
                "name": "no_replacements",
                "passed": True,
            },
            {
                "name": "immutable_resume_inputs_bound",
                "passed": True,
            },
            {
                "name": "dependency_order_bound",
                "passed": True,
            },
        ],
    }
    plan = {
        "plan_id": _sha256_value(body),
        **body,
    }
    _validate_generated_plan(plan)
    return plan


def create_runtime_lifecycle_plan(
    *,
    context_path: str | Path,
    output_directory: str | Path,
) -> tuple[dict[str, Any], Path]:
    """Load and write a non-mutating lifecycle plan."""

    plan = build_runtime_lifecycle_plan(load_runtime_lifecycle_context(context_path))
    path = write_runtime_lifecycle_plan(plan, output_directory)
    return plan, path


def write_runtime_lifecycle_plan(
    plan: Mapping[str, Any],
    output_directory: str | Path,
) -> Path:
    """Write one content-addressed lifecycle plan atomically."""

    value = copy.deepcopy(dict(plan))
    _validate_generated_plan(value)
    output = Path(output_directory)
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeLifecycleError(f"unable to create lifecycle plan directory {output}: {exc}") from exc
    identifier = value["plan_id"].removeprefix("sha256:")
    path = output / f"runtime-{value['operation']}-{identifier}.json"
    _atomic_write(path, value)
    return path


def _validate_change_set_binding(
    context: dict[str, Any],
    stack: dict[str, Any],
    *,
    expected_stack_prefix: str,
    required_resource_type: str,
) -> None:
    stack_name = stack["stack_name"]
    if stack_name != expected_stack_prefix and not stack_name.startswith(expected_stack_prefix + "-"):
        raise RuntimeLifecycleError(f"lifecycle stack {stack_name!r} has an unexpected identity")
    expected_arn_prefix = (
        f"arn:{context['partition']}:cloudformation:{context['region']}:{context['account_id']}:changeSet/"
    )
    if not stack["change_set_arn"].startswith(expected_arn_prefix):
        raise RuntimeLifecycleError(f"{stack_name} change set is not bound to the deployment account and region")
    expected_action = "Remove" if context["operation"] == "park" else "Add"
    required_seen = False
    logical_ids: set[str] = set()
    for change in stack["changes"]:
        logical_id = change["logical_id"]
        if logical_id in logical_ids:
            raise RuntimeLifecycleError(f"{stack_name} change set contains duplicate logical ID {logical_id!r}")
        logical_ids.add(logical_id)
        resource_type = change["resource_type"]
        if resource_type in _PROTECTED_RESOURCE_TYPES:
            raise RuntimeLifecycleError(f"{stack_name} change set touches protected resource type {resource_type}")
        if (
            logical_id,
            resource_type,
        ) == _SENTINEL:
            allowed_sentinel_action = "Add" if context["operation"] == "park" else "Remove"
            if change["action"] != allowed_sentinel_action:
                raise RuntimeLifecycleError(f"{stack_name} parked sentinel action is invalid")
            continue
        if change["action"] != expected_action:
            raise RuntimeLifecycleError(f"{stack_name} lifecycle change must contain only {expected_action} actions")
        if resource_type == required_resource_type:
            required_seen = True
    if not required_seen:
        raise RuntimeLifecycleError(f"{stack_name} lifecycle change lacks {required_resource_type}")


def _validate_image_reference(
    value: str,
    *,
    account_id: str,
    partition: str,
    region: str,
) -> None:
    dns_suffix = "amazonaws.com.cn" if partition == "aws-cn" else "amazonaws.com"
    prefix = f"{account_id}.dkr.ecr.{region}.{dns_suffix}/"
    if (
        not value.startswith(prefix)
        or "@sha256:" not in value
        or len(value.rsplit("@sha256:", 1)[1]) != 64
        or any(character not in "0123456789abcdef" for character in value.rsplit("@sha256:", 1)[1])
    ):
        raise RuntimeLifecycleError("AgentCore image is not digest-pinned in the deployment account and region")


def _affected_stack(
    stack: dict[str, Any],
    *,
    component: str,
) -> dict[str, Any]:
    return {
        "component": component,
        "stack_name": stack["stack_name"],
        "current_stack_state_sha256": (stack["current_stack_state_sha256"]),
        "active_template_sha256": stack["active_template_sha256"],
        "parked_template_sha256": stack["parked_template_sha256"],
        "change_set_arn": stack["change_set_arn"],
        "change_set_status": stack["change_set_status"],
        "execution_status": stack["execution_status"],
        "changes": sorted(
            stack["changes"],
            key=lambda item: item["logical_id"],
        ),
    }


def _validate_generated_plan(plan: dict[str, Any]) -> None:
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import best_match
    except ImportError as exc:  # pragma: no cover - clean install guard
        raise RuntimeLifecycleError("runtime lifecycle planning requires the 'deployment' package extra") from exc
    schema = runtime_lifecycle_plan_schema()
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema).iter_errors(plan))
    if errors:
        error = best_match(errors)
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise RuntimeLifecycleError(f"generated lifecycle plan {location}: {error.message}")
    expected_id = _sha256_value({key: value for key, value in plan.items() if key != "plan_id"})
    if plan.get("plan_id") != expected_id:
        raise RuntimeLifecycleError("runtime lifecycle plan hash does not match its content")
    if plan.get("mutating") is not False:
        raise RuntimeLifecycleError("runtime lifecycle plan must be non-mutating")
    _validate_plan_semantics(plan)


def _validate_plan_semantics(plan: dict[str, Any]) -> None:
    expected_states = {
        "park": ("active", "parked"),
        "resume": ("parked", "active"),
    }[plan["operation"]]
    if (plan["current_state"], plan["desired_state"]) != expected_states:
        raise RuntimeLifecycleError("runtime lifecycle plan has an illegal state transition")

    affected = plan["affected_stacks"]
    components = [item["component"] for item in affected]
    expected_components = {"agentcore-runtime"}
    if plan["network_mode"] == "managed":
        expected_components.add("managed-network")
    if set(components) != expected_components or len(set(components)) != len(components):
        raise RuntimeLifecycleError("runtime lifecycle plan affected stack set is invalid")

    expected_order = ["agentcore-runtime"]
    if plan["network_mode"] == "managed":
        expected_order = (
            ["agentcore-runtime", "managed-network"]
            if plan["operation"] == "park"
            else ["managed-network", "agentcore-runtime"]
        )
    if plan["execution_order"] != expected_order:
        raise RuntimeLifecycleError("runtime lifecycle plan execution order is invalid")

    affected_by_component = {item["component"]: item for item in affected}
    runtime = affected_by_component["agentcore-runtime"]
    _validate_change_set_binding(
        plan,
        runtime,
        expected_stack_prefix="AxonLLMAgentCoreStack",
        required_resource_type="AWS::BedrockAgentCore::Runtime",
    )
    network = affected_by_component.get("managed-network")
    if network is not None:
        _validate_change_set_binding(
            plan,
            network,
            expected_stack_prefix="AxonLLMManagedNetworkStack",
            required_resource_type="AWS::EC2::VPC",
        )
    _validate_image_reference(
        plan["inputs"]["agentcore_image"],
        account_id=plan["account_id"],
        partition=plan["partition"],
        region=plan["region"],
    )

    retained = plan["retained_stacks"]
    retained_kinds = [item["kind"] for item in retained]
    retained_names = [item["stack_name"] for item in retained]
    if (
        set(retained_kinds) != _RETAINED_KINDS
        or len(set(retained_kinds)) != len(retained_kinds)
        or len(set(retained_names)) != len(retained_names)
    ):
        raise RuntimeLifecycleError("runtime lifecycle plan retained stack set is invalid")
    affected_names = {item["stack_name"] for item in affected}
    if affected_names.intersection(retained_names):
        raise RuntimeLifecycleError("runtime lifecycle plan overlaps retained and affected stacks")

    desired_active = plan["desired_state"] == "active"
    expected_network = (
        "ready"
        if desired_active and network is not None
        else "absent"
        if network is not None
        else "customer-owned"
        if plan["network_mode"] == "existing"
        else "public"
    )
    expected_state = {
        "agentcore_runtime": "ready" if desired_active else "absent",
        "runtime_endpoint": "ready" if desired_active else "absent",
        "managed_network": expected_network,
        "control_plane": "available",
        "administration": "available",
        "durable_state": "retained",
    }
    if plan["expected_state"] != expected_state:
        raise RuntimeLifecycleError("runtime lifecycle plan expected state is invalid")

    rollback = plan["rollback"]
    if rollback["operation"] != ("park" if desired_active else "resume"):
        raise RuntimeLifecycleError("runtime lifecycle plan rollback operation is invalid")
    expected_runtime_rollback = (
        runtime["parked_template_sha256"] if desired_active else runtime["active_template_sha256"]
    )
    if rollback["runtime_template_sha256"] != expected_runtime_rollback:
        raise RuntimeLifecycleError("runtime lifecycle plan rollback template is invalid")
    expected_network_rollback = (
        None
        if network is None
        else (network["parked_template_sha256"] if desired_active else network["active_template_sha256"])
    )
    if rollback["managed_network_template_sha256"] != expected_network_rollback:
        raise RuntimeLifecycleError("managed-network rollback template is invalid")

    expected_gates = {
        "external_state_mode",
        "retained_stacks_unchanged",
        "protected_resources_absent",
        "change_sets_available",
        "no_replacements",
        "immutable_resume_inputs_bound",
        "dependency_order_bound",
    }
    gate_names = [item["name"] for item in plan["gates"]]
    if set(gate_names) != expected_gates or len(set(gate_names)) != len(gate_names):
        raise RuntimeLifecycleError("runtime lifecycle plan gate set is invalid")


def _load_schema(name: str) -> dict[str, Any]:
    try:
        value = json.loads(files(_SCHEMA_PACKAGE).joinpath(name).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeLifecycleError(f"unable to load packaged runtime lifecycle schema {name}") from exc
    if not isinstance(value, dict):
        raise RuntimeLifecycleError(f"packaged runtime lifecycle schema {name} is invalid")
    return copy.deepcopy(value)


def _read_json(
    path: Path,
    label: str,
) -> tuple[dict[str, Any], bytes]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise RuntimeLifecycleError(f"unable to read {label} {path}: {exc}") from exc
    if size > _MAX_INPUT_BYTES:
        raise RuntimeLifecycleError(f"{label} exceeds {_MAX_INPUT_BYTES} bytes")
    try:
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        RuntimeLifecycleError,
    ) as exc:
        raise RuntimeLifecycleError(f"unable to parse {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeLifecycleError(f"{label} root must be an object")
    return value, raw


def _unique_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RuntimeLifecycleError(f"duplicate JSON field {key!r}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise RuntimeLifecycleError(f"invalid JSON constant {value}")


def _sha256_value(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _atomic_write(path: Path, value: object) -> None:
    descriptor: int | None = None
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(
            descriptor,
            "w",
            encoding="ascii",
            newline="\n",
        ) as handle:
            descriptor = None
            json.dump(
                value,
                handle,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except OSError as exc:
        raise RuntimeLifecycleError(f"unable to write lifecycle plan {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


__all__ = [
    "RuntimeLifecycleError",
    "build_runtime_lifecycle_plan",
    "create_runtime_lifecycle_plan",
    "load_runtime_lifecycle_context",
    "load_runtime_lifecycle_plan",
    "runtime_lifecycle_context_schema",
    "runtime_lifecycle_plan_schema",
    "validate_runtime_lifecycle_context",
    "validate_runtime_lifecycle_plan",
    "write_runtime_lifecycle_plan",
]
