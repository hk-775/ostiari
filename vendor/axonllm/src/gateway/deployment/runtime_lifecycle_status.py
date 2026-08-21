"""Post-operation verification receipts for AgentCore park and resume."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
import stat
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping

from src.gateway.deployment.runtime_lifecycle import (
    RuntimeLifecycleError,
    load_runtime_lifecycle_plan,
    validate_runtime_lifecycle_plan,
)


_SCHEMA_PACKAGE = "src.gateway.deployment.schemas"
_STATUS_SCHEMA_NAME = "runtime-lifecycle-status-v1.schema.json"
_RECEIPT_SCHEMA_NAME = "runtime-lifecycle-receipt-v1.schema.json"
_RECEIPT_SCHEMA = "urn:axonllm:runtime-lifecycle-receipt:v1"
_MAX_INPUT_BYTES = 16 * 1024 * 1024
_RETAINED_KINDS = frozenset(
    {
        "application-state",
        "control-plane",
        "identity",
        "workers",
    }
)


class RuntimeLifecycleStatusError(RuntimeLifecycleError):
    """Raised when lifecycle observations cannot prove the planned state."""


def runtime_lifecycle_status_schema() -> dict[str, Any]:
    """Return a fresh copy of the packaged observation schema."""

    return _load_schema(_STATUS_SCHEMA_NAME)


def runtime_lifecycle_receipt_schema() -> dict[str, Any]:
    """Return a fresh copy of the packaged receipt schema."""

    return _load_schema(_RECEIPT_SCHEMA_NAME)


def load_runtime_lifecycle_status(
    path: str | Path,
) -> dict[str, Any]:
    """Load one strict non-secret post-operation observation."""

    value = _read_json(Path(path), "runtime lifecycle status")
    return validate_runtime_lifecycle_status(value)


def load_runtime_lifecycle_receipt(
    path: str | Path,
) -> dict[str, Any]:
    """Load one content-addressed lifecycle receipt."""

    value = _read_json(Path(path), "runtime lifecycle receipt")
    return validate_runtime_lifecycle_receipt(value)


def validate_runtime_lifecycle_status(
    value: object,
) -> dict[str, Any]:
    """Validate observed stack and resource state."""

    if not isinstance(value, dict):
        raise RuntimeLifecycleStatusError("runtime lifecycle status root must be an object")
    status = copy.deepcopy(value)
    _validate_schema(
        status,
        runtime_lifecycle_status_schema(),
        label="runtime lifecycle status",
    )
    try:
        observed = datetime.fromisoformat(status["observed_at"].removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise RuntimeLifecycleStatusError("runtime lifecycle observed_at is invalid") from exc
    if observed.tzinfo != timezone.utc:
        raise RuntimeLifecycleStatusError("runtime lifecycle observed_at must be UTC")
    retained = status["retained_stacks"]
    kinds = [item["kind"] for item in retained]
    if set(kinds) != _RETAINED_KINDS or len(set(kinds)) != len(kinds):
        raise RuntimeLifecycleStatusError("observed retained stack kinds must be exact and unique")
    names = [item["stack_name"] for item in retained]
    if len(set(names)) != len(names):
        raise RuntimeLifecycleStatusError("observed retained stack names must be unique")
    return status


def validate_runtime_lifecycle_receipt(
    value: object,
) -> dict[str, Any]:
    """Validate one receipt and its content hash."""

    if not isinstance(value, dict):
        raise RuntimeLifecycleStatusError("runtime lifecycle receipt root must be an object")
    receipt = copy.deepcopy(value)
    _validate_schema(
        receipt,
        runtime_lifecycle_receipt_schema(),
        label="runtime lifecycle receipt",
    )
    expected = _sha256_value({key: item for key, item in receipt.items() if key != "receipt_id"})
    if receipt["receipt_id"] != expected:
        raise RuntimeLifecycleStatusError("runtime lifecycle receipt hash does not match its content")
    return receipt


def build_runtime_lifecycle_receipt(
    plan: Mapping[str, Any],
    status: Mapping[str, Any],
) -> dict[str, Any]:
    """Prove that observed resources reached the reviewed lifecycle plan."""

    normalized_plan = validate_runtime_lifecycle_plan(dict(plan))
    normalized_status = validate_runtime_lifecycle_status(dict(status))
    _validate_plan_binding(normalized_plan, normalized_status)

    affected_by_component = {item["component"]: item for item in normalized_plan["affected_stacks"]}
    runtime_plan = affected_by_component["agentcore-runtime"]
    _validate_runtime_observation(
        normalized_plan,
        runtime_plan,
        normalized_status["runtime"],
        normalized_status["control_plane"]["runtime_health"],
    )

    network_plan = affected_by_component.get("managed-network")
    _validate_network_observation(
        normalized_plan,
        network_plan,
        normalized_status["managed_network"],
    )
    _validate_retained_stacks(
        normalized_plan["retained_stacks"],
        normalized_status["retained_stacks"],
    )
    control = normalized_status["control_plane"]
    if control["available"] is not True:
        raise RuntimeLifecycleStatusError("control plane is unavailable after lifecycle operation")
    if control["administration_probe_passed"] is not True:
        raise RuntimeLifecycleStatusError("administration probe did not pass after lifecycle operation")

    affected = [
        {
            "component": "agentcore-runtime",
            "stack_name": normalized_status["runtime"]["stack_name"],
            "stack_status": normalized_status["runtime"]["stack_status"],
            "template_sha256": normalized_status["runtime"]["template_sha256"],
        }
    ]
    if network_plan is not None:
        network = normalized_status["managed_network"]
        if network is None:  # pragma: no cover - guarded above
            raise RuntimeLifecycleStatusError("managed network observation is missing")
        affected.append(
            {
                "component": "managed-network",
                "stack_name": network["stack_name"],
                "stack_status": network["stack_status"],
                "template_sha256": network["template_sha256"],
            }
        )
    body = {
        "schema": _RECEIPT_SCHEMA,
        "schema_version": 1,
        "plan_id": normalized_plan["plan_id"],
        "status_sha256": _sha256_value(normalized_status),
        "operation": normalized_plan["operation"],
        "final_state": normalized_plan["desired_state"],
        "account_id": normalized_plan["account_id"],
        "partition": normalized_plan["partition"],
        "region": normalized_plan["region"],
        "observed_at": normalized_status["observed_at"],
        "source_revision": normalized_plan["source_revision"],
        "deployment_plan_id": normalized_plan["deployment_plan_id"],
        "deployment_descriptor_id": normalized_plan["deployment_descriptor_id"],
        "execution_order": normalized_plan["execution_order"],
        "inputs": normalized_plan["inputs"],
        "affected_stacks": sorted(
            affected,
            key=lambda item: item["component"],
        ),
        "retained_stacks": sorted(
            normalized_status["retained_stacks"],
            key=lambda item: item["kind"],
        ),
        "verification": {
            "lifecycle_plan_bound": True,
            "desired_template_active": True,
            "runtime_state_verified": True,
            "network_state_verified": True,
            "retained_stacks_unchanged": True,
            "control_plane_available": True,
            "administration_probe_passed": True,
        },
    }
    receipt = {
        "receipt_id": _sha256_value(body),
        **body,
    }
    return validate_runtime_lifecycle_receipt(receipt)


def create_runtime_lifecycle_receipt(
    *,
    plan_path: str | Path,
    status_path: str | Path,
    output_directory: str | Path,
) -> tuple[dict[str, Any], Path]:
    """Load evidence and write one content-addressed receipt."""

    receipt = build_runtime_lifecycle_receipt(
        load_runtime_lifecycle_plan(plan_path),
        load_runtime_lifecycle_status(status_path),
    )
    path = write_runtime_lifecycle_receipt(
        receipt,
        output_directory,
    )
    return receipt, path


def write_runtime_lifecycle_receipt(
    receipt: Mapping[str, Any],
    output_directory: str | Path,
) -> Path:
    """Write one validated lifecycle receipt atomically."""

    value = validate_runtime_lifecycle_receipt(dict(receipt))
    output = Path(output_directory)
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeLifecycleStatusError(f"unable to create lifecycle receipt directory {output}: {exc}") from exc
    identifier = value["receipt_id"].removeprefix("sha256:")
    path = output / (f"runtime-{value['operation']}-receipt-{identifier}.json")
    _atomic_write(path, value)
    return path


def _validate_plan_binding(
    plan: dict[str, Any],
    status: dict[str, Any],
) -> None:
    for field in (
        "plan_id",
        "account_id",
        "partition",
        "region",
        "operation",
        "desired_state",
    ):
        if status[field] != plan[field]:
            raise RuntimeLifecycleStatusError(f"runtime lifecycle status {field} does not match the plan")


def _validate_runtime_observation(
    plan: dict[str, Any],
    stack_plan: dict[str, Any],
    observed: dict[str, Any],
    health: str,
) -> None:
    _validate_observed_stack(
        plan,
        stack_plan,
        observed,
        label="AgentCore runtime",
    )
    counts = (
        observed["runtime_count"],
        observed["ready_runtime_count"],
        observed["endpoint_count"],
        observed["ready_endpoint_count"],
    )
    if plan["desired_state"] == "parked":
        if counts != (0, 0, 0, 0):
            raise RuntimeLifecycleStatusError("parked runtime observation still contains runtime or endpoint resources")
        if health != "not-applicable":
            raise RuntimeLifecycleStatusError("parked runtime health must be not-applicable")
        return
    if (
        observed["runtime_count"] != 1
        or observed["ready_runtime_count"] != 1
        or observed["endpoint_count"] < 1
        or observed["ready_endpoint_count"] != observed["endpoint_count"]
    ):
        raise RuntimeLifecycleStatusError("resumed runtime and endpoints are not fully ready")
    if health != "passed":
        raise RuntimeLifecycleStatusError("resumed runtime health probe did not pass")


def _validate_network_observation(
    plan: dict[str, Any],
    stack_plan: dict[str, Any] | None,
    observed: dict[str, Any] | None,
) -> None:
    if plan["network_mode"] != "managed":
        if stack_plan is not None or observed is not None:
            raise RuntimeLifecycleStatusError("customer-owned network lifecycle cannot report a managed network stack")
        return
    if stack_plan is None or observed is None:
        raise RuntimeLifecycleStatusError("managed network lifecycle observation is missing")
    _validate_observed_stack(
        plan,
        stack_plan,
        observed,
        label="managed network",
    )
    counts = (
        observed["vpc_count"],
        observed["subnet_count"],
        observed["nat_gateway_count"],
        observed["vpc_endpoint_count"],
    )
    if plan["desired_state"] == "parked":
        if counts != (0, 0, 0, 0):
            raise RuntimeLifecycleStatusError("parked managed network observation still contains network resources")
        return
    if observed["vpc_count"] != 1 or observed["subnet_count"] < 2:
        raise RuntimeLifecycleStatusError("resumed managed network is not ready")


def _validate_observed_stack(
    plan: dict[str, Any],
    stack_plan: dict[str, Any],
    observed: dict[str, Any],
    *,
    label: str,
) -> None:
    if observed["stack_name"] != stack_plan["stack_name"]:
        raise RuntimeLifecycleStatusError(f"{label} stack name does not match the plan")
    desired_template = (
        stack_plan["active_template_sha256"]
        if plan["desired_state"] == "active"
        else stack_plan["parked_template_sha256"]
    )
    if observed["template_sha256"] != desired_template:
        raise RuntimeLifecycleStatusError(f"{label} template does not match the planned desired state")


def _validate_retained_stacks(
    expected: list[dict[str, Any]],
    observed: list[dict[str, Any]],
) -> None:
    expected_by_kind = {item["kind"]: item for item in expected}
    observed_by_kind = {item["kind"]: item for item in observed}
    if set(expected_by_kind) != set(observed_by_kind):
        raise RuntimeLifecycleStatusError("observed retained stack set does not match the plan")
    for kind, expected_stack in expected_by_kind.items():
        observed_stack = observed_by_kind[kind]
        if (
            observed_stack["stack_name"] != expected_stack["stack_name"]
            or observed_stack["stack_state_sha256"] != expected_stack["stack_state_sha256"]
        ):
            raise RuntimeLifecycleStatusError(f"retained {kind} stack changed during lifecycle operation")


def _validate_schema(
    value: object,
    schema: dict[str, Any],
    *,
    label: str,
) -> None:
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import best_match
    except ImportError as exc:  # pragma: no cover - clean install guard
        raise RuntimeLifecycleStatusError(
            "runtime lifecycle verification requires the 'deployment' package extra"
        ) from exc
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        error = best_match(errors)
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise RuntimeLifecycleStatusError(f"{label} {location}: {error.message}")


def _load_schema(name: str) -> dict[str, Any]:
    try:
        value = json.loads(files(_SCHEMA_PACKAGE).joinpath(name).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeLifecycleStatusError(f"unable to load packaged runtime lifecycle schema {name}") from exc
    if not isinstance(value, dict):
        raise RuntimeLifecycleStatusError(f"packaged runtime lifecycle schema {name} is invalid")
    return copy.deepcopy(value)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        if path.stat().st_size > _MAX_INPUT_BYTES:
            raise RuntimeLifecycleStatusError(f"{label} exceeds {_MAX_INPUT_BYTES} bytes")
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        RuntimeLifecycleStatusError,
    ) as exc:
        raise RuntimeLifecycleStatusError(f"unable to parse {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeLifecycleStatusError(f"{label} root must be an object")
    return value


def _unique_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RuntimeLifecycleStatusError(f"duplicate JSON field {key!r}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise RuntimeLifecycleStatusError(f"invalid JSON constant {value}")


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
        raise RuntimeLifecycleStatusError(f"unable to write lifecycle receipt {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


__all__ = [
    "RuntimeLifecycleStatusError",
    "build_runtime_lifecycle_receipt",
    "create_runtime_lifecycle_receipt",
    "load_runtime_lifecycle_receipt",
    "load_runtime_lifecycle_status",
    "runtime_lifecycle_receipt_schema",
    "runtime_lifecycle_status_schema",
    "validate_runtime_lifecycle_receipt",
    "validate_runtime_lifecycle_status",
    "write_runtime_lifecycle_receipt",
]
