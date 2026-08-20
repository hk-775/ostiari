"""Strict, non-mutating validation for AxonLLM deployment configuration."""

from __future__ import annotations

import copy
import json
from importlib.resources import files
from ipaddress import AddressValueError, NetmaskValueError, ip_network
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

_MAX_CONFIG_BYTES = 1024 * 1024
_SCHEMA_PACKAGE = "src.gateway.deployment.schemas"
_SCHEMA_NAME = "deployment-v1.schema.json"
_SUPPORTED_AZS_PACKAGE = "src.gateway.deployment.infra"
_SUPPORTED_AZS_NAME = "agentcore-supported-availability-zones-v1.json"
_PRIVATE_AWS_PROVIDERS = frozenset({"bedrock"})
_MANAGED_ENDPOINT_SERVICES = frozenset(
    {
        "bedrock-runtime",
        "cognito-idp",
        "dynamodb",
        "ecr.api",
        "ecr.dkr",
        "kms",
        "logs",
        "s3",
        "secretsmanager",
        "sns",
        "sqs",
    }
)


class DeploymentConfigError(ValueError):
    """Raised when a deployment configuration is unsafe or invalid."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def deployment_config_schema() -> dict[str, Any]:
    """Return a fresh copy of the packaged deployment schema."""

    resource = files(_SCHEMA_PACKAGE).joinpath(_SCHEMA_NAME)
    return json.loads(resource.read_text(encoding="utf-8"))


def load_deployment_config(path: str | Path) -> dict[str, Any]:
    """Load and validate one deployment YAML document without side effects."""

    config_path = Path(path)
    try:
        size = config_path.stat().st_size
    except OSError as exc:
        raise DeploymentConfigError(f"unable to read deployment configuration {config_path}: {exc}") from exc
    if size > _MAX_CONFIG_BYTES:
        raise DeploymentConfigError(f"deployment configuration exceeds {_MAX_CONFIG_BYTES} bytes")
    try:
        raw = config_path.read_text(encoding="utf-8")
        value = yaml.load(raw, Loader=_UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise DeploymentConfigError(f"unable to parse deployment configuration {config_path}: {exc}") from exc
    return validate_deployment_config(value)


def validate_deployment_config(value: object) -> dict[str, Any]:
    """Validate one in-memory deployment configuration and return a copy."""

    if not isinstance(value, dict):
        raise DeploymentConfigError("deployment configuration root must be a mapping")

    try:
        from jsonschema import Draft202012Validator, FormatChecker
        from jsonschema.exceptions import best_match
    except ImportError as exc:  # pragma: no cover - exercised in clean installs
        raise DeploymentConfigError("deployment validation requires the 'deployment' package extra") from exc

    schema = deployment_config_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
    )
    errors = list(validator.iter_errors(value))
    if errors:
        error = best_match(errors)
        path = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise DeploymentConfigError(f"{path}: {error.message}")

    _validate_semantics(value)
    return copy.deepcopy(value)


def required_endpoint_services(
    config: dict[str, Any],
) -> tuple[str, ...]:
    """Return the private AWS services required by one valid config."""

    services = set(_MANAGED_ENDPOINT_SERVICES)
    if config["identity"]["mode"] != "managed-cognito":
        services.discard("cognito-idp")
    return tuple(sorted(services))


def _validate_semantics(config: dict[str, Any]) -> None:
    profile = config["deployment_profile"]
    network = config["network"]
    runtime = config["runtime"]

    if network["mode"] == "public" and profile != "development":
        raise DeploymentConfigError("network.mode: public networking is development-only")

    if network["mode"] == "managed":
        try:
            managed_network = ip_network(network["vpc_cidr"], strict=True)
        except (AddressValueError, NetmaskValueError, ValueError) as exc:
            raise DeploymentConfigError("network.vpc_cidr: must be a canonical IPv4 CIDR") from exc
        if managed_network.version != 4:
            raise DeploymentConfigError("network.vpc_cidr: must be a canonical IPv4 CIDR")
        supported_document = json.loads(
            files(_SUPPORTED_AZS_PACKAGE).joinpath(_SUPPORTED_AZS_NAME).read_text(encoding="utf-8")
        )
        supported = supported_document["regions"].get(config["region"])
        if not isinstance(supported, list):
            raise DeploymentConfigError("region: AgentCore VPC networking is not supported")
        unsupported = sorted(set(network["availability_zone_ids"]).difference(supported))
        if unsupported:
            raise DeploymentConfigError(
                f"network.availability_zone_ids: unsupported AgentCore Availability Zone IDs: {', '.join(unsupported)}"
            )

    egress = network.get("egress")
    if egress and egress["mode"] == "endpoints-only":
        if config["identity"]["mode"] == "existing-oidc":
            raise DeploymentConfigError(
                "identity.mode: endpoints-only requires managed-cognito "
                "until an explicit private OIDC reachability contract is "
                "configured"
            )
        external = sorted(set(runtime["providers"]).difference(_PRIVATE_AWS_PROVIDERS))
        if external:
            names = ", ".join(external)
            raise DeploymentConfigError(f"runtime.providers: endpoints-only cannot reach external providers: {names}")
        if network["mode"] == "managed":
            services = set(egress.get("services", []))
            missing = sorted(set(required_endpoint_services(config)).difference(services))
            if missing:
                raise DeploymentConfigError(
                    "network.egress.services: managed endpoints-only is "
                    f"missing required services: {', '.join(missing)}"
                )
