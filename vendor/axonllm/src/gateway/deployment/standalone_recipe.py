"""Deterministic, non-mutating standalone ECS deployment planning."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from src.gateway.config import VALID_PROVIDERS

_SCHEMA_PACKAGE = "src.gateway.deployment.schemas"
_CONTEXT_SCHEMA_NAME = "standalone-ecs-context-v1.schema.json"
_PLAN_SCHEMA_NAME = "standalone-ecs-plan-v1.schema.json"
_PLAN_SCHEMA = "urn:axonllm:standalone-ecs-plan:v1"
_MAX_INPUT_BYTES = 1024 * 1024
_CONTAINER_NAME = "axonllm"
_CONTAINER_PORT = 8000
_SAFE_SECRET_NAME = re.compile(
    r"^(?:[A-Z][A-Z0-9_]*(?:API_KEY|PASSWORD|SECRET|TOKEN)"
    r"|AXON_SCIM_TENANTS)$"
)
_ECR_IMAGE = re.compile(
    r"^(?P<account>[0-9]{12})\.dkr\.ecr\."
    r"(?P<region>[a-z0-9-]+)\."
    r"(?P<dns>amazonaws\.com(?:\.cn)?)/"
    r"(?P<repository>[a-z0-9]+(?:[._/-][a-z0-9]+)*)"
    r"@sha256:(?P<digest>[0-9a-f]{64})$"
)
_CPU_MEMORY = {
    256: frozenset({512, 1024, 2048}),
    512: frozenset({1024, 2048, 3072, 4096}),
    1024: frozenset(range(2048, 8193, 1024)),
    2048: frozenset(range(4096, 16385, 1024)),
    4096: frozenset(range(8192, 30721, 1024)),
    8192: frozenset(range(16384, 61441, 4096)),
    16384: frozenset(range(32768, 122881, 8192)),
}
_RESERVED_SECRET_NAMES = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_DEFAULT_REGION",
        "AWS_REGION",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AXON_AUTH_MODE",
        "AXON_CONTROL_PLANE_ONLY",
        "AXON_DEPLOYMENT_PROFILE",
        "AXON_DYNAMODB_TABLE",
        "AXON_LOAD_DEMO_DATA",
        "AXON_OIDC_AUDIENCE",
        "AXON_OIDC_ISSUER",
        "AXON_REQUIRE_CANONICAL_IDENTITY",
        "AXON_ROUTING_CONFIG_SIGNING_KEY_ARN",
        "AXON_ROUTING_CONFIG_SIGNING_MODE",
        "AXON_SERVER_HOST",
        "AXON_SERVER_PORT",
        "LLM_ROUTER_DYNAMODB_ENABLED",
    }
)


class StandaloneRecipeError(ValueError):
    """Raised when a standalone deployment plan is unsafe or incomplete."""


def standalone_ecs_context_schema() -> dict[str, Any]:
    """Return a fresh copy of the packaged standalone input schema."""

    return _load_schema(_CONTEXT_SCHEMA_NAME)


def standalone_ecs_plan_schema() -> dict[str, Any]:
    """Return a fresh copy of the packaged standalone plan schema."""

    return _load_schema(_PLAN_SCHEMA_NAME)


def load_standalone_ecs_context(path: str | Path) -> dict[str, Any]:
    """Load and validate one strict non-secret ECS planning context."""

    context_path = Path(path)
    try:
        size = context_path.stat().st_size
    except OSError as exc:
        raise StandaloneRecipeError(f"unable to read standalone ECS context {context_path}: {exc}") from exc
    if size > _MAX_INPUT_BYTES:
        raise StandaloneRecipeError(f"standalone ECS context exceeds {_MAX_INPUT_BYTES} bytes")
    try:
        value = json.loads(
            context_path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        StandaloneRecipeError,
    ) as exc:
        raise StandaloneRecipeError(f"unable to parse standalone ECS context {context_path}: {exc}") from exc
    return validate_standalone_ecs_context(value)


def validate_standalone_ecs_context(value: object) -> dict[str, Any]:
    """Validate one existing-infrastructure ECS deployment context."""

    if not isinstance(value, dict):
        raise StandaloneRecipeError("standalone ECS context root must be an object")
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import best_match
    except ImportError as exc:  # pragma: no cover - clean install guard
        raise StandaloneRecipeError("standalone planning requires the 'deployment' package extra") from exc

    schema = standalone_ecs_context_schema()
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        error = best_match(errors)
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise StandaloneRecipeError(f"{location}: {error.message}")

    context = copy.deepcopy(value)
    account_id = context["account_id"]
    partition = context["partition"]
    region = context["region"]
    _validate_image(
        context["image_reference"],
        account_id=account_id,
        partition=partition,
        region=region,
    )
    task = context["task"]
    valid_memory = _CPU_MEMORY[task["cpu"]]
    if task["memory_mib"] not in valid_memory:
        raise StandaloneRecipeError(f"task.memory_mib is invalid for cpu={task['cpu']}")
    if task["execution_role_arn"] == task["task_role_arn"]:
        raise StandaloneRecipeError("task execution and application roles must be distinct")
    for name in ("execution_role_arn", "task_role_arn"):
        _validate_arn(
            task[name],
            service="iam",
            account_id=account_id,
            partition=partition,
            region=None,
            field=f"task.{name}",
        )
    _validate_log_group_arn(
        task["log_group_arn"],
        account_id=account_id,
        partition=partition,
        region=region,
    )

    network = context["network"]
    _validate_arn(
        network["cluster_arn"],
        service="ecs",
        account_id=account_id,
        partition=partition,
        region=region,
        field="network.cluster_arn",
    )
    _validate_arn(
        network["target_group_arn"],
        service="elasticloadbalancing",
        account_id=account_id,
        partition=partition,
        region=region,
        field="network.target_group_arn",
    )
    state = context["state"]
    _validate_arn(
        state["data_key_arn"],
        service="kms",
        account_id=account_id,
        partition=partition,
        region=region,
        field="state.data_key_arn",
    )
    _validate_arn(
        state["routing_signing_key_arn"],
        service="kms",
        account_id=account_id,
        partition=partition,
        region=region,
        field="state.routing_signing_key_arn",
    )
    if state["data_key_arn"] == state["routing_signing_key_arn"]:
        raise StandaloneRecipeError("data encryption and routing-signing keys must be distinct")

    issuer = context["identity"]["issuer"]
    parsed_issuer = urlsplit(issuer)
    if (
        parsed_issuer.scheme != "https"
        or not parsed_issuer.hostname
        or parsed_issuer.username is not None
        or parsed_issuer.password is not None
        or parsed_issuer.fragment
    ):
        raise StandaloneRecipeError("identity.issuer must be an absolute HTTPS URL")

    providers = set(context["providers"])
    unknown = sorted(providers.difference(VALID_PROVIDERS))
    if unknown:
        raise StandaloneRecipeError("providers contains unknown values: " + ", ".join(unknown))
    names: set[str] = set()
    for index, secret in enumerate(context["provider_secrets"]):
        name = secret["name"]
        if name in _RESERVED_SECRET_NAMES or _SAFE_SECRET_NAME.fullmatch(name) is None:
            raise StandaloneRecipeError(f"provider_secrets.{index}.name is not an allowed secret environment variable")
        if name in names:
            raise StandaloneRecipeError("provider secret environment names must be unique")
        names.add(name)
        _validate_secret_reference(
            secret["value_from"],
            account_id=account_id,
            partition=partition,
            region=region,
            field=f"provider_secrets.{index}.value_from",
        )
    return context


def build_standalone_ecs_plan(
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Build hardened task and service specifications without AWS calls."""

    normalized = validate_standalone_ecs_context(dict(context))
    task = normalized["task"]
    network = normalized["network"]
    state = normalized["state"]
    identity = normalized["identity"]
    environment = [
        {"name": name, "value": value}
        for name, value in sorted(
            {
                "AWS_DEFAULT_REGION": normalized["region"],
                "AXON_AUTH_MODE": "ENFORCE",
                "AXON_BEDROCK_REGION": normalized["region"],
                "AXON_DEPLOYMENT_PROFILE": "production",
                "AXON_DYNAMODB_TABLE": state["table_name"],
                "AXON_ENABLED_PROVIDERS": ",".join(sorted(normalized["providers"])),
                "AXON_LOAD_DEMO_DATA": "false",
                "AXON_NO_BROWSER": "true",
                "AXON_OIDC_AUDIENCE": identity["audience"],
                "AXON_OIDC_ISSUER": identity["issuer"],
                "AXON_OIDC_PROJECT_CLAIM": identity["project_claim"],
                "AXON_OIDC_TENANT_CLAIM": identity["tenant_claim"],
                "AXON_REQUIRE_CANONICAL_IDENTITY": "true",
                "AXON_ROUTING_CONFIG_SIGNING_KEY_ARN": (state["routing_signing_key_arn"]),
                "AXON_ROUTING_CONFIG_SIGNING_MODE": "sign-verify",
                "AXON_SERVER_HOST": "0.0.0.0",
                "AXON_SERVER_PORT": str(_CONTAINER_PORT),
                "LLM_ROUTER_DYNAMODB_ENABLED": "true",
            }.items()
        )
    ]
    secrets = [
        {
            "name": item["name"],
            "valueFrom": item["value_from"],
        }
        for item in sorted(
            normalized["provider_secrets"],
            key=lambda item: item["name"],
        )
    ]
    architecture = {
        "linux/amd64": "X86_64",
        "linux/arm64": "ARM64",
    }[normalized["platform"]]
    task_definition = {
        "family": task["family"],
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": str(task["cpu"]),
        "memory": str(task["memory_mib"]),
        "executionRoleArn": task["execution_role_arn"],
        "taskRoleArn": task["task_role_arn"],
        "runtimePlatform": {
            "cpuArchitecture": architecture,
            "operatingSystemFamily": "LINUX",
        },
        "containerDefinitions": [
            {
                "name": _CONTAINER_NAME,
                "image": normalized["image_reference"],
                "essential": True,
                "user": "10001:10001",
                "readonlyRootFilesystem": True,
                "stopTimeout": 45,
                "portMappings": [
                    {
                        "containerPort": _CONTAINER_PORT,
                        "protocol": "tcp",
                    }
                ],
                "environment": environment,
                "secrets": secrets,
                "linuxParameters": {
                    "initProcessEnabled": True,
                    "capabilities": {"drop": ["ALL"]},
                },
                "healthCheck": {
                    "command": [
                        "CMD",
                        "python",
                        "-c",
                        ("import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3)"),
                    ],
                    "interval": 30,
                    "timeout": 5,
                    "retries": 3,
                    "startPeriod": 30,
                },
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": task["log_group_name"],
                        "awslogs-region": normalized["region"],
                        "awslogs-stream-prefix": "standalone",
                        "mode": "blocking",
                    },
                },
            }
        ],
    }
    service = {
        "cluster": network["cluster_arn"],
        "serviceName": task["service_name"],
        "desiredCount": task["desired_count"],
        "launchType": "FARGATE",
        "platformVersion": "LATEST",
        "taskDefinitionBinding": {
            "family": task["family"],
            "taskDefinitionSha256": _sha256_value(task_definition),
        },
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": sorted(network["subnet_ids"]),
                "securityGroups": sorted(network["security_group_ids"]),
                "assignPublicIp": "DISABLED",
            }
        },
        "loadBalancers": [
            {
                "targetGroupArn": network["target_group_arn"],
                "containerName": _CONTAINER_NAME,
                "containerPort": _CONTAINER_PORT,
            }
        ],
        "healthCheckGracePeriodSeconds": 60,
        "deploymentConfiguration": {
            "minimumHealthyPercent": 100,
            "maximumPercent": 200,
            "deploymentCircuitBreaker": {
                "enable": True,
                "rollback": True,
            },
        },
        "enableExecuteCommand": False,
    }
    body = {
        "schema": _PLAN_SCHEMA,
        "schema_version": 1,
        "operation": "standalone-ecs-plan",
        "mutating": False,
        "approval_required": True,
        "account_id": normalized["account_id"],
        "partition": normalized["partition"],
        "region": normalized["region"],
        "source_revision": normalized["source_revision"],
        "release_evidence_ids": sorted(normalized["release_evidence_ids"]),
        "image_reference": normalized["image_reference"],
        "platform": normalized["platform"],
        "ownership": {
            "customer": [
                "ecs-cluster",
                "private-subnets",
                "task-security-groups",
                "target-group-and-ingress",
                "dynamodb-table",
                "kms-keys",
                "iam-roles",
                "log-group",
            ],
            "axonllm": ["task-definition", "ecs-service"],
            "created_network_resources": [],
        },
        "preflight_requirements": [
            "target group uses target type ip and health check path /ready",
            "task security groups allow port 8000 only from customer ingress",
            "private subnets provide approved provider and AWS service egress",
            "execution role can pull the image, write logs, and fetch secret ARNs",
            "task role can access only the declared state, keys, and providers",
        ],
        "task_definition": task_definition,
        "service": service,
    }
    plan = {"plan_id": _sha256_value(body), **body}
    _validate_generated_plan(plan)
    return plan


def write_standalone_ecs_plan(
    plan: Mapping[str, Any],
    output_directory: str | Path,
) -> tuple[Path, Path]:
    """Write the plan and register-task-definition input atomically."""

    normalized = copy.deepcopy(dict(plan))
    _validate_generated_plan(normalized)
    output = Path(output_directory)
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StandaloneRecipeError(f"unable to create standalone plan directory {output}: {exc}") from exc
    plan_hex = normalized["plan_id"].removeprefix("sha256:")
    task_hex = normalized["service"]["taskDefinitionBinding"]["taskDefinitionSha256"].removeprefix("sha256:")
    plan_path = output / f"standalone-ecs-plan-{plan_hex}.json"
    task_path = output / f"task-definition-{task_hex}.json"
    _atomic_write_json(plan_path, normalized)
    _atomic_write_json(task_path, normalized["task_definition"])
    return plan_path, task_path


def create_standalone_ecs_plan(
    *,
    context_path: str | Path,
    output_directory: str | Path,
) -> tuple[dict[str, Any], Path, Path]:
    """Load, build, and write one standalone plan without AWS access."""

    context = load_standalone_ecs_context(context_path)
    plan = build_standalone_ecs_plan(context)
    plan_path, task_path = write_standalone_ecs_plan(
        plan,
        output_directory,
    )
    return plan, plan_path, task_path


def _validate_generated_plan(plan: dict[str, Any]) -> None:
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import best_match
    except ImportError as exc:  # pragma: no cover - clean install guard
        raise StandaloneRecipeError("standalone planning requires the 'deployment' package extra") from exc
    schema = standalone_ecs_plan_schema()
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema).iter_errors(plan))
    if errors:
        error = best_match(errors)
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise StandaloneRecipeError(f"generated standalone plan {location}: {error.message}")
    body = {key: value for key, value in plan.items() if key != "plan_id"}
    if plan["plan_id"] != _sha256_value(body):
        raise StandaloneRecipeError("standalone plan_id does not match plan content")
    _validate_plan_semantics(plan)


def _validate_plan_semantics(plan: dict[str, Any]) -> None:
    if plan["ownership"]["created_network_resources"]:
        raise StandaloneRecipeError("standalone plan cannot create network resources")

    task = plan["task_definition"]
    if (
        task.get("networkMode") != "awsvpc"
        or task.get("requiresCompatibilities") != ["FARGATE"]
        or task.get("executionRoleArn") == task.get("taskRoleArn")
    ):
        raise StandaloneRecipeError("standalone task violates the Fargate or IAM contract")
    try:
        cpu = int(task["cpu"])
        memory = int(task["memory"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StandaloneRecipeError("standalone task CPU or memory is invalid") from exc
    if cpu not in _CPU_MEMORY or memory not in _CPU_MEMORY[cpu]:
        raise StandaloneRecipeError("standalone task has an invalid CPU/memory pair")
    expected_architecture = {
        "linux/amd64": "X86_64",
        "linux/arm64": "ARM64",
    }[plan["platform"]]
    if task.get("runtimePlatform") != {
        "cpuArchitecture": expected_architecture,
        "operatingSystemFamily": "LINUX",
    }:
        raise StandaloneRecipeError("standalone task platform does not match the reviewed image")

    containers = task.get("containerDefinitions")
    if not isinstance(containers, list) or len(containers) != 1:
        raise StandaloneRecipeError("standalone task must contain exactly one container")
    container = containers[0]
    if (
        container.get("name") != _CONTAINER_NAME
        or container.get("image") != plan["image_reference"]
        or container.get("user") != "10001:10001"
        or container.get("readonlyRootFilesystem") is not True
        or container.get("stopTimeout") != 45
        or container.get("linuxParameters")
        != {
            "initProcessEnabled": True,
            "capabilities": {"drop": ["ALL"]},
        }
        or container.get("portMappings") != [{"containerPort": _CONTAINER_PORT, "protocol": "tcp"}]
    ):
        raise StandaloneRecipeError("standalone container violates the hardened runtime contract")

    environment = container.get("environment")
    if not isinstance(environment, list):
        raise StandaloneRecipeError("standalone container environment is invalid")
    if any(
        not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("value"), str)
        for item in environment
    ):
        raise StandaloneRecipeError("standalone container environment is invalid")
    environment_map = {item["name"]: item["value"] for item in environment}
    if len(environment_map) != len(environment):
        raise StandaloneRecipeError("standalone container environment names must be unique")
    required_environment = {
        "AXON_AUTH_MODE": "ENFORCE",
        "AXON_DEPLOYMENT_PROFILE": "production",
        "AXON_LOAD_DEMO_DATA": "false",
        "AXON_REQUIRE_CANONICAL_IDENTITY": "true",
        "AXON_ROUTING_CONFIG_SIGNING_MODE": "sign-verify",
        "AXON_SERVER_HOST": "0.0.0.0",
        "AXON_SERVER_PORT": str(_CONTAINER_PORT),
        "LLM_ROUTER_DYNAMODB_ENABLED": "true",
    }
    if any(environment_map.get(name) != value for name, value in required_environment.items()):
        raise StandaloneRecipeError("standalone container environment weakens production safety")

    secret_names: set[str] = set()
    for index, secret in enumerate(container.get("secrets", [])):
        if not isinstance(secret, dict):
            raise StandaloneRecipeError("standalone task contains an invalid secret reference")
        name = secret.get("name")
        value_from = secret.get("valueFrom")
        if (
            not isinstance(name, str)
            or name in secret_names
            or name in environment_map
            or name in _RESERVED_SECRET_NAMES
            or _SAFE_SECRET_NAME.fullmatch(name) is None
            or not isinstance(value_from, str)
        ):
            raise StandaloneRecipeError("standalone task contains an unsafe secret binding")
        secret_names.add(name)
        _validate_secret_reference(
            value_from,
            account_id=plan["account_id"],
            partition=plan["partition"],
            region=plan["region"],
            field=f"task_definition.secrets.{index}.valueFrom",
        )
    log_configuration = container.get("logConfiguration", {})
    if (
        log_configuration.get("logDriver") != "awslogs"
        or log_configuration.get("options", {}).get("mode") != "blocking"
    ):
        raise StandaloneRecipeError("standalone task must use blocking awslogs delivery")

    service = plan["service"]
    network = service.get("networkConfiguration", {}).get(
        "awsvpcConfiguration",
        {},
    )
    deployment = service.get("deploymentConfiguration", {})
    if (
        service.get("launchType") != "FARGATE"
        or service.get("platformVersion") != "LATEST"
        or service.get("desiredCount", 0) < 2
        or network.get("assignPublicIp") != "DISABLED"
        or len(network.get("subnets", [])) < 2
        or not network.get("securityGroups")
        or service.get("healthCheckGracePeriodSeconds") != 60
        or deployment.get("minimumHealthyPercent") != 100
        or deployment.get("maximumPercent") != 200
        or deployment.get("deploymentCircuitBreaker") != {"enable": True, "rollback": True}
        or service.get("enableExecuteCommand") is not False
    ):
        raise StandaloneRecipeError("standalone service violates the production deployment contract")
    load_balancers = service.get("loadBalancers")
    if (
        not isinstance(load_balancers, list)
        or len(load_balancers) != 1
        or load_balancers[0].get("containerName") != _CONTAINER_NAME
        or load_balancers[0].get("containerPort") != _CONTAINER_PORT
        or not load_balancers[0].get("targetGroupArn")
    ):
        raise StandaloneRecipeError("standalone service load-balancer binding is invalid")
    if service.get("taskDefinitionBinding", {}).get("taskDefinitionSha256") != _sha256_value(task):
        raise StandaloneRecipeError("standalone service is not bound to the reviewed task definition")
    for name in ("executionRoleArn", "taskRoleArn"):
        _validate_arn(
            task[name],
            service="iam",
            account_id=plan["account_id"],
            partition=plan["partition"],
            region=None,
            field=f"task_definition.{name}",
        )
    _validate_arn(
        service["cluster"],
        service="ecs",
        account_id=plan["account_id"],
        partition=plan["partition"],
        region=plan["region"],
        field="service.cluster",
    )
    _validate_arn(
        load_balancers[0]["targetGroupArn"],
        service="elasticloadbalancing",
        account_id=plan["account_id"],
        partition=plan["partition"],
        region=plan["region"],
        field="service.loadBalancers.0.targetGroupArn",
    )
    _validate_image(
        plan["image_reference"],
        account_id=plan["account_id"],
        partition=plan["partition"],
        region=plan["region"],
    )


def _validate_image(
    value: str,
    *,
    account_id: str,
    partition: str,
    region: str,
) -> None:
    match = _ECR_IMAGE.fullmatch(value)
    expected_dns = "amazonaws.com.cn" if partition == "aws-cn" else "amazonaws.com"
    if (
        match is None
        or match.group("account") != account_id
        or match.group("region") != region
        or match.group("dns") != expected_dns
    ):
        raise StandaloneRecipeError("image_reference must be an exact same-account, same-region private ECR digest")


def _validate_arn(
    value: str,
    *,
    service: str,
    account_id: str,
    partition: str,
    region: str | None,
    field: str,
) -> None:
    parts = value.split(":", 5)
    if (
        len(parts) != 6
        or parts[0] != "arn"
        or parts[1] != partition
        or parts[2] != service
        or parts[4] != account_id
        or (region is not None and parts[3] != region)
        or not parts[5]
    ):
        raise StandaloneRecipeError(f"{field} does not match the deployment account, partition, region, or service")


def _validate_log_group_arn(
    value: str,
    *,
    account_id: str,
    partition: str,
    region: str,
) -> None:
    _validate_arn(
        value,
        service="logs",
        account_id=account_id,
        partition=partition,
        region=region,
        field="task.log_group_arn",
    )
    if ":log-group:" not in value:
        raise StandaloneRecipeError("task.log_group_arn must identify a CloudWatch Logs log group")


def _validate_secret_reference(
    value: str,
    *,
    account_id: str,
    partition: str,
    region: str,
    field: str,
) -> None:
    parts = value.split(":", 5)
    if (
        len(parts) != 6
        or parts[0] != "arn"
        or parts[1] != partition
        or parts[2] not in {"secretsmanager", "ssm"}
        or parts[3] != region
        or parts[4] != account_id
        or not parts[5]
    ):
        raise StandaloneRecipeError(f"{field} must be a same-account, same-region Secrets Manager or SSM parameter ARN")


def _load_schema(name: str) -> dict[str, Any]:
    resource = files(_SCHEMA_PACKAGE).joinpath(name)
    return json.loads(resource.read_text(encoding="utf-8"))


def _sha256_value(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StandaloneRecipeError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise StandaloneRecipeError(f"invalid JSON numeric constant {value}")


def _atomic_write_json(path: Path, value: object) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
    except OSError as exc:
        raise StandaloneRecipeError(f"unable to write standalone plan artifact {path}: {exc}") from exc
