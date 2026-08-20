"""Deterministic, non-mutating deployment planning for AxonLLM."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from collections import Counter
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.gateway.deployment.config_contract import (
    DeploymentConfigError,
    load_deployment_config,
    validate_deployment_config,
)

_MAX_CONTEXT_BYTES = 1024 * 1024
_SCHEMA_PACKAGE = "src.gateway.deployment.schemas"
_CONTEXT_SCHEMA_NAME = "deployment-plan-context-v1.schema.json"
_DESCRIPTOR_SCHEMA_NAME = "deployment-descriptor-v1.schema.json"
_PLAN_SCHEMA_NAME = "deployment-plan-v1.schema.json"
_PLAN_SCHEMA = "urn:axonllm:deployment-plan:v1"
_DESCRIPTOR_SCHEMA = "urn:axonllm:deployment-descriptor:v1"
_PRIVATE_ENDPOINT_SERVICES = frozenset({"s3", "dynamodb"})
_COST_CLASSES = (
    "fixed-monthly",
    "hourly",
    "storage",
    "request-based",
)


class DeploymentPlanError(ValueError):
    """Raised when a deterministic deployment plan cannot be produced."""


def deployment_plan_context_schema() -> dict[str, Any]:
    """Return a fresh copy of the packaged planning-context schema."""

    return _load_packaged_schema(_CONTEXT_SCHEMA_NAME)


def deployment_descriptor_schema() -> dict[str, Any]:
    """Return a fresh copy of the packaged deployment-descriptor schema."""

    return _load_packaged_schema(_DESCRIPTOR_SCHEMA_NAME)


def deployment_plan_schema() -> dict[str, Any]:
    """Return a fresh copy of the packaged deployment-plan schema."""

    return _load_packaged_schema(_PLAN_SCHEMA_NAME)


def load_deployment_plan_context(path: str | Path) -> dict[str, Any]:
    """Load and validate one strict JSON planning context."""

    context_path = Path(path)
    try:
        size = context_path.stat().st_size
    except OSError as exc:
        raise DeploymentPlanError(f"unable to read deployment planning context {context_path}: {exc}") from exc
    if size > _MAX_CONTEXT_BYTES:
        raise DeploymentPlanError(f"deployment planning context exceeds {_MAX_CONTEXT_BYTES} bytes")
    try:
        raw = context_path.read_text(encoding="utf-8")
        value = json.loads(
            raw,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, DeploymentPlanError) as exc:
        raise DeploymentPlanError(f"unable to parse deployment planning context {context_path}: {exc}") from exc
    return validate_deployment_plan_context(value)


def validate_deployment_plan_context(value: object) -> dict[str, Any]:
    """Validate explicit planning evidence without performing discovery."""

    if not isinstance(value, dict):
        raise DeploymentPlanError("deployment planning context root must be an object")

    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import best_match
    except ImportError as exc:  # pragma: no cover - exercised in clean installs
        raise DeploymentPlanError("deployment planning requires the 'deployment' package extra") from exc

    schema = deployment_plan_context_schema()
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        error = best_match(errors)
        path = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise DeploymentPlanError(f"{path}: {error.message}")

    _validate_context_semantics(value)
    return copy.deepcopy(value)


def build_deployment_plan(
    config: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a content-addressed plan from validated, explicit inputs."""

    try:
        normalized_config = validate_deployment_config(dict(config))
    except DeploymentConfigError as exc:
        raise DeploymentPlanError(str(exc)) from exc
    normalized_context = validate_deployment_plan_context(dict(context))

    descriptor = _build_descriptor(normalized_config, normalized_context)
    inventory = _build_inventory(normalized_config)
    changes = _normalized_changes(normalized_context["stacks"])
    cost_summary = _build_cost_summary(inventory)
    summary = _build_summary(inventory, changes)

    body = {
        "schema": _PLAN_SCHEMA,
        "schema_version": 1,
        "operation": "plan",
        "mutating": False,
        "target": normalized_config["target"],
        "deployment_profile": normalized_config["deployment_profile"],
        "account_id": normalized_context["account_id"],
        "region": normalized_config["region"],
        "desired_runtime_state": normalized_config["runtime"]["state"],
        "inputs": {
            "configuration_sha256": _sha256_value(normalized_config),
            "source": normalized_context["source"],
            "images": [
                {"name": name, "reference": reference}
                for name, reference in sorted(normalized_context["images"].items())
            ],
            "stacks": [
                {
                    "name": stack["name"],
                    "template_sha256": stack["template_sha256"],
                    "stack_state_sha256": stack["stack_state_sha256"],
                }
                for stack in sorted(normalized_context["stacks"], key=lambda item: item["name"])
            ],
            "descriptor_sha256": descriptor["descriptor_id"],
        },
        "descriptor": descriptor,
        "inventory": inventory,
        "changes": changes,
        "cost_summary": cost_summary,
        "summary": summary,
    }
    plan = {"plan_id": _sha256_value(body), **body}
    _validate_generated_plan(plan)
    return plan


def write_deployment_plan(
    plan: Mapping[str, Any],
    output_directory: str | Path,
) -> tuple[Path, Path]:
    """Write descriptor and plan artifacts atomically under content-addressed names."""

    normalized = copy.deepcopy(dict(plan))
    _validate_generated_plan(normalized)
    output = Path(output_directory)
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DeploymentPlanError(f"unable to create plan output directory {output}: {exc}") from exc

    plan_hex = normalized["plan_id"].removeprefix("sha256:")
    descriptor_hex = normalized["descriptor"]["descriptor_id"].removeprefix("sha256:")
    descriptor_path = output / f"descriptor-{descriptor_hex}.json"
    plan_path = output / f"plan-{plan_hex}.json"
    _atomic_write_json(descriptor_path, normalized["descriptor"])
    _atomic_write_json(plan_path, normalized)
    return plan_path, descriptor_path


def create_deployment_plan(
    *,
    config_path: str | Path,
    context_path: str | Path,
    output_directory: str | Path,
) -> tuple[dict[str, Any], Path, Path]:
    """Load, build, and write one plan without AWS or subprocess access."""

    config = load_deployment_config(config_path)
    context = load_deployment_plan_context(context_path)
    plan = build_deployment_plan(config, context)
    plan_path, descriptor_path = write_deployment_plan(plan, output_directory)
    return plan, plan_path, descriptor_path


def _build_descriptor(
    config: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    descriptor_body = {
        "schema": _DESCRIPTOR_SCHEMA,
        "schema_version": 1,
        "resolution": context["descriptor"]["resolution"],
        "target": config["target"],
        "account_id": context["account_id"],
        "region": config["region"],
        "source_revision": context["source"]["revision"],
        "network": copy.deepcopy(config["network"]),
        "images": [{"name": name, "reference": reference} for name, reference in sorted(context["images"].items())],
        "resources": sorted(
            copy.deepcopy(context["descriptor"]["resources"]),
            key=lambda item: (item["name"], item["resource_type"], item["identifier"]),
        ),
        "release_evidence_ids": sorted(context["descriptor"]["release_evidence_ids"]),
    }
    return {
        "descriptor_id": _sha256_value(descriptor_body),
        **descriptor_body,
    }


def _build_inventory(config: dict[str, Any]) -> list[dict[str, Any]]:
    active = config["runtime"]["state"] == "active"
    runtime_desired = "present" if active else "absent"
    entries: list[dict[str, Any]] = [
        _inventory(
            "application-state",
            "durable-application-state",
            "axonllm",
            "retained",
            "present",
            ("storage", "request-based"),
            "Canonical configuration, identity, usage, budget, session, and audit state.",
        ),
        _inventory(
            "application-encryption",
            "kms-keys",
            "axonllm",
            "retained",
            "present",
            ("request-based",),
            "Data encryption and signed-routing keys remain available while parked.",
        ),
        _inventory(
            "provider-secret-metadata",
            "secrets-manager-secret",
            "axonllm",
            "retained",
            "present",
            ("storage", "request-based"),
            "AxonLLM owns only secret metadata and runtime retrieval policy.",
        ),
        _inventory(
            "event-delivery",
            "queues-topics-and-logs",
            "axonllm",
            "retained",
            "present",
            ("storage", "request-based"),
            "Durable asynchronous delivery and audit history.",
        ),
        _inventory(
            "agentcore-runtime",
            "bedrock-agentcore-runtime",
            "axonllm",
            "runtime",
            runtime_desired,
            ("request-based",),
            "AgentCore data plane exists only while the runtime is active.",
        ),
        _inventory(
            "agentcore-runtime-iam",
            "iam-runtime-role",
            "axonllm",
            "runtime",
            runtime_desired,
            (),
            "Least-privilege runtime role follows the runtime lifecycle.",
        ),
        _inventory(
            "control-plane-ui",
            "cloudfront-and-private-s3",
            "axonllm",
            "retained",
            "present",
            ("storage", "request-based"),
            "Static browser UI remains available without Fargate or an ALB.",
        ),
        _inventory(
            "control-plane-api",
            "lambda-control-api-and-workers",
            "axonllm",
            "retained",
            "present",
            ("request-based",),
            "Request-driven administration and asynchronous workers.",
        ),
        _inventory(
            "customer-boundary",
            "web-application-firewall",
            "axonllm",
            "retained",
            "present",
            ("fixed-monthly", "request-based"),
            "Authenticated CloudFront boundary and request filtering.",
        ),
        _inventory(
            "observability",
            "logs-alarms-and-dashboard",
            "axonllm",
            "retained",
            "present",
            ("storage", "request-based"),
            "Operational telemetry and alarms.",
        ),
    ]

    identity = config["identity"]
    if identity["mode"] == "managed-cognito":
        entries.append(
            _inventory(
                "identity",
                "cognito-user-pool-and-clients",
                "axonllm",
                "retained",
                "present",
                ("request-based",),
                "AxonLLM-managed browser and runtime identity.",
            )
        )
    else:
        entries.append(
            _inventory(
                "identity",
                "oidc-provider-and-client",
                "customer",
                "imported",
                "referenced",
                (),
                "Existing enterprise identity remains customer-owned.",
            )
        )

    hostname = config["control_plane"]["hostname"]
    if hostname["mode"] == "custom":
        entries.extend(
            [
                _inventory(
                    "tls-certificate",
                    "acm-certificate",
                    "customer",
                    "imported",
                    "referenced",
                    (),
                    "The supplied certificate is never adopted or deleted.",
                ),
                _inventory(
                    "dns-zone",
                    "route53-hosted-zone",
                    "customer",
                    "imported",
                    "referenced",
                    (),
                    "The supplied hosted zone remains customer-owned.",
                ),
            ]
        )

    network = config["network"]
    if network["mode"] == "existing":
        entries.extend(_existing_network_inventory(network, runtime_desired))
    elif network["mode"] == "managed":
        entries.extend(_managed_network_inventory(network, runtime_desired))

    return sorted(entries, key=lambda item: (item["ownership"], item["name"], item["kind"]))


def _existing_network_inventory(
    network: dict[str, Any],
    runtime_desired: str,
) -> list[dict[str, Any]]:
    entries = [
        _inventory(
            "runtime-vpc",
            "vpc",
            "customer",
            "imported",
            "referenced",
            (),
            "AxonLLM reads the supplied VPC identifier and does not manage it.",
        ),
        _inventory(
            "runtime-private-subnets",
            "private-subnets",
            "customer",
            "imported",
            "referenced",
            (),
            "AxonLLM validates but does not modify supplied private subnets.",
            quantity=len(network["private_subnet_ids"]),
        ),
    ]
    security_groups = network["security_group_ids"]
    if security_groups:
        entries.append(
            _inventory(
                "runtime-security-groups",
                "security-groups",
                "customer",
                "imported",
                "referenced",
                (),
                "Supplied security groups remain customer-owned.",
                quantity=len(security_groups),
            )
        )
    else:
        entries.append(
            _inventory(
                "runtime-security-group",
                "security-group",
                "axonllm",
                "runtime",
                runtime_desired,
                (),
                "A dedicated runtime security group is created without changing customer groups.",
            )
        )

    egress = network["egress"]
    if egress["mode"] == "existing-egress":
        entries.append(
            _inventory(
                "runtime-egress",
                "customer-egress-path",
                "customer",
                "imported",
                "referenced",
                (),
                "Existing NAT, firewall, proxy, or centralized egress remains customer-owned.",
            )
        )
    else:
        services = egress.get("services", [])
        entries.append(
            _inventory(
                "runtime-private-connectivity",
                "customer-vpc-endpoint-connectivity",
                "customer",
                "imported",
                "referenced",
                (),
                "Required endpoints are validated in the existing VPC, not created by AxonLLM.",
                quantity=len(services),
            )
        )
    return entries


def _managed_network_inventory(
    network: dict[str, Any],
    runtime_desired: str,
) -> list[dict[str, Any]]:
    zone_count = len(network["availability_zone_ids"])
    entries = [
        _inventory(
            "runtime-vpc",
            "vpc",
            "axonllm",
            "runtime",
            runtime_desired,
            (),
            "Managed networking is isolated in the disposable runtime lifecycle.",
        ),
        _inventory(
            "runtime-private-subnets",
            "private-subnets",
            "axonllm",
            "runtime",
            runtime_desired,
            (),
            "One private runtime subnet is planned per selected Availability Zone ID.",
            quantity=zone_count,
        ),
        _inventory(
            "runtime-security-group",
            "security-group",
            "axonllm",
            "runtime",
            runtime_desired,
            (),
            "Dedicated runtime security group.",
        ),
    ]
    egress = network["egress"]
    if egress["mode"] == "managed-nat":
        nat_count = egress["nat_gateway_count"]
        entries.extend(
            [
                _inventory(
                    "runtime-public-subnets",
                    "public-subnets",
                    "axonllm",
                    "runtime",
                    runtime_desired,
                    (),
                    "Public subnets exist only to host explicitly approved managed NAT.",
                    quantity=nat_count,
                ),
                _inventory(
                    "runtime-internet-gateway",
                    "internet-gateway",
                    "axonllm",
                    "runtime",
                    runtime_desired,
                    (),
                    "Internet gateway exists only for explicitly approved managed NAT.",
                ),
                _inventory(
                    "runtime-nat-gateways",
                    "nat-gateways",
                    "axonllm",
                    "runtime",
                    runtime_desired,
                    ("hourly", "request-based"),
                    "Explicitly acknowledged internet egress for external providers.",
                    quantity=nat_count,
                ),
            ]
        )
    else:
        services = sorted(egress.get("services", []))
        interface_services = [service for service in services if service not in _PRIVATE_ENDPOINT_SERVICES]
        gateway_services = [service for service in services if service in _PRIVATE_ENDPOINT_SERVICES]
        if interface_services:
            entries.append(
                _inventory(
                    "runtime-interface-endpoints",
                    "interface-vpc-endpoints",
                    "axonllm",
                    "runtime",
                    runtime_desired,
                    ("hourly", "request-based"),
                    "Private connectivity for AWS services without managed NAT.",
                    quantity=len(interface_services),
                )
            )
        if gateway_services:
            entries.append(
                _inventory(
                    "runtime-gateway-endpoints",
                    "gateway-vpc-endpoints",
                    "axonllm",
                    "runtime",
                    runtime_desired,
                    (),
                    "Gateway endpoints for private S3 or DynamoDB connectivity.",
                    quantity=len(gateway_services),
                )
            )
    return entries


def _inventory(
    name: str,
    kind: str,
    ownership: str,
    lifecycle: str,
    desired: str,
    cost_classes: Iterable[str],
    reason: str,
    *,
    quantity: int = 1,
) -> dict[str, Any]:
    costs = sorted(set(cost_classes), key=_COST_CLASSES.index)
    return {
        "name": name,
        "kind": kind,
        "ownership": ownership,
        "lifecycle": lifecycle,
        "desired": desired,
        "quantity": quantity,
        "cost_classes": costs,
        "reason": reason,
    }


def _normalized_changes(stacks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for stack in stacks:
        for change in stack["changes"]:
            changes.append({"stack": stack["name"], **copy.deepcopy(change)})
    return sorted(changes, key=lambda item: (item["stack"], item["logical_id"]))


def _build_cost_summary(inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "cost_class": cost_class,
            "resources": [
                item["name"] for item in inventory if cost_class in item["cost_classes"] and item["desired"] != "absent"
            ],
        }
        for cost_class in _COST_CLASSES
    ]


def _build_summary(
    inventory: list[dict[str, Any]],
    changes: list[dict[str, Any]],
) -> dict[str, Any]:
    ownership = Counter(item["ownership"] for item in inventory)
    actions = Counter(item["action"] for item in changes)
    replacement_count = sum(item["replacement"] in {"True", "Conditional"} for item in changes)
    chargeable_networking = any(
        item["name"] in {"runtime-nat-gateways", "runtime-interface-endpoints"} and item["desired"] == "present"
        for item in inventory
    )
    return {
        "resource_counts_by_ownership": dict(sorted(ownership.items())),
        "change_counts_by_action": {action: actions.get(action, 0) for action in ("Add", "Import", "Modify", "Remove")},
        "replacement_review_required": replacement_count > 0,
        "replacement_count": replacement_count,
        "chargeable_networking": chargeable_networking,
    }


def _validate_context_semantics(context: dict[str, Any]) -> None:
    if "agentcore" not in context["images"]:
        raise DeploymentPlanError("images.agentcore: an immutable AgentCore image is required")

    stack_names: set[str] = set()
    for stack in context["stacks"]:
        name = stack["name"]
        if name in stack_names:
            raise DeploymentPlanError(f"stacks: duplicate stack name {name!r}")
        stack_names.add(name)
        logical_ids: set[str] = set()
        for change in stack["changes"]:
            logical_id = change["logical_id"]
            if logical_id in logical_ids:
                raise DeploymentPlanError(f"stacks.{name}.changes: duplicate logical ID {logical_id!r}")
            logical_ids.add(logical_id)
            if change["action"] != "Modify" and change["replacement"] != "NotApplicable":
                raise DeploymentPlanError(
                    f"stacks.{name}.changes.{logical_id}: replacement must be NotApplicable for {change['action']}"
                )

    binding_names: set[str] = set()
    for binding in context["descriptor"]["resources"]:
        name = binding["name"]
        if name in binding_names:
            raise DeploymentPlanError(f"descriptor.resources: duplicate binding name {name!r}")
        binding_names.add(name)
        identifier = binding["identifier"]
        if "?" in identifier or "#" in identifier:
            raise DeploymentPlanError(
                f"descriptor.resources.{name}.identifier: query strings and fragments are not allowed"
            )
        if "://" in identifier and "@" in identifier.partition("://")[2].partition("/")[0]:
            raise DeploymentPlanError(f"descriptor.resources.{name}.identifier: URI user information is not allowed")


def _validate_generated_plan(plan: dict[str, Any]) -> None:
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import best_match
    except ImportError as exc:  # pragma: no cover - exercised in clean installs
        raise DeploymentPlanError("deployment planning requires the 'deployment' package extra") from exc

    descriptor_validator = Draft202012Validator(deployment_descriptor_schema())
    descriptor_errors = list(descriptor_validator.iter_errors(plan.get("descriptor")))
    if descriptor_errors:
        error = best_match(descriptor_errors)
        path = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise DeploymentPlanError(f"generated descriptor {path}: {error.message}")

    plan_validator = Draft202012Validator(deployment_plan_schema())
    plan_errors = list(plan_validator.iter_errors(plan))
    if plan_errors:
        error = best_match(plan_errors)
        path = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise DeploymentPlanError(f"generated plan {path}: {error.message}")

    plan_id = plan.get("plan_id")
    descriptor = plan.get("descriptor")
    if not isinstance(plan_id, str) or not isinstance(descriptor, dict):
        raise DeploymentPlanError("generated deployment plan has an invalid structure")
    expected_descriptor_id = _sha256_value({key: value for key, value in descriptor.items() if key != "descriptor_id"})
    if descriptor.get("descriptor_id") != expected_descriptor_id:
        raise DeploymentPlanError("deployment descriptor hash does not match its content")
    expected_plan_id = _sha256_value({key: value for key, value in plan.items() if key != "plan_id"})
    if plan_id != expected_plan_id:
        raise DeploymentPlanError("deployment plan hash does not match its content")
    if plan.get("operation") != "plan" or plan.get("mutating") is not False:
        raise DeploymentPlanError("deployment plan must be explicitly non-mutating")


def _load_packaged_schema(name: str) -> dict[str, Any]:
    resource = files(_SCHEMA_PACKAGE).joinpath(name)
    return json.loads(resource.read_text(encoding="utf-8"))


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DeploymentPlanError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise DeploymentPlanError(f"invalid JSON constant: {value}")


def _sha256_value(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value)).hexdigest()}"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _atomic_write_json(path: Path, value: object) -> None:
    payload = (
        json.dumps(
            value,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )
    descriptor: int | None = None
    temporary_path: str | None = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        raise DeploymentPlanError(f"unable to write deployment artifact {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
