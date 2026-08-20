"""Deterministic qualification and rollback planning for control-plane cutover."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import tempfile
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


_SCHEMA_PACKAGE = "src.gateway.deployment.schemas"
_CONTEXT_SCHEMA_NAME = "edge-transition-context-v1.schema.json"
_PLAN_SCHEMA_NAME = "edge-transition-plan-v1.schema.json"
_PLAN_SCHEMA = "urn:axonllm:edge-transition-plan:v1"
_VALIDATION_SCHEMA = "axonllm.production-validation/v1"
_MAX_INPUT_BYTES = 16 * 1024 * 1024
_SUPPLEMENTAL_CATEGORIES = frozenset(
    {
        "audit",
        "authentication",
        "budgets",
        "configuration",
        "exports",
        "scim",
        "workers",
    }
)
_ALLOWED_EDGE_CHANGES = {
    ("Add", "AWS::CloudFront::OriginAccessControl"),
    ("Modify", "AWS::CloudFront::Distribution"),
    ("Modify", "AWS::CloudFront::Function"),
}


class EdgeTransitionError(ValueError):
    """Raised when edge qualification or rollback evidence is unsafe."""


def edge_transition_context_schema() -> dict[str, Any]:
    """Return a fresh copy of the packaged input schema."""

    return _load_schema(_CONTEXT_SCHEMA_NAME)


def edge_transition_plan_schema() -> dict[str, Any]:
    """Return a fresh copy of the packaged output schema."""

    return _load_schema(_PLAN_SCHEMA_NAME)


def load_edge_transition_context(path: str | Path) -> dict[str, Any]:
    """Load one strict, non-secret transition context."""

    value, _ = _read_json(Path(path), "edge transition context")
    return validate_edge_transition_context(value)


def validate_edge_transition_context(value: object) -> dict[str, Any]:
    """Validate transition structure and cross-field safety rules."""

    if not isinstance(value, dict):
        raise EdgeTransitionError(
            "edge transition context root must be an object"
        )
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import best_match
    except ImportError as exc:  # pragma: no cover - clean install guard
        raise EdgeTransitionError(
            "edge transition planning requires the 'deployment' package extra"
        ) from exc

    schema = edge_transition_context_schema()
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        error = best_match(errors)
        location = ".".join(
            str(part) for part in error.absolute_path
        ) or "<root>"
        raise EdgeTransitionError(f"{location}: {error.message}")

    context = copy.deepcopy(value)
    operation = context["operation"]
    production = context["production"]
    serverless = context["serverless"]
    expected_backends = {
        "prepare": ("fargate", "fargate"),
        "cutover": ("fargate", "serverless"),
        "rollback": ("serverless", "fargate"),
    }[operation]
    if (
        production["current_backend"],
        production["desired_backend"],
    ) != expected_backends:
        raise EdgeTransitionError(
            f"{operation} has an illegal edge backend transition"
        )
    expected_distribution_arn = (
        f"arn:{production['partition']}:cloudfront::"
        f"{context['account_id']}:distribution/"
        f"{production['distribution_id']}"
    )
    if production["distribution_arn"] != expected_distribution_arn:
        raise EdgeTransitionError(
            "production distribution ARN does not match its account and ID"
        )
    if serverless["source_revision"] != context["source_revision"]:
        raise EdgeTransitionError(
            "serverless artifacts do not match the reviewed source revision"
        )
    if (
        production["state_table_name"]
        != serverless["state_table_name"]
    ):
        raise EdgeTransitionError(
            "legacy and serverless control planes do not share canonical state"
        )
    production_url = _https_url(
        f"https://{production['hostname']}",
        "production hostname",
    )
    qualification_url = _https_url(
        serverless["qualification_url"],
        "serverless qualification URL",
    )
    if production_url == qualification_url:
        raise EdgeTransitionError(
            "qualification and production endpoints must be distinct"
        )
    if serverless["production_hostname"] != production["hostname"]:
        raise EdgeTransitionError(
            "serverless callback binding does not preserve the production "
            "hostname"
        )
    _timestamp(
        context["rollback"]["not_before"],
        "rollback.not_before",
    )
    evidence_categories = {
        item["category"]
        for item in context["supplemental_evidence"]
    }
    if evidence_categories != _SUPPLEMENTAL_CATEGORIES:
        missing = sorted(_SUPPLEMENTAL_CATEGORIES - evidence_categories)
        extra = sorted(evidence_categories - _SUPPLEMENTAL_CATEGORIES)
        raise EdgeTransitionError(
            "supplemental evidence categories do not match: "
            f"missing={missing}, extra={extra}"
        )
    if any(
        _https_url(item["endpoint"], "supplemental evidence endpoint")
        != qualification_url
        for item in context["supplemental_evidence"]
    ):
        raise EdgeTransitionError(
            "supplemental evidence does not target the qualified serverless "
            "endpoint"
        )
    _validate_change_set(context)
    return context


def build_edge_transition_plan(
    context: Mapping[str, Any],
    legacy_report: Mapping[str, Any],
    serverless_report: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a non-mutating, hash-bound edge transition plan."""

    normalized = validate_edge_transition_context(dict(context))
    legacy = _validate_report(
        dict(legacy_report),
        expected_target="fargate",
        expected_endpoint=(
            f"https://{normalized['production']['hostname']}"
        ),
    )
    serverless = _validate_report(
        dict(serverless_report),
        expected_target="serverless-control",
        expected_endpoint=(
            normalized["serverless"]["qualification_url"]
        ),
    )
    parity = _validation_parity(legacy, serverless)
    body = {
        "schema": _PLAN_SCHEMA,
        "schema_version": 1,
        "operation": normalized["operation"],
        "mutating": False,
        "approval_required": True,
        "account_id": normalized["account_id"],
        "region": normalized["region"],
        "source_revision": normalized["source_revision"],
        "deployment_plan_id": normalized["deployment_plan_id"],
        "deployment_descriptor_id": (
            normalized["deployment_descriptor_id"]
        ),
        "production": normalized["production"],
        "serverless": normalized["serverless"],
        "change_set": normalized["change_set"],
        "qualification": {
            "legacy_report_sha256": _sha256_value(legacy),
            "serverless_report_sha256": _sha256_value(serverless),
            "legacy_overall_status": "PASS",
            "serverless_overall_status": "PASS",
            "parity": parity,
            "supplemental_evidence": sorted(
                normalized["supplemental_evidence"],
                key=lambda item: item["category"],
            ),
        },
        "rollback": normalized["rollback"],
        "gates": [
            {
                "name": "production_hostname_preserved",
                "passed": (
                    normalized["serverless"]["production_hostname"]
                    == normalized["production"]["hostname"]
                ),
            },
            {
                "name": "canonical_state_preserved",
                "passed": (
                    normalized["serverless"]["state_table_name"]
                    == normalized["production"]["state_table_name"]
                ),
            },
            {
                "name": "legacy_validation_passed",
                "passed": True,
            },
            {
                "name": "serverless_validation_passed",
                "passed": True,
            },
            {
                "name": "control_plane_outputs_match",
                "passed": parity["status"] == "PASS",
            },
            {
                "name": "supplemental_canaries_passed",
                "passed": all(
                    item["status"] == "PASS"
                    for item in normalized["supplemental_evidence"]
                ),
            },
            {
                "name": "change_set_is_edge_only",
                "passed": True,
            },
            {
                "name": "fargate_rollback_retained",
                "passed": normalized["rollback"]["retain_fargate"],
            },
        ],
    }
    plan = {
        "plan_id": _sha256_value(body),
        **body,
    }
    _validate_generated_plan(plan)
    return plan


def create_edge_transition_plan(
    *,
    context_path: str | Path,
    legacy_report_path: str | Path,
    serverless_report_path: str | Path,
    output_directory: str | Path,
) -> tuple[dict[str, Any], Path]:
    """Load and write one edge plan without AWS or subprocess access."""

    context = load_edge_transition_context(context_path)
    legacy, _ = _read_json(
        Path(legacy_report_path),
        "legacy validation report",
    )
    serverless, _ = _read_json(
        Path(serverless_report_path),
        "serverless validation report",
    )
    plan = build_edge_transition_plan(
        context,
        legacy,
        serverless,
    )
    path = write_edge_transition_plan(plan, output_directory)
    return plan, path


def write_edge_transition_plan(
    plan: Mapping[str, Any],
    output_directory: str | Path,
) -> Path:
    """Write one content-addressed edge plan atomically."""

    value = copy.deepcopy(dict(plan))
    _validate_generated_plan(value)
    output = Path(output_directory)
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise EdgeTransitionError(
            f"unable to create edge-plan directory {output}: {exc}"
        ) from exc
    identifier = value["plan_id"].removeprefix("sha256:")
    path = output / f"edge-transition-{identifier}.json"
    _atomic_write(path, value)
    return path


def _validate_change_set(context: dict[str, Any]) -> None:
    operation = context["operation"]
    changes = context["change_set"]["changes"]
    if not changes:
        raise EdgeTransitionError(
            "edge transition change set must contain reviewed changes"
        )
    for change in changes:
        pair = (change["action"], change["resource_type"])
        if pair not in _ALLOWED_EDGE_CHANGES:
            raise EdgeTransitionError(
                "edge transition change set contains a non-edge change"
            )
        if change["replacement"] != "False":
            raise EdgeTransitionError(
                "edge transition change set cannot replace resources"
            )
        if (
            change["action"] == "Add"
            and operation != "prepare"
        ):
            raise EdgeTransitionError(
                "only edge preparation may add origin access control"
            )
    if operation == "prepare":
        required = {
            ("Add", "AWS::CloudFront::OriginAccessControl"),
            ("Modify", "AWS::CloudFront::Distribution"),
            ("Modify", "AWS::CloudFront::Function"),
        }
        actual = {
            (item["action"], item["resource_type"])
            for item in changes
        }
        if not required.issubset(actual):
            raise EdgeTransitionError(
                "edge preparation lacks its distribution, function, or OAC "
                "change"
            )
    elif any(item["action"] != "Modify" for item in changes):
        raise EdgeTransitionError(
            "cutover and rollback may only modify edge resources"
        )


def _validate_report(
    report: dict[str, Any],
    *,
    expected_target: str,
    expected_endpoint: str,
) -> dict[str, Any]:
    if (
        report.get("schemaVersion") != _VALIDATION_SCHEMA
        or report.get("target") != expected_target
        or report.get("overallStatus") != "PASS"
        or report.get("launchGates", {}).get("status") != "PASS"
        or report.get("canaries", {}).get("status") != "PASS"
        or report.get("load", {}).get("status") != "PASS"
    ):
        raise EdgeTransitionError(
            f"{expected_target} validation report did not pass"
        )
    endpoints = report.get("httpEndpoints")
    normalized_endpoint = _https_url(
        expected_endpoint,
        f"{expected_target} endpoint",
    )
    if (
        not isinstance(endpoints, list)
        or len(endpoints) != 1
        or _https_url(
            endpoints[0],
            f"{expected_target} report endpoint",
        )
        != normalized_endpoint
    ):
        raise EdgeTransitionError(
            f"{expected_target} validation report targets the wrong endpoint"
        )
    results = report.get("canaries", {}).get("results")
    if (
        not isinstance(results, list)
        or not results
        or any(
            not isinstance(result, dict)
            or result.get("passed") is not True
            or result.get("baseUrl") != normalized_endpoint
            for result in results
        )
    ):
        raise EdgeTransitionError(
            f"{expected_target} canary results are incomplete"
        )
    _timestamp(report.get("startedAt"), f"{expected_target}.startedAt")
    _timestamp(report.get("finishedAt"), f"{expected_target}.finishedAt")
    return copy.deepcopy(report)


def _validation_parity(
    legacy: dict[str, Any],
    serverless: dict[str, Any],
) -> dict[str, Any]:
    legacy_results = _normalized_canary_results(legacy)
    serverless_results = _normalized_canary_results(serverless)
    if legacy_results != serverless_results:
        raise EdgeTransitionError(
            "legacy and serverless control-plane canary outcomes differ"
        )
    legacy_load = legacy["load"]
    serverless_load = serverless["load"]
    load_contract = {
        "method": legacy_load.get("method"),
        "path": legacy_load.get("path"),
        "expectedStatuses": legacy_load.get("expectedStatuses"),
        "requestCountConfigured": legacy_load.get(
            "requestCountConfigured"
        ),
        "concurrency": legacy_load.get("concurrency"),
        "thresholds": legacy_load.get("thresholds"),
    }
    serverless_contract = {
        "method": serverless_load.get("method"),
        "path": serverless_load.get("path"),
        "expectedStatuses": serverless_load.get("expectedStatuses"),
        "requestCountConfigured": serverless_load.get(
            "requestCountConfigured"
        ),
        "concurrency": serverless_load.get("concurrency"),
        "thresholds": serverless_load.get("thresholds"),
    }
    if load_contract != serverless_contract:
        raise EdgeTransitionError(
            "legacy and serverless load contracts differ"
        )
    return {
        "status": "PASS",
        "canary_outcomes": legacy_results,
        "load_contract": load_contract,
    }


def _normalized_canary_results(
    report: dict[str, Any],
) -> list[dict[str, Any]]:
    return sorted(
        (
            {
                "name": item.get("name"),
                "category": item.get("category"),
                "method": item.get("method"),
                "path": item.get("path"),
                "expected_statuses": item.get("expectedStatuses"),
                "status_code": item.get("statusCode"),
                "query_response_validated": item.get(
                    "queryResponseValidated"
                ),
                "error_code_validated": item.get(
                    "errorCodeValidated"
                ),
                "round_trip_passed": (
                    item.get("roundTrip", {}).get("status") == "PASS"
                    if isinstance(item.get("roundTrip"), dict)
                    else None
                ),
            }
            for item in report["canaries"]["results"]
        ),
        key=lambda item: (
            str(item["category"]),
            str(item["name"]),
            str(item["path"]),
        ),
    )


def _validate_generated_plan(plan: dict[str, Any]) -> None:
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import best_match
    except ImportError as exc:  # pragma: no cover - clean install guard
        raise EdgeTransitionError(
            "edge transition planning requires the 'deployment' package extra"
        ) from exc
    schema = edge_transition_plan_schema()
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema).iter_errors(plan))
    if errors:
        error = best_match(errors)
        location = ".".join(
            str(part) for part in error.absolute_path
        ) or "<root>"
        raise EdgeTransitionError(
            f"generated edge plan {location}: {error.message}"
        )
    expected = _sha256_value(
        {key: value for key, value in plan.items() if key != "plan_id"}
    )
    if plan["plan_id"] != expected:
        raise EdgeTransitionError(
            "edge transition plan hash does not match its content"
        )
    if plan["mutating"] is not False:
        raise EdgeTransitionError(
            "edge transition plan must be explicitly non-mutating"
        )
    if not all(gate["passed"] for gate in plan["gates"]):
        raise EdgeTransitionError(
            "edge transition plan contains a failed gate"
        )


def _read_json(
    path: Path,
    label: str,
) -> tuple[dict[str, Any], bytes]:
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size > _MAX_INPUT_BYTES
        ):
            raise EdgeTransitionError(f"{label} file is unsafe")
        raw = path.read_bytes()
        after = path.stat()
    except EdgeTransitionError:
        raise
    except OSError as exc:
        raise EdgeTransitionError(f"unable to read {label}") from exc
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or len(raw) != after.st_size
    ):
        raise EdgeTransitionError(f"{label} changed while reading")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EdgeTransitionError(
            f"{label} is not strict UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise EdgeTransitionError(f"{label} must be a JSON object")
    return value, raw


def _load_schema(name: str) -> dict[str, Any]:
    return json.loads(
        files(_SCHEMA_PACKAGE)
        .joinpath(name)
        .read_text(encoding="utf-8")
    )


def _https_url(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise EdgeTransitionError(f"{label} is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise EdgeTransitionError(f"{label} is invalid")
    return f"https://{parsed.hostname}"


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise EdgeTransitionError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EdgeTransitionError(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EdgeTransitionError(f"{label} is invalid")
    return parsed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise EdgeTransitionError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise EdgeTransitionError(f"invalid JSON constant: {value}")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _sha256_value(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(value)
    ).hexdigest()


def _atomic_write(path: Path, value: object) -> None:
    payload = (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )
    descriptor: int | None = None
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise EdgeTransitionError(
            f"unable to write edge transition plan {path}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


__all__ = [
    "EdgeTransitionError",
    "build_edge_transition_plan",
    "create_edge_transition_plan",
    "edge_transition_context_schema",
    "edge_transition_plan_schema",
    "load_edge_transition_context",
    "validate_edge_transition_context",
    "write_edge_transition_plan",
]
