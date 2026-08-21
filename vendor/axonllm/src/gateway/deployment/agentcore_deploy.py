#!/usr/bin/env python3
"""Deploy an authenticated first-adopter AgentCore configuration."""

from __future__ import annotations

import argparse
import asyncio
import base64
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import ipaddress
from importlib.resources import files
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import unquote, urlsplit
import zlib

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

from src.gateway.agentcore_setup import (
    CLOUDFRONT,
    CUSTOM_DOMAIN,
    DEFAULT_PROJECT_CLAIM,
    DEFAULT_TENANT_CLAIM,
    EXTERNAL_OIDC,
    MANAGED_COGNITO,
    AgentCoreSetupConfig,
    AgentCoreSetupError,
    load_agentcore_setup,
    redact_sensitive,
)
from src.gateway.deployment.bootstrap_policy import (
    EXECUTION_POLICY_PART_COUNT,
    bootstrap_boundary_arn as bootstrap_role_boundary_arn,
    bootstrap_boundary_document as bootstrap_role_boundary_document,
    bootstrap_boundary_name as bootstrap_role_boundary_name,
    boundary_arn as service_boundary_arn,
    boundary_document as service_boundary_document,
    boundary_name as service_boundary_name,
    policy_documents as bootstrap_policy_documents,
    policy_part_arn as bootstrap_policy_part_arn,
    policy_part_name as bootstrap_policy_part_name,
    qualifier_for_namespace as bootstrap_qualifier_for_namespace,
    toolkit_stack_name as bootstrap_toolkit_stack_name,
)
from src.gateway.deployment.provider_secret import (
    ALLOWED_SECRET_FIELDS,
    ProviderSecretError,
    ProviderSecretVersion,
    collect_provider_secret,
    load_provider_environment_file,
    rollback_provider_secret,
    synchronize_provider_secret,
)

if TYPE_CHECKING:
    from src.gateway.deployment.network_preflight import (
        NetworkPreflightResult,
    )


class AgentCoreDeploymentError(RuntimeError):
    """Raised when deployment cannot prove a safe resulting configuration."""


_INFRA_RESOURCE_NAMES = (
    "application-state-migration-v1.json",
    "application_state.py",
    "application_state_stack.py",
    "agentcore-supported-availability-zones-v1.json",
    "agentcore_stack.py",
    "app.py",
    "cdk.json",
    "control_plane_stack.py",
    "identity_stack.py",
    "managed_network_stack.py",
    "parked_stack.py",
    "package-lock.json",
    "package.json",
    "requirements.txt",
    "runtime_network.py",
    "serverless_control_plane_stack.py",
    "serverless_workers_stack.py",
    "static_asset_deployer.py",
)


def _infra_resources() -> dict[str, bytes]:
    resource_root = files("src.gateway.deployment.infra")
    resources: dict[str, bytes] = {}
    for name in _INFRA_RESOURCE_NAMES:
        resource = resource_root.joinpath(name)
        if not resource.is_file():
            raise AgentCoreDeploymentError(f"installed AgentCore infrastructure is missing {name}")
        resources[name] = resource.read_bytes()
    return resources


def _infra_resource_digest(
    resources: Mapping[str, bytes] | None = None,
) -> str:
    digest = hashlib.sha256()
    for name, content in sorted((resources or _infra_resources()).items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()


def _infra_cache_base() -> Path:
    cache_home = Path(
        os.environ.get(
            "XDG_CACHE_HOME",
            Path.home() / ".cache",
        )
    ).expanduser()
    return cache_home / "axonllm" / "agentcore-infra"


_INFRA_DIGEST = _infra_resource_digest()
_INFRA_CACHE_BASE = _infra_cache_base()
INFRA_ROOT = _INFRA_CACHE_BASE / "artifacts" / _INFRA_DIGEST
INFRA_TOOLS_ROOT = (
    _INFRA_CACHE_BASE
    / "tools"
    / _INFRA_DIGEST
    / (f"{sys.platform}-{platform.machine().lower()}-py{sys.version_info.major}{sys.version_info.minor}")
)
INFRA_RUN_ROOT = _INFRA_CACHE_BASE / "runs"


@contextmanager
def _infra_lock():
    lock_root = INFRA_ROOT.parent
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = lock_root / f".{INFRA_ROOT.name}.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise AgentCoreDeploymentError("could not lock the AgentCore infrastructure cache") from exc
    with os.fdopen(descriptor, "r+b", closefd=True) as lock_file:
        try:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            elif msvcrt is not None:
                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            else:  # pragma: no cover - supported platforms have one
                raise OSError("no supported file lock")
        except OSError as exc:
            raise AgentCoreDeploymentError("could not lock the AgentCore infrastructure cache") from exc
        yield


def _materialization_is_valid(
    resources: Mapping[str, bytes],
    digest: str,
) -> bool:
    marker = INFRA_ROOT / ".complete"
    try:
        if (
            INFRA_ROOT.is_symlink()
            or not INFRA_ROOT.is_dir()
            or marker.is_symlink()
            or marker.read_text(encoding="ascii").strip() != digest
        ):
            return False
        for name, expected in resources.items():
            target = INFRA_ROOT / name
            if target.is_symlink() or not target.is_file():
                return False
            if target.read_bytes() != expected:
                return False
    except (OSError, UnicodeError):
        return False
    return True


def _tool_tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    try:
        paths = sorted(
            (path for path in root.rglob("*") if path.name != ".complete"),
            key=lambda path: path.relative_to(root).as_posix(),
        )
        for path in paths:
            relative = path.relative_to(root).as_posix().encode("utf-8")
            if path.is_symlink():
                digest.update(b"L\0")
                digest.update(relative)
                digest.update(b"\0")
                digest.update(os.readlink(path).encode("utf-8"))
            elif path.is_file():
                digest.update(b"F\0")
                digest.update(relative)
                digest.update(b"\0")
                with path.open("rb") as source:
                    for chunk in iter(
                        lambda: source.read(1024 * 1024),
                        b"",
                    ):
                        digest.update(chunk)
            elif path.is_dir():
                digest.update(b"D\0")
                digest.update(relative)
            else:
                raise OSError("unsupported CDK tool cache entry")
            digest.update(b"\0")
    except (OSError, UnicodeError) as exc:
        raise AgentCoreDeploymentError("could not verify the AgentCore CDK tool cache") from exc
    return digest.hexdigest()


def _valid_tool_cache_marker(
    marker: Path,
    *,
    python: Path,
    cdk: Path,
    infra_digest: str,
) -> bool:
    try:
        if (
            INFRA_TOOLS_ROOT.is_symlink()
            or not INFRA_TOOLS_ROOT.is_dir()
            or marker.is_symlink()
            or not marker.is_file()
            or not python.is_file()
            or not cdk.is_file()
        ):
            return False
        value = json.loads(marker.read_text(encoding="ascii"))
        if (
            not isinstance(value, dict)
            or set(value) != {"infraDigest", "toolsDigest"}
            or value.get("infraDigest") != infra_digest
            or not isinstance(value.get("toolsDigest"), str)
        ):
            return False
        return secrets.compare_digest(
            value["toolsDigest"],
            _tool_tree_digest(INFRA_TOOLS_ROOT),
        )
    except (
        AgentCoreDeploymentError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ):
        return False


def _materialize_infra() -> None:
    """Atomically copy and verify immutable CDK files from the installed wheel."""
    resources = _infra_resources()
    digest = _infra_resource_digest(resources)
    if _materialization_is_valid(resources, digest):
        return
    with _infra_lock():
        if _materialization_is_valid(resources, digest):
            return
        temporary = Path(
            tempfile.mkdtemp(
                prefix=f".{INFRA_ROOT.name}.",
                dir=INFRA_ROOT.parent,
            )
        )
        temporary.chmod(0o700)
        try:
            for name, content in resources.items():
                target = temporary / name
                target.write_bytes(content)
                target.chmod(0o600)
            marker = temporary / ".complete"
            marker.write_text(digest + "\n", encoding="ascii")
            marker.chmod(0o600)
            if INFRA_ROOT.is_symlink():
                raise AgentCoreDeploymentError("AgentCore infrastructure cache must not be a symlink")
            if INFRA_ROOT.exists():
                if not INFRA_ROOT.is_dir():
                    raise AgentCoreDeploymentError("AgentCore infrastructure cache is not a directory")
                shutil.rmtree(INFRA_ROOT)
            temporary.replace(INFRA_ROOT)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def _ensure_cdk_environment() -> None:
    """Install the hash-pinned CDK environment once per resource digest."""
    _materialize_infra()
    marker = INFRA_TOOLS_ROOT / ".complete"
    venv = INFRA_TOOLS_ROOT / ".venv"
    scripts_dir = venv / ("Scripts" if os.name == "nt" else "bin")
    python = scripts_dir / ("python.exe" if os.name == "nt" else "python")
    cdk = _cdk_cli_path()
    digest = _infra_resource_digest()
    with _infra_lock():
        if _valid_tool_cache_marker(
            marker,
            python=python,
            cdk=cdk,
            infra_digest=digest,
        ):
            return
        uv = shutil.which("uv")
        if uv is None:
            raise AgentCoreDeploymentError("uv is required to install the hash-pinned AgentCore CDK environment")
        if INFRA_TOOLS_ROOT.is_symlink():
            raise AgentCoreDeploymentError("AgentCore CDK tools directory must not be a symlink")
        if INFRA_TOOLS_ROOT.exists():
            shutil.rmtree(INFRA_TOOLS_ROOT)
        INFRA_TOOLS_ROOT.mkdir(mode=0o700, parents=True)
        for name in ("package.json", "package-lock.json"):
            shutil.copy2(INFRA_ROOT / name, INFRA_TOOLS_ROOT / name)
        try:
            subprocess.run(
                [uv, "venv", "--python", "3.12", str(venv)],
                check=True,
            )
            subprocess.run(
                [
                    uv,
                    "pip",
                    "sync",
                    "--python",
                    str(python),
                    "--require-hashes",
                    str(INFRA_ROOT / "requirements.txt"),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "npm",
                    "ci",
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                ],
                cwd=INFRA_TOOLS_ROOT,
                check=True,
            )
            if not python.is_file():
                raise OSError("uv did not create the CDK Python interpreter")
            if not cdk.is_file():
                raise OSError("npm did not install the pinned CDK CLI")
            marker.write_text(
                json.dumps(
                    {
                        "infraDigest": digest,
                        "toolsDigest": _tool_tree_digest(INFRA_TOOLS_ROOT),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n",
                encoding="ascii",
            )
            marker.chmod(0o600)
        except (OSError, subprocess.CalledProcessError) as exc:
            shutil.rmtree(INFRA_TOOLS_ROOT, ignore_errors=True)
            raise AgentCoreDeploymentError("could not install the hash-pinned AgentCore CDK tools") from exc


def _cdk_cli_path() -> Path:
    return INFRA_TOOLS_ROOT / "node_modules" / ".bin" / ("cdk.cmd" if os.name == "nt" else "cdk")


_PRODUCTION_IDENTITY_STACK = "AxonLLMIdentityStack"
_PRODUCTION_AGENTCORE_STACK = "AxonLLMAgentCoreStack"
_PRODUCTION_CONTROL_PLANE_STACK = "AxonLLMControlPlaneStack"
_PRODUCTION_SERVERLESS_CONTROL_PLANE_STACK = (
    "AxonLLMServerlessControlPlaneStack"
)
_PRODUCTION_SERVERLESS_WORKERS_STACK = "AxonLLMServerlessWorkersStack"
_PRODUCTION_APPLICATION_STATE_STACK = "AxonLLMApplicationStateStack"
_PRODUCTION_MANAGED_NETWORK_STACK = "AxonLLMManagedNetworkStack"
_PRODUCTION_STATE_TABLE_NAME = "axonllm-agentcore-state"
_PRODUCTION_USER_POOL_NAME = "axonllm-agentcore-users"
IDENTITY_STACK = _PRODUCTION_IDENTITY_STACK
AGENTCORE_STACK = _PRODUCTION_AGENTCORE_STACK
CONTROL_PLANE_STACK = _PRODUCTION_CONTROL_PLANE_STACK
SERVERLESS_CONTROL_PLANE_STACK = (
    _PRODUCTION_SERVERLESS_CONTROL_PLANE_STACK
)
SERVERLESS_WORKERS_STACK = _PRODUCTION_SERVERLESS_WORKERS_STACK
APPLICATION_STATE_STACK = _PRODUCTION_APPLICATION_STATE_STACK
MANAGED_NETWORK_STACK = _PRODUCTION_MANAGED_NETWORK_STACK
CDK_BOOTSTRAP_STACK = bootstrap_toolkit_stack_name(bootstrap_qualifier_for_namespace(""))
_PRIMARY_STATE_TABLE_NAME = _PRODUCTION_STATE_TABLE_NAME
_MANAGED_USER_POOL_NAME = _PRODUCTION_USER_POOL_NAME
_ACTIVE_DEPLOYMENT_NAMESPACE = ""
_DEPLOYMENT_NAMESPACE_PATTERN = re.compile(r"^[a-z](?:[a-z0-9-]{0,14}[a-z0-9])?$")
_REHEARSAL_CONTROL_TABLE_ARN_PATTERN = re.compile(
    r"^arn:aws:dynamodb:(?P<region>[a-z0-9-]+):"
    r"(?P<account>[0-9]{12}):table/"
    r"axonllm-rehearsal-control-ledger$"
)
_DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_AGENTCORE_ENVIRONMENT_VALUE_CHARACTERS = 2_048
_CANDIDATE_ENDPOINT_PATTERN = re.compile(r"^candidate_[0-9a-f]{32}$")
_TRANSITION_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TRANSITION_RUN_PATTERN = re.compile(r"^[1-9][0-9]*$")
_TRANSITION_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_TRANSITION_CHANGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}$")
_TRANSITION_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_UNBOUND_DEPLOYMENT_TRANSITION_ID = "unbound"
_LIFECYCLE_CHANGE_SET_NAME_PATTERN = re.compile(
    r"^[A-Za-z][A-Za-z0-9-]{0,127}$"
)
CommandRunner = Callable[[list[str], Path], None]


@dataclass(frozen=True)
class DeploymentNames:
    namespace: str
    identity_stack: str
    agentcore_stack: str
    control_plane_stack: str
    serverless_control_plane_stack: str
    serverless_workers_stack: str
    application_state_stack: str
    managed_network_stack: str
    state_table: str
    user_pool: str


def deployment_names(namespace: str | None = None) -> DeploymentNames:
    """Return collision-free names for one production or qualification set."""
    normalized = "" if namespace is None else namespace
    if not isinstance(normalized, str):
        raise AgentCoreDeploymentError("deployment namespace must be a string")
    if normalized and _DEPLOYMENT_NAMESPACE_PATTERN.fullmatch(normalized) is None:
        raise AgentCoreDeploymentError(
            "deployment namespace must be 1-16 lowercase letters, digits, "
            "or internal hyphens, start with a letter, and end with a letter "
            "or digit"
        )
    suffix = f"-{normalized}" if normalized else ""
    return DeploymentNames(
        namespace=normalized,
        identity_stack=f"{_PRODUCTION_IDENTITY_STACK}{suffix}",
        agentcore_stack=f"{_PRODUCTION_AGENTCORE_STACK}{suffix}",
        control_plane_stack=(f"{_PRODUCTION_CONTROL_PLANE_STACK}{suffix}"),
        serverless_control_plane_stack=(
            f"{_PRODUCTION_SERVERLESS_CONTROL_PLANE_STACK}{suffix}"
        ),
        serverless_workers_stack=(
            f"{_PRODUCTION_SERVERLESS_WORKERS_STACK}{suffix}"
        ),
        application_state_stack=(
            f"{_PRODUCTION_APPLICATION_STATE_STACK}{suffix}"
        ),
        managed_network_stack=(
            f"{_PRODUCTION_MANAGED_NETWORK_STACK}{suffix}"
        ),
        state_table=f"{_PRODUCTION_STATE_TABLE_NAME}{suffix}",
        user_pool=f"{_PRODUCTION_USER_POOL_NAME}{suffix}",
    )


def _current_deployment_names() -> DeploymentNames:
    return deployment_names(_ACTIVE_DEPLOYMENT_NAMESPACE)


def _activate_deployment_namespace(
    namespace: str | None,
) -> DeploymentNames:
    global AGENTCORE_STACK
    global APPLICATION_STATE_STACK
    global CONTROL_PLANE_STACK
    global IDENTITY_STACK
    global MANAGED_NETWORK_STACK
    global SERVERLESS_CONTROL_PLANE_STACK
    global SERVERLESS_WORKERS_STACK
    global _ACTIVE_DEPLOYMENT_NAMESPACE
    global _MANAGED_USER_POOL_NAME
    global _PRIMARY_STATE_TABLE_NAME

    previous = _current_deployment_names()
    selected = deployment_names(namespace)
    _ACTIVE_DEPLOYMENT_NAMESPACE = selected.namespace
    IDENTITY_STACK = selected.identity_stack
    AGENTCORE_STACK = selected.agentcore_stack
    APPLICATION_STATE_STACK = selected.application_state_stack
    MANAGED_NETWORK_STACK = selected.managed_network_stack
    CONTROL_PLANE_STACK = selected.control_plane_stack
    SERVERLESS_CONTROL_PLANE_STACK = (
        selected.serverless_control_plane_stack
    )
    SERVERLESS_WORKERS_STACK = selected.serverless_workers_stack
    _PRIMARY_STATE_TABLE_NAME = selected.state_table
    _MANAGED_USER_POOL_NAME = selected.user_pool
    return previous


def _deployment_context_arguments(namespace: str) -> list[str]:
    qualifier = bootstrap_qualifier_for_namespace(namespace)
    arguments = ["-c", f"cdk_qualifier={qualifier}"]
    if namespace:
        arguments.extend(["-c", f"deployment_namespace={namespace}"])
    return arguments


def _append_verified_network_context(
    command: list[str],
    context: Mapping[str, Any],
    *,
    namespace: str,
) -> None:
    context_namespace = context.get("deployment_namespace", "")
    if context_namespace != namespace:
        raise AgentCoreDeploymentError(
            "network preflight namespace does not match deployment namespace"
        )
    allowed = {
        "deployment_namespace",
        "deployment_profile",
        "runtime_network_availability_zones",
        "runtime_network_egress_mode",
        "runtime_network_mode",
        "runtime_network_private_subnet_ids",
        "runtime_network_security_group_ids",
        "runtime_network_vpc_cidr",
        "runtime_network_vpc_id",
    }
    unknown = sorted(set(context).difference(allowed))
    if unknown:
        raise AgentCoreDeploymentError(
            "network preflight contains unsupported CDK context: "
            f"{', '.join(unknown)}"
        )
    for name in sorted(set(context).difference({"deployment_namespace"})):
        value = context[name]
        encoded = (
            json.dumps(value, separators=(",", ":"), sort_keys=True)
            if isinstance(value, (bool, dict, int, list))
            else value
        )
        if not isinstance(encoded, str) or not encoded:
            raise AgentCoreDeploymentError(
                f"network preflight context {name} is invalid"
            )
        command.extend(["-c", f"{name}={encoded}"])


def _append_managed_network_context(
    command: list[str],
    preflight: NetworkPreflightResult,
    *,
    namespace: str,
) -> None:
    context = preflight.managed_stack_context
    if preflight.mode != "managed" or context is None:
        raise AgentCoreDeploymentError(
            "managed-network deployment requires managed preflight"
        )
    context_namespace = context.get("deployment_namespace", "")
    if context_namespace != namespace:
        raise AgentCoreDeploymentError(
            "managed-network preflight namespace does not match deployment"
        )
    allowed = {
        "deployment_namespace",
        "deployment_profile",
        "managed_network_availability_zone_ids",
        "managed_network_availability_zones",
        "managed_network_cost_acknowledgement",
        "managed_network_egress_mode",
        "managed_network_nat_gateway_count",
        "managed_network_vpc_cidr",
    }
    unknown = sorted(set(context).difference(allowed))
    if unknown:
        raise AgentCoreDeploymentError(
            "managed-network preflight contains unsupported CDK context: "
            f"{', '.join(unknown)}"
        )
    for name in sorted(set(context).difference({"deployment_namespace"})):
        value = context[name]
        encoded = (
            json.dumps(value, separators=(",", ":"), sort_keys=True)
            if isinstance(value, (bool, dict, int, list))
            else value
        )
        if not isinstance(encoded, str) or not encoded:
            raise AgentCoreDeploymentError(
                f"managed-network preflight context {name} is invalid"
            )
        command.extend(["-c", f"{name}={encoded}"])


def validate_rehearsal_control_table_arn(
    *,
    aws_region: str,
    deployment_namespace: str | None,
    rehearsal_control_table_arn: str | None,
) -> str | None:
    """Bind qualification deployments to one exact rehearsal ledger."""
    namespace = deployment_names(deployment_namespace).namespace
    if not namespace:
        if rehearsal_control_table_arn is not None:
            raise AgentCoreDeploymentError(
                "--rehearsal-control-table-arn is forbidden for the production/default deployment namespace"
            )
        return None
    if rehearsal_control_table_arn is None:
        raise AgentCoreDeploymentError("--rehearsal-control-table-arn is required with --deployment-namespace")
    match = _REHEARSAL_CONTROL_TABLE_ARN_PATTERN.fullmatch(rehearsal_control_table_arn)
    if match is None or match.group("region") != aws_region or "*" in rehearsal_control_table_arn:
        raise AgentCoreDeploymentError(
            "--rehearsal-control-table-arn must be the exact "
            "arn:aws:dynamodb ARN for table "
            "axonllm-rehearsal-control-ledger in the configured region"
        )
    return rehearsal_control_table_arn


def deployment_control_plane_domain(
    reviewed_domain: str,
    deployment_namespace: str | None = None,
) -> str:
    """Return the production hostname or its isolated qualification child."""
    namespace = (
        _ACTIVE_DEPLOYMENT_NAMESPACE
        if deployment_namespace is None
        else deployment_names(deployment_namespace).namespace
    )
    value = reviewed_domain if not namespace else f"{namespace}.{reviewed_domain}"
    labels = value.split(".")
    if len(value) > 253 or len(labels) < 2 or any(_DNS_LABEL_PATTERN.fullmatch(label) is None for label in labels):
        raise AgentCoreDeploymentError(
            "namespaced control-plane domain must be at most 253 characters with valid lowercase DNS labels"
        )
    return value


@dataclass(frozen=True)
class AwsIdentity:
    account_id: str
    partition: str


@dataclass(frozen=True)
class ApplicationStateValues:
    """Non-secret outputs passed explicitly to state consumers."""

    stack_name: str
    state_table_name: str
    selected_state_table_name: str
    data_key_arn: str
    routing_config_signing_key_arn: str
    provider_secret_arn: str
    event_outbox_queue_url: str
    event_outbox_queue_arn: str
    event_dead_letter_queue_url: str
    event_dead_letter_queue_arn: str
    security_event_topic_arn: str
    security_event_log_group_arn: str
    backup_vault_arn: str
    backup_role_arn: str

    def common_parameters(self) -> dict[str, str]:
        """Return the state parameters shared by runtime and control plane."""

        return {
            "ApplicationStateStackName": self.stack_name,
            "ApplicationStateDataKeyArn": self.data_key_arn,
            "ApplicationStateRoutingConfigSigningKeyArn": (
                self.routing_config_signing_key_arn
            ),
            "ApplicationStateSecurityEventOutboxQueueUrl": (
                self.event_outbox_queue_url
            ),
            "ApplicationStateSecurityEventOutboxQueueArn": (
                self.event_outbox_queue_arn
            ),
            "ApplicationStateSecurityEventTopicArn": (
                self.security_event_topic_arn
            ),
            "ApplicationStateSecurityEventLogGroupArn": (
                self.security_event_log_group_arn
            ),
        }

    def agentcore_parameters(self) -> dict[str, str]:
        """Return the full external-state contract consumed by AgentCore."""

        runtime_table = (
            ""
            if self.selected_state_table_name == self.state_table_name
            else self.selected_state_table_name
        )
        return {
            **self.common_parameters(),
            "PrimaryStateTableName": self.state_table_name,
            "RuntimeStateTableName": runtime_table,
            "ApplicationStateProviderSecretArn": self.provider_secret_arn,
            "ApplicationStateSecurityEventDeadLetterQueueUrl": (
                self.event_dead_letter_queue_url
            ),
            "ApplicationStateSecurityEventDeadLetterQueueArn": (
                self.event_dead_letter_queue_arn
            ),
            "ApplicationStateBackupVaultArn": self.backup_vault_arn,
            "ApplicationStateBackupRoleArn": self.backup_role_arn,
        }

    def control_plane_parameters(
        self,
        *,
        selected_state_table_name: str,
    ) -> dict[str, str]:
        """Return state parameters after checking runtime/state agreement."""

        if selected_state_table_name != self.selected_state_table_name:
            raise AgentCoreDeploymentError(
                "application-state and AgentCore selected tables do not match"
            )
        return self.common_parameters()

    def security_event_worker_parameters(self) -> dict[str, str]:
        """Return only retained identifiers required by event delivery."""

        return {
            "ApplicationStateStackName": self.stack_name,
            "ApplicationStateDataKeyArn": self.data_key_arn,
            "ApplicationStateSecurityEventOutboxQueueArn": (
                self.event_outbox_queue_arn
            ),
            "ApplicationStateSecurityEventTopicArn": (
                self.security_event_topic_arn
            ),
            "ApplicationStateSecurityEventLogGroupArn": (
                self.security_event_log_group_arn
            ),
        }

    def query_reconciliation_parameters(self) -> dict[str, str]:
        """Return selected-table parameters for scheduled reconciliation."""

        runtime_table = (
            ""
            if self.selected_state_table_name == self.state_table_name
            else self.selected_state_table_name
        )
        return {
            "PrimaryStateTableName": self.state_table_name,
            "RuntimeStateTableName": runtime_table,
        }


@dataclass(frozen=True)
class ServerlessControlArtifactValues:
    """Published, non-secret serverless artifacts bound to one source commit."""

    source_revision: str
    artifact_bucket_name: str
    artifact_bucket_key_arn: str
    control_api_object_key: str
    control_api_object_version: str
    control_api_sha256: str
    static_assets_object_key: str
    static_assets_object_version: str
    static_assets_sha256: str

    def validate(
        self,
        *,
        identity: AwsIdentity,
        region: str,
    ) -> None:
        if _TRANSITION_COMMIT_PATTERN.fullmatch(self.source_revision) is None:
            raise AgentCoreDeploymentError(
                "serverless artifact source revision is invalid"
            )
        if (
            re.fullmatch(
                r"[a-z0-9](?:[a-z0-9.-]{1,61}[a-z0-9])?",
                self.artifact_bucket_name,
            )
            is None
            or ".." in self.artifact_bucket_name
            or re.fullmatch(r"[0-9.]+", self.artifact_bucket_name)
        ):
            raise AgentCoreDeploymentError(
                "serverless artifact bucket name is invalid"
            )
        expected_key_prefix = (
            f"arn:{identity.partition}:kms:{region}:"
            f"{identity.account_id}:key/"
        )
        if (
            not self.artifact_bucket_key_arn.startswith(
                expected_key_prefix
            )
            or re.fullmatch(
                re.escape(expected_key_prefix) + r"[0-9a-fA-F-]{36}",
                self.artifact_bucket_key_arn,
            )
            is None
        ):
            raise AgentCoreDeploymentError(
                "serverless artifact KMS key does not match deployment "
                "account and region"
            )
        for label, key, version, digest, prefix in (
            (
                "control API",
                self.control_api_object_key,
                self.control_api_object_version,
                self.control_api_sha256,
                "axonllm-control-api",
            ),
            (
                "static assets",
                self.static_assets_object_key,
                self.static_assets_object_version,
                self.static_assets_sha256,
                "axonllm-static-assets",
            ),
        ):
            if (
                re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or re.fullmatch(r"[A-Za-z0-9._~-]{1,1024}", version)
                is None
                or not key
                or key.startswith("/")
                or "\\" in key
                or any(part in {"", ".", ".."} for part in key.split("/"))
                or len(key) > 1024
                or key.rsplit("/", 1)[-1]
                != f"{prefix}-{digest}.zip"
            ):
                raise AgentCoreDeploymentError(
                    f"serverless {label} artifact binding is invalid"
                )

    def parameters(self) -> dict[str, str]:
        return {
            "SourceRevision": self.source_revision,
            "ArtifactBucketName": self.artifact_bucket_name,
            "ArtifactBucketKeyArn": self.artifact_bucket_key_arn,
            "ControlApiCodeObjectKey": self.control_api_object_key,
            "ControlApiCodeObjectVersion": (
                self.control_api_object_version
            ),
            "ControlApiCodeSha256": self.control_api_sha256,
            "StaticAssetsObjectKey": self.static_assets_object_key,
            "StaticAssetsObjectVersion": (
                self.static_assets_object_version
            ),
            "StaticAssetsSha256": self.static_assets_sha256,
        }

    def worker_parameters(self) -> dict[str, str]:
        """Return the exact-version code binding used by worker Lambdas."""

        return {
            "SourceRevision": self.source_revision,
            "ArtifactBucketName": self.artifact_bucket_name,
            "WorkerCodeObjectKey": self.control_api_object_key,
            "WorkerCodeObjectVersion": self.control_api_object_version,
            "WorkerCodeSha256": self.control_api_sha256,
        }


@dataclass(frozen=True)
class ServerlessWorkersValues:
    """Non-secret worker outputs consumed by the serverless control API."""

    stack_name: str
    export_queue_url: str
    export_queue_arn: str
    export_bucket_name: str
    export_bucket_arn: str

    def control_plane_parameters(self) -> dict[str, str]:
        return {
            "ExportQueueUrl": self.export_queue_url,
            "ExportQueueArn": self.export_queue_arn,
            "ExportBucketName": self.export_bucket_name,
        }


@dataclass(frozen=True)
class ProductionEdgeValues:
    """Validated, non-secret outputs from the existing production edge."""

    stack_name: str
    distribution_id: str
    distribution_arn: str
    hostname: str
    state_table_name: str
    browser_client_id: str
    web_acl_arn: str

    def serverless_attachment_parameters(self) -> dict[str, str]:
        return {
            "ProductionDistributionArn": self.distribution_arn,
            "ProductionDistributionId": self.distribution_id,
            "ProductionControlPlaneHostname": self.hostname,
        }


@dataclass(frozen=True)
class ServerlessEdgeValues:
    """Qualified serverless origins bound to the existing production edge."""

    stack_name: str
    production_stack_name: str
    production_distribution_id: str
    production_distribution_arn: str
    production_hostname: str
    qualification_distribution_id: str
    qualification_url: str
    state_table_name: str
    source_revision: str
    control_api_sha256: str
    static_assets_sha256: str
    control_api_domain_name: str
    control_api_origin_path: str
    origin_credential_secret_arn: str
    static_bucket_domain_name: str

    def control_plane_parameters(
        self,
        *,
        backend_mode: str,
        migration_id: str,
    ) -> dict[str, str]:
        if backend_mode not in {"fargate", "serverless"}:
            raise AgentCoreDeploymentError(
                "edge backend mode must be fargate or serverless"
            )
        if _TRANSITION_ID_PATTERN.fullmatch(migration_id) is None:
            raise AgentCoreDeploymentError(
                "edge migration ID must be 64 lowercase hexadecimal "
                "characters"
            )
        return {
            "EdgeBackendMode": backend_mode,
            "EdgeMigrationId": migration_id,
            "ServerlessControlApiDomainName": (
                self.control_api_domain_name
            ),
            "ServerlessControlApiOriginPath": (
                self.control_api_origin_path
            ),
            "ServerlessOriginCredentialSecretArn": (
                self.origin_credential_secret_arn
            ),
            "ServerlessStaticBucketRegionalDomainName": (
                self.static_bucket_domain_name
            ),
            "ServerlessSourceRevision": self.source_revision,
            "ServerlessControlApiSha256": self.control_api_sha256,
            "ServerlessStaticAssetsSha256": (
                self.static_assets_sha256
            ),
        }


@dataclass(frozen=True)
class PrefixListRequirement:
    prefix_list_id: str
    location: str
    minimum_prefix_length: int
    maximum_total_addresses: int


@dataclass(frozen=True)
class IdentityValues:
    issuer: str
    discovery_url: str
    client_id: str
    audience: str
    tenant_claim: str
    project_claim: str
    user_pool_id: str | None = None
    hosted_ui_domain: str | None = None
    certification_client_id: str | None = None

    @property
    def client_ids(self) -> tuple[str, ...]:
        return (
            (self.client_id, self.certification_client_id)
            if self.certification_client_id is not None
            else (self.client_id,)
        )

    @property
    def audiences(self) -> tuple[str, ...]:
        return (
            (self.audience, self.certification_client_id)
            if self.certification_client_id is not None
            else (self.audience,)
        )


@dataclass(frozen=True)
class ManagedAdminResult:
    subject: str
    created: bool


@dataclass(frozen=True)
class CandidateBinding:
    endpoint_name: str
    provider_secret_version: str


def _aws_identity(
    boto3_session: Any,
    *,
    region: str,
) -> AwsIdentity:
    try:
        response = boto3_session.client(
            "sts",
            region_name=region,
        ).get_caller_identity()
    except Exception as exc:
        raise AgentCoreDeploymentError("could not resolve the AWS account used for deployment preflight") from exc
    account_id = response.get("Account")
    arn = response.get("Arn")
    if (
        not isinstance(account_id, str)
        or re.fullmatch(r"[0-9]{12}", account_id) is None
        or not isinstance(arn, str)
        or not arn.startswith("arn:")
    ):
        raise AgentCoreDeploymentError("AWS returned malformed deployment identity metadata")
    partition = arn.split(":", 2)[1]
    if partition not in {"aws", "aws-cn", "aws-us-gov"}:
        raise AgentCoreDeploymentError("AWS returned an unsupported deployment partition")
    return AwsIdentity(account_id=account_id, partition=partition)


def _canonical_policy_document(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(unquote(value))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AgentCoreDeploymentError("the bootstrap execution policy document is malformed") from exc
    if not isinstance(value, dict):
        raise AgentCoreDeploymentError("the bootstrap execution policy document is malformed")
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _require_exact_managed_policy(
    iam_client: Any,
    *,
    policy_arn: str,
    policy_name: str,
    description: str,
    document: dict[str, Any],
    tags: list[dict[str, str]],
    create_if_missing: bool,
    purpose: str,
) -> None:
    try:
        policy = iam_client.get_policy(PolicyArn=policy_arn).get("Policy")
    except Exception as exc:
        if _aws_error_code(exc) != "NoSuchEntity":
            raise AgentCoreDeploymentError(f"could not inspect the repository-defined {purpose}") from exc
        if not create_if_missing:
            raise AgentCoreDeploymentError(
                f"required {purpose} {policy_arn} is absent; run "
                "--bootstrap-cdk with an IAM bootstrap principal before "
                "deploying"
            ) from exc
        try:
            created = iam_client.create_policy(
                PolicyName=policy_name,
                Description=description,
                PolicyDocument=_canonical_policy_document(document),
                Tags=tags,
            )
        except Exception as create_exc:
            raise AgentCoreDeploymentError(f"could not create the repository-defined {purpose}") from create_exc
        policy = created.get("Policy")
    if not isinstance(policy, dict):
        raise AgentCoreDeploymentError(f"IAM returned malformed {purpose} metadata")
    version_id = policy.get("DefaultVersionId")
    actual_arn = policy.get("Arn")
    if not isinstance(version_id, str) or not version_id or actual_arn != policy_arn:
        raise AgentCoreDeploymentError(f"IAM returned unexpected {purpose} metadata")
    try:
        version = iam_client.get_policy_version(
            PolicyArn=policy_arn,
            VersionId=version_id,
        ).get("PolicyVersion")
    except Exception as exc:
        raise AgentCoreDeploymentError(f"could not read the {purpose} version") from exc
    actual_document = version.get("Document") if isinstance(version, dict) else None
    if not secrets.compare_digest(
        _canonical_policy_document(actual_document),
        _canonical_policy_document(document),
    ):
        raise AgentCoreDeploymentError(
            f"{purpose} {policy_arn} differs from this repository; "
            "replace it through a reviewed IAM change before deployment"
        )


def _require_bootstrap_execution_policy(
    boto3_session: Any,
    *,
    config: AgentCoreSetupConfig,
    create_if_missing: bool,
) -> tuple[AwsIdentity, tuple[str, ...]]:
    identity = _aws_identity(
        boto3_session,
        region=config.aws_region,
    )
    qualifier = bootstrap_qualifier_for_namespace(_ACTIVE_DEPLOYMENT_NAMESPACE)
    expected_service_boundary_arn = service_boundary_arn(
        partition=identity.partition,
        account_id=identity.account_id,
        region=config.aws_region,
        qualifier=qualifier,
    )
    expected_service_boundary_document = service_boundary_document(
        partition=identity.partition,
        account_id=identity.account_id,
        region=config.aws_region,
        qualifier=qualifier,
    )
    expected_bootstrap_boundary_arn = bootstrap_role_boundary_arn(
        partition=identity.partition,
        account_id=identity.account_id,
        region=config.aws_region,
        qualifier=qualifier,
    )
    expected_bootstrap_boundary_document = bootstrap_role_boundary_document(
        partition=identity.partition,
        account_id=identity.account_id,
        region=config.aws_region,
        qualifier=qualifier,
    )
    expected_documents = bootstrap_policy_documents(
        partition=identity.partition,
        account_id=identity.account_id,
        region=config.aws_region,
        qualifier=qualifier,
    )
    expected_arns = tuple(
        bootstrap_policy_part_arn(
            partition=identity.partition,
            account_id=identity.account_id,
            region=config.aws_region,
            qualifier=qualifier,
            part=part,
        )
        for part in range(1, EXECUTION_POLICY_PART_COUNT + 1)
    )
    iam_client = boto3_session.client(
        "iam",
        region_name=config.aws_region,
    )
    common_tags = [
        {"Key": "Application", "Value": "AxonLLM"},
        {"Key": "Qualifier", "Value": qualifier},
        {"Key": "Region", "Value": config.aws_region},
    ]
    _require_exact_managed_policy(
        iam_client,
        policy_arn=expected_service_boundary_arn,
        policy_name=service_boundary_name(
            config.aws_region,
            qualifier=qualifier,
        ),
        description=("Mandatory anti-escalation boundary for AxonLLM service roles"),
        document=expected_service_boundary_document,
        tags=[
            *common_tags,
            {"Key": "Purpose", "Value": "ServiceRoleBoundary"},
        ],
        create_if_missing=create_if_missing,
        purpose="bootstrap service-role boundary",
    )
    _require_exact_managed_policy(
        iam_client,
        policy_arn=expected_bootstrap_boundary_arn,
        policy_name=bootstrap_role_boundary_name(
            config.aws_region,
            qualifier=qualifier,
        ),
        description=("Mandatory anti-escalation boundary for AxonLLM CDK roles"),
        document=expected_bootstrap_boundary_document,
        tags=[
            *common_tags,
            {"Key": "Purpose", "Value": "BootstrapRoleBoundary"},
        ],
        create_if_missing=create_if_missing,
        purpose="CDK bootstrap-role boundary",
    )
    for part, (expected_arn, expected_document) in enumerate(
        zip(expected_arns, expected_documents, strict=True),
        start=1,
    ):
        _require_exact_managed_policy(
            iam_client,
            policy_arn=expected_arn,
            policy_name=bootstrap_policy_part_name(
                config.aws_region,
                qualifier=qualifier,
                part=part,
            ),
            description=(
                "Bounded CloudFormation execution for AxonLLM AgentCore "
                f"(part {part} of {EXECUTION_POLICY_PART_COUNT})"
            ),
            document=expected_document,
            tags=[
                *common_tags,
                {
                    "Key": "Purpose",
                    "Value": f"CloudFormationExecutionPart{part}",
                },
            ],
            create_if_missing=create_if_missing,
            purpose=f"bootstrap execution policy part {part}",
        )
    return identity, expected_arns


def _assert_cdk_execution_role_policy(
    boto3_session: Any,
    *,
    config: AgentCoreSetupConfig,
    identity: AwsIdentity,
    expected_policy_arns: tuple[str, ...],
) -> None:
    qualifier = bootstrap_qualifier_for_namespace(_ACTIVE_DEPLOYMENT_NAMESPACE)
    role_name = f"cdk-{qualifier}-cfn-exec-role-{identity.account_id}-{config.aws_region}"
    iam_client = boto3_session.client(
        "iam",
        region_name=config.aws_region,
    )
    expected_boundary = bootstrap_role_boundary_arn(
        partition=identity.partition,
        account_id=identity.account_id,
        region=config.aws_region,
        qualifier=qualifier,
    )
    attached: list[str] = []
    marker: str | None = None
    try:
        for _ in range(100):
            arguments: dict[str, str] = {"RoleName": role_name}
            if marker is not None:
                arguments["Marker"] = marker
            response = iam_client.list_attached_role_policies(**arguments)
            policies = response.get("AttachedPolicies")
            if not isinstance(policies, list):
                raise AgentCoreDeploymentError("IAM returned malformed CDK execution-role policies")
            for policy in policies:
                arn = policy.get("PolicyArn") if isinstance(policy, dict) else None
                if not isinstance(arn, str):
                    raise AgentCoreDeploymentError("IAM returned malformed CDK execution-role policies")
                attached.append(arn)
            if not response.get("IsTruncated"):
                break
            raw_marker = response.get("Marker")
            if not isinstance(raw_marker, str) or not raw_marker:
                raise AgentCoreDeploymentError("IAM truncated CDK execution-role policies without a marker")
            marker = raw_marker
        else:
            raise AgentCoreDeploymentError("IAM returned too many CDK execution-role policy pages")
        inline = iam_client.list_role_policies(RoleName=role_name)
        role = iam_client.get_role(RoleName=role_name).get("Role")
    except AgentCoreDeploymentError:
        raise
    except Exception as exc:
        raise AgentCoreDeploymentError(
            "could not verify the CDK CloudFormation execution role; bootstrap the account with --bootstrap-cdk"
        ) from exc
    inline_names = inline.get("PolicyNames")
    permissions_boundary = role.get("PermissionsBoundary") if isinstance(role, dict) else None
    if (
        sorted(attached) != sorted(expected_policy_arns)
        or not isinstance(inline_names, list)
        or inline_names
        or not isinstance(permissions_boundary, dict)
        or permissions_boundary.get("PermissionsBoundaryArn") != expected_boundary
        or permissions_boundary.get("PermissionsBoundaryType") != "Policy"
    ):
        raise AgentCoreDeploymentError(
            "CDK CloudFormation execution role must contain only the "
            "repository-defined policies, exact permissions boundary, and no "
            "inline policies"
        )


def _prefix_list_requirements(
    config: AgentCoreSetupConfig,
) -> tuple[PrefixListRequirement, ...]:
    requirements = [
        PrefixListRequirement(
            prefix_list_id=config.runtime.approved_https_prefix_list_id,
            location="runtime.approved_https_prefix_list_id",
            minimum_prefix_length=16,
            maximum_total_addresses=1_048_576,
        )
    ]
    if config.control_plane is not None:
        if (
            config.control_plane.endpoint_mode == CUSTOM_DOMAIN
            and config.control_plane.approved_ingress_prefix_list_id
            is not None
        ):
            requirements.append(
                PrefixListRequirement(
                    prefix_list_id=(config.control_plane.approved_ingress_prefix_list_id),
                    location=("control_plane.approved_ingress_prefix_list_id"),
                    minimum_prefix_length=24,
                    maximum_total_addresses=65_536,
                )
            )
        requirements.append(
                PrefixListRequirement(
                    prefix_list_id=(config.control_plane.approved_https_prefix_list_id),
                    location=("control_plane.approved_https_prefix_list_id"),
                    minimum_prefix_length=16,
                    maximum_total_addresses=1_048_576,
                )
        )
    return tuple(requirements)


def _validate_prefix_list_inputs(
    boto3_session: Any,
    *,
    config: AgentCoreSetupConfig,
) -> None:
    identity = _aws_identity(
        boto3_session,
        region=config.aws_region,
    )
    ec2_client = boto3_session.client(
        "ec2",
        region_name=config.aws_region,
    )
    validated: dict[str, tuple[int, int]] = {}
    for requirement in _prefix_list_requirements(config):
        previous = validated.get(requirement.prefix_list_id)
        constraints = (
            requirement.minimum_prefix_length,
            requirement.maximum_total_addresses,
        )
        if previous is not None and previous[0] >= constraints[0] and previous[1] <= constraints[1]:
            continue
        try:
            response = ec2_client.describe_managed_prefix_lists(PrefixListIds=[requirement.prefix_list_id])
        except Exception as exc:
            raise AgentCoreDeploymentError(f"could not validate {requirement.location} in AWS") from exc
        lists = response.get("PrefixLists")
        if not isinstance(lists, list) or len(lists) != 1:
            raise AgentCoreDeploymentError(f"{requirement.location} did not resolve to one managed prefix list")
        prefix_list = lists[0]
        if (
            not isinstance(prefix_list, dict)
            or prefix_list.get("PrefixListId") != requirement.prefix_list_id
            or prefix_list.get("OwnerId") != identity.account_id
            or prefix_list.get("AddressFamily") != "IPv4"
            or prefix_list.get("State")
            not in {
                "create-complete",
                "modify-complete",
                "restore-complete",
            }
            or not isinstance(prefix_list.get("Version"), int)
        ):
            raise AgentCoreDeploymentError(
                f"{requirement.location} must be a stable, customer-owned IPv4 prefix list in the deployment account"
            )
        entries: list[object] = []
        next_token: str | None = None
        for _ in range(100):
            arguments: dict[str, Any] = {
                "PrefixListId": requirement.prefix_list_id,
                "TargetVersion": prefix_list["Version"],
                "MaxResults": 100,
            }
            if next_token is not None:
                arguments["NextToken"] = next_token
            try:
                page = ec2_client.get_managed_prefix_list_entries(**arguments)
            except Exception as exc:
                raise AgentCoreDeploymentError(f"could not read entries for {requirement.location}") from exc
            raw_entries = page.get("Entries")
            if not isinstance(raw_entries, list):
                raise AgentCoreDeploymentError(f"AWS returned malformed entries for {requirement.location}")
            entries.extend(raw_entries)
            raw_next = page.get("NextToken")
            if raw_next is None:
                break
            if not isinstance(raw_next, str) or not raw_next:
                raise AgentCoreDeploymentError(f"AWS returned malformed pagination for {requirement.location}")
            next_token = raw_next
        else:
            raise AgentCoreDeploymentError(f"{requirement.location} has too many entry pages")
        if not entries:
            raise AgentCoreDeploymentError(f"{requirement.location} must contain at least one CIDR")
        total_addresses = 0
        for entry in entries:
            cidr = entry.get("Cidr") if isinstance(entry, dict) else None
            try:
                network = ipaddress.ip_network(cidr, strict=True)
            except (TypeError, ValueError) as exc:
                raise AgentCoreDeploymentError(f"{requirement.location} contains an invalid CIDR") from exc
            if (
                not isinstance(network, ipaddress.IPv4Network)
                or not network.is_global
                or network.prefixlen < requirement.minimum_prefix_length
            ):
                raise AgentCoreDeploymentError(
                    f"{requirement.location} contains unsafe CIDR {network}; "
                    f"require globally routable IPv4 prefixes of /"
                    f"{requirement.minimum_prefix_length} or narrower"
                )
            total_addresses += network.num_addresses
        if total_addresses > requirement.maximum_total_addresses:
            raise AgentCoreDeploymentError(
                f"{requirement.location} exposes {total_addresses} addresses; "
                f"the maximum is {requirement.maximum_total_addresses}"
            )
        validated[requirement.prefix_list_id] = constraints


def _assert_no_retained_runtime_without_stack(
    boto3_session: Any,
    *,
    config: AgentCoreSetupConfig,
) -> None:
    dynamodb_client = boto3_session.client(
        "dynamodb",
        region_name=config.aws_region,
    )
    try:
        response = dynamodb_client.describe_table(TableName=_PRIMARY_STATE_TABLE_NAME)
    except Exception as exc:
        if _aws_error_code(exc) == "ResourceNotFoundException":
            return
        raise AgentCoreDeploymentError("could not check for retained AgentCore state before deployment") from exc
    table = response.get("Table")
    if isinstance(table, dict) and table.get("TableName") == (_PRIMARY_STATE_TABLE_NAME):
        raise AgentCoreDeploymentError(
            f"retained table {_PRIMARY_STATE_TABLE_NAME} exists without "
            f"{AGENTCORE_STACK}; import the retained table into a recovered "
            "stack or remove it through an approved recovery change before "
            "deployment; no AWS resources were changed"
        )
    raise AgentCoreDeploymentError("DynamoDB returned malformed retained-state metadata")


def _assert_no_retained_identity_without_stack(
    boto3_session: Any,
    *,
    config: AgentCoreSetupConfig,
) -> None:
    managed = config.managed_cognito
    if managed is None:
        return
    cognito_client = boto3_session.client(
        "cognito-idp",
        region_name=config.aws_region,
    )
    hosted_ui_domain_prefix = _managed_hosted_ui_domain_prefix(
        config,
        deployment_namespace=_ACTIVE_DEPLOYMENT_NAMESPACE,
    )
    try:
        domain = cognito_client.describe_user_pool_domain(Domain=hosted_ui_domain_prefix).get("DomainDescription")
    except Exception as exc:
        raise AgentCoreDeploymentError("could not check for a retained Cognito domain before deployment") from exc
    if isinstance(domain, dict) and domain.get("UserPoolId"):
        raise AgentCoreDeploymentError(
            f"retained Cognito domain {hosted_ui_domain_prefix} exists "
            f"without {IDENTITY_STACK}; recover or import the retained "
            "identity stack before deployment; no AWS resources were changed"
        )
    next_token: str | None = None
    for _ in range(100):
        arguments: dict[str, Any] = {"MaxResults": 60}
        if next_token is not None:
            arguments["NextToken"] = next_token
        try:
            response = cognito_client.list_user_pools(**arguments)
        except Exception as exc:
            raise AgentCoreDeploymentError("could not check for retained Cognito pools before deployment") from exc
        pools = response.get("UserPools")
        if not isinstance(pools, list):
            raise AgentCoreDeploymentError("Cognito returned malformed retained-pool metadata")
        if any(isinstance(pool, dict) and pool.get("Name") == _MANAGED_USER_POOL_NAME for pool in pools):
            raise AgentCoreDeploymentError(
                f"retained Cognito pool {_MANAGED_USER_POOL_NAME} exists "
                f"without {IDENTITY_STACK}; recover or import the retained "
                "identity stack before deployment; no AWS resources were "
                "changed"
            )
        raw_next = response.get("NextToken")
        if raw_next is None:
            return
        if not isinstance(raw_next, str) or not raw_next:
            raise AgentCoreDeploymentError("Cognito returned malformed retained-pool pagination")
        next_token = raw_next
    raise AgentCoreDeploymentError("Cognito returned too many retained-pool pages")


def _parameter(name: str, value: str, *, stack: str) -> list[str]:
    return ["--parameters", f"{stack}:{name}={value}"]


def managed_ses_sender(
    config: AgentCoreSetupConfig,
) -> tuple[str, str]:
    """Return an explicit SES sender/source-identity pair."""
    managed = config.managed_cognito
    if managed is None:
        raise AgentCoreDeploymentError("managed Cognito settings are missing")
    if managed.ses_from_email is not None:
        if managed.ses_verified_domain is None:
            raise AgentCoreDeploymentError(
                "managed Cognito SES source identity is missing"
            )
        return managed.ses_from_email, managed.ses_verified_domain
    local, separator, domain = config.admin.email.rpartition("@")
    if separator != "@" or not local or not domain:
        raise AgentCoreDeploymentError("administrator email cannot be used as the SES sender")
    domain = domain.casefold()
    return f"{local}@{domain}", domain


def _managed_hosted_ui_domain_prefix(
    config: AgentCoreSetupConfig,
    *,
    deployment_namespace: str | None = None,
) -> str:
    if config.identity_mode != MANAGED_COGNITO:
        raise AgentCoreDeploymentError("the identity stack is only used for managed-cognito")
    managed = config.managed_cognito
    if managed is None:
        raise AgentCoreDeploymentError("managed Cognito settings are missing")
    namespace = (
        _ACTIVE_DEPLOYMENT_NAMESPACE
        if deployment_namespace is None
        else deployment_names(deployment_namespace).namespace
    )
    suffix = f"-{namespace}" if namespace else ""
    value = f"{managed.hosted_ui_domain_prefix}{suffix}"
    if len(value) > 63:
        raise AgentCoreDeploymentError(
            "managed Cognito hosted UI domain prefix plus deployment namespace must be at most 63 characters"
        )
    return value


def _identity_parameters(
    config: AgentCoreSetupConfig,
    *,
    deployment_namespace: str | None = None,
) -> dict[str, str]:
    if config.identity_mode != MANAGED_COGNITO:
        raise AgentCoreDeploymentError("the identity stack is only used for managed-cognito")
    managed = config.managed_cognito
    control_plane = config.control_plane
    if managed is None or control_plane is None:
        raise AgentCoreDeploymentError("managed Cognito or control-plane settings are missing")
    namespace = (
        _ACTIVE_DEPLOYMENT_NAMESPACE
        if deployment_namespace is None
        else deployment_names(deployment_namespace).namespace
    )
    ses_from_email, ses_source_identity = managed_ses_sender(config)
    parameters = {
        "HostedUiDomainPrefix": _managed_hosted_ui_domain_prefix(
            config,
            deployment_namespace=namespace,
        ),
        "SesFromEmail": ses_from_email,
        "SesVerifiedDomain": ses_source_identity,
    }
    if control_plane.endpoint_mode == CLOUDFRONT:
        parameters["EndpointMode"] = CLOUDFRONT
    if control_plane.endpoint_mode == CUSTOM_DOMAIN:
        if control_plane.domain_name is None:
            raise AgentCoreDeploymentError(
                "custom-domain control-plane hostname is missing"
            )
        domain_name = deployment_control_plane_domain(
            control_plane.domain_name,
            namespace,
        )
        parameters.update(
            {
                "ControlPlaneDomainName": domain_name,
            }
        )
    return parameters


def identity_deploy_command(
    config: AgentCoreSetupConfig,
    *,
    outputs_file: Path,
    assume_yes: bool,
    deployment_namespace: str | None = None,
) -> list[str]:
    names = _current_deployment_names() if deployment_namespace is None else deployment_names(deployment_namespace)
    command = [
        str(_cdk_cli_path()),
        "deploy",
        names.identity_stack,
        "-c",
        "deployment_target=identity",
        "-c",
        f"region={config.aws_region}",
    ]
    command.extend(_deployment_context_arguments(names.namespace))
    for name, value in _identity_parameters(
        config,
        deployment_namespace=names.namespace,
    ).items():
        command.extend(_parameter(name, value, stack=names.identity_stack))
    command.extend(
        [
            "--require-approval",
            "never" if assume_yes else "broadening",
            "--outputs-file",
            str(outputs_file),
        ]
    )
    return command


def application_state_deploy_command(
    config: AgentCoreSetupConfig,
    *,
    outputs_file: Path,
    assume_yes: bool,
    runtime_state_table_name: str = "",
    deployment_namespace: str | None = None,
    backup_vault_name: str | None = None,
    security_event_topic_name: str | None = None,
) -> list[str]:
    """Build a dedicated application-state deployment command."""

    names = (
        _current_deployment_names()
        if deployment_namespace is None
        else deployment_names(deployment_namespace)
    )
    command = [
        str(_cdk_cli_path()),
        "deploy",
        names.application_state_stack,
        "-c",
        "deployment_target=application-state",
        "-c",
        f"region={config.aws_region}",
    ]
    command.extend(_deployment_context_arguments(names.namespace))
    if backup_vault_name is not None:
        command.extend(
            [
                "-c",
                f"application_state_backup_vault_name={backup_vault_name}",
            ]
        )
    if security_event_topic_name is not None:
        command.extend(
            [
                "-c",
                (
                    "application_state_security_event_topic_name="
                    f"{security_event_topic_name}"
                ),
            ]
        )
    if runtime_state_table_name:
        command.extend(
            _parameter(
                "RuntimeStateTableName",
                runtime_state_table_name,
                stack=names.application_state_stack,
            )
        )
    command.extend(
        [
            "--require-approval",
            "never" if assume_yes else "broadening",
            "--outputs-file",
            str(outputs_file),
        ]
    )
    return command


def agentcore_parked_change_set_command(
    config: AgentCoreSetupConfig,
    *,
    change_set_name: str,
    deployment_namespace: str | None = None,
) -> list[str]:
    """Build a prepare-only change set for the parked runtime shell."""

    names = (
        _current_deployment_names()
        if deployment_namespace is None
        else deployment_names(deployment_namespace)
    )
    return _parked_change_set_command(
        region=config.aws_region,
        stack_name=names.agentcore_stack,
        deployment_target="agentcore-parked",
        deployment_namespace=names.namespace,
        change_set_name=change_set_name,
    )


def managed_network_parked_change_set_command(
    config: AgentCoreSetupConfig,
    *,
    change_set_name: str,
    deployment_namespace: str | None = None,
) -> list[str]:
    """Build a prepare-only change set for the parked network shell."""

    names = (
        _current_deployment_names()
        if deployment_namespace is None
        else deployment_names(deployment_namespace)
    )
    return _parked_change_set_command(
        region=config.aws_region,
        stack_name=names.managed_network_stack,
        deployment_target="managed-network-parked",
        deployment_namespace=names.namespace,
        change_set_name=change_set_name,
    )


def prepare_change_set_command(
    deploy_command: list[str],
    *,
    change_set_name: str,
) -> list[str]:
    """Convert one validated CDK deploy command into a preview-only command."""

    name = _validate_lifecycle_change_set_name(change_set_name)
    command = list(deploy_command)
    if len(command) < 3 or command[1] != "deploy":
        raise AgentCoreDeploymentError(
            "lifecycle preview requires a CDK deploy command"
        )
    if "--method" in command or "--change-set-name" in command:
        raise AgentCoreDeploymentError(
            "lifecycle preview command already selects a deployment method"
        )
    while "--outputs-file" in command:
        index = command.index("--outputs-file")
        if index + 1 >= len(command):
            raise AgentCoreDeploymentError(
                "deployment command has an incomplete outputs-file option"
            )
        del command[index : index + 2]
    if "--require-approval" in command:
        index = command.index("--require-approval")
        if index + 1 >= len(command):
            raise AgentCoreDeploymentError(
                "deployment command has an incomplete approval option"
            )
        command[index + 1] = "never"
    else:
        command.extend(["--require-approval", "never"])
    command.extend(
        [
            "--method",
            "prepare-change-set",
            "--change-set-name",
            name,
        ]
    )
    return command


def managed_network_deploy_command(
    config: AgentCoreSetupConfig,
    preflight: NetworkPreflightResult,
    application_state: ApplicationStateValues,
    *,
    outputs_file: Path,
    assume_yes: bool,
    deployment_namespace: str | None = None,
    rehearsal_control_table_arn: str | None = None,
) -> list[str]:
    """Build the optional managed-network deployment command."""

    names = (
        _current_deployment_names()
        if deployment_namespace is None
        else deployment_names(deployment_namespace)
    )
    if application_state.stack_name != names.application_state_stack:
        raise AgentCoreDeploymentError(
            "application-state descriptor does not match the deployment "
            "namespace"
        )
    command = [
        str(_cdk_cli_path()),
        "deploy",
        names.managed_network_stack,
        "-c",
        "deployment_target=managed-network",
        "-c",
        f"region={config.aws_region}",
    ]
    command.extend(_deployment_context_arguments(names.namespace))
    _append_managed_network_context(
        command,
        preflight,
        namespace=names.namespace,
    )
    rehearsal_arn = validate_rehearsal_control_table_arn(
        aws_region=config.aws_region,
        deployment_namespace=names.namespace,
        rehearsal_control_table_arn=rehearsal_control_table_arn,
    )
    parameters = {
        "SelectedStateTableName": (
            application_state.selected_state_table_name
        ),
        "ApplicationStateDataKeyArn": application_state.data_key_arn,
        "ApplicationStateRoutingConfigSigningKeyArn": (
            application_state.routing_config_signing_key_arn
        ),
        "ApplicationStateProviderSecretArn": (
            application_state.provider_secret_arn
        ),
        "ApplicationStateSecurityEventOutboxQueueArn": (
            application_state.event_outbox_queue_arn
        ),
        "ApplicationStateSecurityEventTopicArn": (
            application_state.security_event_topic_arn
        ),
        "ApplicationStateSecurityEventLogGroupArn": (
            application_state.security_event_log_group_arn
        ),
        "BedrockInvokeResourceArns": ",".join(
            config.runtime.bedrock_invoke_resource_arns
        ),
        "VerifiedImageUri": config.runtime.verified_image_uri,
    }
    if preflight.egress_mode == "managed-nat":
        prefix_list_id = preflight.approved_https_prefix_list_id
        if prefix_list_id is None:
            raise AgentCoreDeploymentError(
                "managed-nat preflight is missing the approved HTTPS "
                "prefix list"
            )
        parameters["ApprovedHttpsPrefixListId"] = prefix_list_id
    if rehearsal_arn is not None:
        parameters["RehearsalControlTableArn"] = rehearsal_arn
    for name, value in parameters.items():
        command.extend(
            _parameter(
                name,
                value,
                stack=names.managed_network_stack,
            )
        )
    _append_athena_contexts(command, config)
    command.extend(
        [
            "--require-approval",
            "never" if assume_yes else "broadening",
            "--outputs-file",
            str(outputs_file),
        ]
    )
    return command


def _parked_change_set_command(
    *,
    region: str,
    stack_name: str,
    deployment_target: str,
    deployment_namespace: str,
    change_set_name: str,
) -> list[str]:
    name = _validate_lifecycle_change_set_name(change_set_name)
    command = [
        str(_cdk_cli_path()),
        "deploy",
        stack_name,
        "-c",
        f"deployment_target={deployment_target}",
        "-c",
        f"region={region}",
    ]
    command.extend(
        _deployment_context_arguments(deployment_namespace)
    )
    command.extend(
        [
            "--require-approval",
            "never",
            "--method",
            "prepare-change-set",
            "--change-set-name",
            name,
        ]
    )
    return command


def _validate_lifecycle_change_set_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or _LIFECYCLE_CHANGE_SET_NAME_PATTERN.fullmatch(value) is None
    ):
        raise AgentCoreDeploymentError(
            "lifecycle change-set name must start with a letter and contain "
            "only letters, digits, or hyphens"
        )
    return value


def _athena_contexts(
    config: AgentCoreSetupConfig,
) -> dict[str, str]:
    athena = config.runtime.athena_query
    if athena is None:
        return {}
    bindings = [
        {
            "tenant_id": config.tenant.tenant_id,
            "project_id": config.tenant.project_id,
            "role_arn": role_arn,
        }
        for role_arn in athena.role_arns
    ]
    bindings_json = json.dumps(
        bindings,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(bindings_json) > _MAX_AGENTCORE_ENVIRONMENT_VALUE_CHARACTERS:
        raise AgentCoreDeploymentError(
            "athena_query_bindings exceeds the AgentCore 2,048-character environment value limit"
        )
    return {
        "athena_query_bindings": bindings_json,
        "athena_query_timeout_seconds": f"{athena.timeout_seconds:g}",
        "athena_query_max_rows": str(athena.max_rows),
        "athena_query_max_result_bytes": str(athena.max_result_bytes),
        "athena_query_max_bytes_scanned": str(athena.max_bytes_scanned),
        "athena_query_poll_interval_seconds": (f"{athena.poll_interval_seconds:g}"),
        "athena_query_project_rpm": str(athena.project_rpm),
        "athena_query_principal_rpm": str(athena.principal_rpm),
        "athena_query_project_concurrency": str(athena.project_concurrency),
        "athena_query_principal_concurrency": str(athena.principal_concurrency),
        "athena_query_project_scan_bytes_per_minute": str(athena.project_scan_bytes_per_minute),
        "athena_query_principal_scan_bytes_per_minute": str(athena.principal_scan_bytes_per_minute),
        "athena_query_max_datasources_per_tenant": str(athena.max_datasources_per_tenant),
    }


def _append_athena_contexts(
    command: list[str],
    config: AgentCoreSetupConfig,
) -> None:
    for name, value in _athena_contexts(config).items():
        command.extend(["-c", f"{name}={value}"])


def _athena_configuration_fingerprint(
    config: AgentCoreSetupConfig,
) -> str:
    contexts = _athena_contexts(config)
    values: dict[str, object] = {
        "enabled": config.runtime.athena_query is not None,
    }
    values.update(contexts)
    encoded = json.dumps(
        values,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _shared_runtime_configuration(
    config: AgentCoreSetupConfig,
) -> dict[str, str]:
    return {
        "AlarmNotificationEmail": config.admin.email,
        "ApprovedHttpsPrefixListId": (config.runtime.approved_https_prefix_list_id),
        "AthenaConfigurationFingerprint": (_athena_configuration_fingerprint(config)),
        "BedrockInvokeResourceArns": ",".join(config.runtime.bedrock_invoke_resource_arns),
    }


def _validate_shared_runtime_configuration(
    config: AgentCoreSetupConfig,
    outputs: Mapping[str, str],
) -> None:
    expected = _shared_runtime_configuration(config)
    missing = sorted(name for name in expected if name not in outputs)
    mismatched = sorted(name for name, value in expected.items() if outputs.get(name) != value)
    if missing:
        raise AgentCoreDeploymentError(
            "the production stack predates shared-runtime configuration "
            "locking; migrate it under an approved maintenance change before "
            "deploying a candidate"
        )
    if mismatched:
        raise AgentCoreDeploymentError(
            "candidate deployment cannot change production-shared "
            "configuration before blue/green runtime isolation: " + ", ".join(mismatched)
        )


def _new_candidate_endpoint_name() -> str:
    return f"candidate_{secrets.token_hex(16)}"


def _validate_candidate_endpoint_name(value: str) -> str:
    if _CANDIDATE_ENDPOINT_PATTERN.fullmatch(value) is None:
        raise AgentCoreDeploymentError("candidate endpoint name must be a generated high-entropy qualifier")
    return value


def initial_routing_config_zlib_base64() -> str:
    """Return the validated packaged routing defaults for one-time seeding."""
    from src.gateway.model_registry import ModelRegistry
    from src.gateway.routing_config import RoutingConfigSnapshot

    resource = files("src.gateway").joinpath(
        "resources/runtime/config/models.yaml"
    )
    registry = ModelRegistry.from_yaml(resource.read_text(encoding="utf-8"))
    snapshot = RoutingConfigSnapshot.from_registry(registry)
    compressed = base64.b64encode(
        zlib.compress(snapshot.document.encode("utf-8"), level=9)
    ).decode("ascii")
    if len(compressed) > 4096:
        raise AgentCoreDeploymentError(
            "packaged routing configuration is too large for AgentCore "
            "bootstrap"
        )
    return compressed


def agentcore_deploy_command(
    config: AgentCoreSetupConfig,
    identity: IdentityValues,
    *,
    outputs_file: Path,
    assume_yes: bool,
    candidate_endpoint_name: str,
    provider_secret_version: str = "bootstrap",
    publish_candidate_endpoint: bool = True,
    publish_production_endpoint: bool = False,
    production_runtime_version: str = "",
    application_state: ApplicationStateValues | None = None,
    network_preflight: NetworkPreflightResult | None = None,
    managed_network_outputs: Mapping[str, str] | None = None,
    deployment_namespace: str | None = None,
    rehearsal_control_table_arn: str | None = None,
) -> list[str]:
    candidate_endpoint_name = _validate_candidate_endpoint_name(candidate_endpoint_name)
    names = _current_deployment_names() if deployment_namespace is None else deployment_names(deployment_namespace)
    command = [
        str(_cdk_cli_path()),
        "deploy",
        names.agentcore_stack,
        "-c",
        "deployment_target=agentcore",
        "-c",
        f"region={config.aws_region}",
    ]
    command.extend(_deployment_context_arguments(names.namespace))
    verified_network_context = None
    if network_preflight is not None:
        from src.gateway.deployment.network_preflight import (
            NetworkPreflightError,
            runtime_network_context,
        )

        try:
            verified_network_context = runtime_network_context(
                network_preflight,
                managed_outputs=(
                    dict(managed_network_outputs)
                    if managed_network_outputs is not None
                    else None
                ),
                expected_managed_stack_name=(
                    names.managed_network_stack
                    if network_preflight.mode == "managed"
                    else None
                ),
            )
        except NetworkPreflightError as exc:
            raise AgentCoreDeploymentError(
                f"runtime network preflight is invalid: {exc}"
            ) from exc
        _append_verified_network_context(
            command,
            verified_network_context,
            namespace=names.namespace,
        )
    elif managed_network_outputs is not None:
        raise AgentCoreDeploymentError(
            "managed network outputs require network preflight"
        )
    if application_state is not None:
        if application_state.stack_name != names.application_state_stack:
            raise AgentCoreDeploymentError(
                "application-state descriptor does not match the deployment "
                "namespace"
            )
        command.extend(["-c", "application_state_mode=external"])
    rehearsal_arn = validate_rehearsal_control_table_arn(
        aws_region=config.aws_region,
        deployment_namespace=names.namespace,
        rehearsal_control_table_arn=rehearsal_control_table_arn,
    )
    parameters = {
        "VerifiedImageUri": config.runtime.verified_image_uri,
        "OidcIssuer": identity.issuer,
        "OidcDiscoveryUrl": identity.discovery_url,
        "OidcClientIds": ",".join(identity.client_ids),
        "OidcAudiences": ",".join(identity.audiences),
        "OidcTenantClaim": identity.tenant_claim,
        "OidcProjectClaim": identity.project_claim,
        "BedrockInvokeResourceArns": ",".join(config.runtime.bedrock_invoke_resource_arns),
        "AlarmNotificationEmail": config.admin.email,
        "CandidateEndpointName": candidate_endpoint_name,
        "EnabledProviders": ",".join(config.runtime.enabled_providers),
        "ProviderSecretVersion": provider_secret_version,
        "InitialRoutingConfigZlibBase64": (
            initial_routing_config_zlib_base64()
        ),
        "PublishCandidateEndpoint": ("true" if publish_candidate_endpoint else "false"),
        "PublishProductionEndpoint": ("true" if publish_production_endpoint else "false"),
        "ProductionRuntimeVersion": production_runtime_version,
    }
    if verified_network_context is None:
        parameters["ApprovedHttpsPrefixListId"] = (
            config.runtime.approved_https_prefix_list_id
        )
    elif (
        verified_network_context["runtime_network_mode"] == "existing"
        and verified_network_context["runtime_network_egress_mode"]
        == "existing-egress"
        and not verified_network_context[
            "runtime_network_security_group_ids"
        ]
    ):
        prefix_list_id = (
            network_preflight.approved_https_prefix_list_id
            if network_preflight is not None
            else None
        )
        if prefix_list_id is None:
            raise AgentCoreDeploymentError(
                "existing-egress with an AxonLLM security group requires "
                "an approved HTTPS prefix list"
            )
        parameters["ApprovedHttpsPrefixListId"] = prefix_list_id
    if rehearsal_arn is not None:
        parameters["RehearsalControlTableArn"] = rehearsal_arn
    if application_state is not None:
        parameters.update(application_state.agentcore_parameters())
    for name, value in parameters.items():
        command.extend(_parameter(name, value, stack=names.agentcore_stack))
    _append_athena_contexts(command, config)
    command.extend(
        [
            "--require-approval",
            "never" if assume_yes else "broadening",
            "--outputs-file",
            str(outputs_file),
        ]
    )
    return command


def _control_plane_parameters(
    config: AgentCoreSetupConfig,
    *,
    primary_state_table_name: str,
    runtime_state_table_name: str = "",
    recovery_approval_id: str = "",
    deployment_transition_id: str = _UNBOUND_DEPLOYMENT_TRANSITION_ID,
    application_state: ApplicationStateValues | None = None,
    deployment_namespace: str | None = None,
    rehearsal_control_table_arn: str | None = None,
) -> dict[str, str]:
    if config.identity_mode != MANAGED_COGNITO:
        raise AgentCoreDeploymentError("the web control plane currently requires managed-cognito")
    control_plane = config.control_plane
    if control_plane is None:
        raise AgentCoreDeploymentError("control-plane settings are missing")
    names = _current_deployment_names() if deployment_namespace is None else deployment_names(deployment_namespace)
    parameters = {
        "AgentCoreStackName": names.agentcore_stack,
        "IdentityStackName": names.identity_stack,
        "ControlPlaneVerifiedImageUri": (control_plane.verified_image_uri),
        "ApprovedHttpsPrefixListId": (control_plane.approved_https_prefix_list_id),
        "SamlLoginPath": control_plane.saml_login_path,
        "PrimaryStateTableName": primary_state_table_name,
        "RuntimeStateTableName": runtime_state_table_name,
        "RecoveryCutoverMode": "normal",
        "RecoveryApprovalId": recovery_approval_id,
        "DeploymentTransitionId": deployment_transition_id,
    }
    if application_state is not None:
        if (
            application_state.stack_name
            != names.application_state_stack
            or application_state.state_table_name
            != primary_state_table_name
        ):
            raise AgentCoreDeploymentError(
                "application-state descriptor does not match the control-plane "
                "deployment"
            )
        selected_state_table_name = (
            runtime_state_table_name or primary_state_table_name
        )
        parameters.update(
            application_state.control_plane_parameters(
                selected_state_table_name=selected_state_table_name,
            )
        )
    if control_plane.endpoint_mode == CLOUDFRONT:
        parameters["EndpointMode"] = CLOUDFRONT
    if control_plane.endpoint_mode == CUSTOM_DOMAIN:
        if (
            control_plane.domain_name is None
            or control_plane.certificate_arn is None
            or control_plane.public_hosted_zone_id is None
            or control_plane.approved_ingress_prefix_list_id is None
        ):
            raise AgentCoreDeploymentError(
                "custom-domain control-plane settings are incomplete"
            )
        domain_name = deployment_control_plane_domain(
            control_plane.domain_name,
            names.namespace,
        )
        parameters.update(
            {
                "CertificateArn": control_plane.certificate_arn,
                "ControlPlaneDomainName": domain_name,
                "PublicHostedZoneId": (
                    control_plane.public_hosted_zone_id
                ),
                "ApprovedIngressPrefixListId": (
                    control_plane.approved_ingress_prefix_list_id
                ),
            }
        )
    elif control_plane.endpoint_mode == CLOUDFRONT:
        parameters["AllowedViewerCidrs"] = ",".join(
            control_plane.allowed_viewer_cidrs
        )
    else:  # pragma: no cover - setup validation owns the closed set
        raise AgentCoreDeploymentError(
            "unsupported control-plane endpoint mode"
        )
    rehearsal_arn = validate_rehearsal_control_table_arn(
        aws_region=config.aws_region,
        deployment_namespace=names.namespace,
        rehearsal_control_table_arn=rehearsal_control_table_arn,
    )
    if rehearsal_arn is not None:
        parameters["RehearsalControlTableArn"] = rehearsal_arn
    return parameters


def control_plane_deploy_command(
    config: AgentCoreSetupConfig,
    *,
    primary_state_table_name: str,
    outputs_file: Path,
    assume_yes: bool,
    runtime_state_table_name: str = "",
    recovery_approval_id: str = "",
    deployment_transition_id: str = _UNBOUND_DEPLOYMENT_TRANSITION_ID,
    application_state: ApplicationStateValues | None = None,
    deployment_namespace: str | None = None,
    rehearsal_control_table_arn: str | None = None,
    serverless_edge: ServerlessEdgeValues | None = None,
    edge_backend_mode: str = "fargate",
    edge_migration_id: str = "",
) -> list[str]:
    names = _current_deployment_names() if deployment_namespace is None else deployment_names(deployment_namespace)
    command = [
        str(_cdk_cli_path()),
        "deploy",
        names.control_plane_stack,
        "-c",
        "deployment_target=control-plane",
        "-c",
        f"region={config.aws_region}",
    ]
    command.extend(_deployment_context_arguments(names.namespace))
    if application_state is not None:
        command.extend(["-c", "application_state_mode=external"])
    if serverless_edge is not None:
        if (
            serverless_edge.stack_name
            != names.serverless_control_plane_stack
            or serverless_edge.production_stack_name
            != names.control_plane_stack
        ):
            raise AgentCoreDeploymentError(
                "serverless edge descriptor does not match the control-plane "
                "namespace"
            )
        if serverless_edge.state_table_name != primary_state_table_name:
            raise AgentCoreDeploymentError(
                "serverless edge descriptor does not match canonical state"
            )
        command.extend(["-c", "edge_cutover_enabled=true"])
    elif edge_backend_mode != "fargate" or edge_migration_id:
        raise AgentCoreDeploymentError(
            "edge backend selection requires a qualified serverless edge "
            "descriptor"
        )
    parameters = _control_plane_parameters(
        config,
        primary_state_table_name=primary_state_table_name,
        runtime_state_table_name=runtime_state_table_name,
        recovery_approval_id=recovery_approval_id,
        deployment_transition_id=deployment_transition_id,
        application_state=application_state,
        deployment_namespace=names.namespace,
        rehearsal_control_table_arn=rehearsal_control_table_arn,
    )
    if serverless_edge is not None:
        parameters.update(
            serverless_edge.control_plane_parameters(
                backend_mode=edge_backend_mode,
                migration_id=edge_migration_id,
            )
        )
    for name, value in parameters.items():
        command.extend(
            _parameter(
                name,
                value,
                stack=names.control_plane_stack,
            )
        )
    control_plane = config.control_plane
    if control_plane is None:
        raise AgentCoreDeploymentError("control-plane settings are missing")
    if control_plane.scim_tenants_secret_arn is not None:
        command.extend(
            [
                "-c",
                (f"scim_tenants_secret_arn={control_plane.scim_tenants_secret_arn}"),
            ]
        )
    _append_athena_contexts(command, config)
    command.extend(
        [
            "--require-approval",
            "never" if assume_yes else "broadening",
            "--outputs-file",
            str(outputs_file),
        ]
    )
    return command


def serverless_control_plane_deploy_command(
    config: AgentCoreSetupConfig,
    identity: IdentityValues,
    application_state: ApplicationStateValues,
    artifacts: ServerlessControlArtifactValues,
    workers: ServerlessWorkersValues,
    *,
    aws_identity: AwsIdentity,
    outputs_file: Path,
    assume_yes: bool,
    deployment_namespace: str | None = None,
    production_edge: ProductionEdgeValues | None = None,
) -> list[str]:
    """Build a serverless control-plane command without executing it."""

    names = (
        _current_deployment_names()
        if deployment_namespace is None
        else deployment_names(deployment_namespace)
    )
    if config.identity_mode != MANAGED_COGNITO:
        raise AgentCoreDeploymentError(
            "serverless control plane requires managed Cognito"
        )
    control_plane = config.control_plane
    if control_plane is None or control_plane.endpoint_mode != CLOUDFRONT:
        raise AgentCoreDeploymentError(
            "serverless control plane currently requires CloudFront mode"
        )
    if (
        identity.user_pool_id is None
        or identity.hosted_ui_domain is None
    ):
        raise AgentCoreDeploymentError(
            "serverless control plane requires complete managed identity "
            "outputs"
        )
    if application_state.stack_name != names.application_state_stack:
        raise AgentCoreDeploymentError(
            "application-state descriptor does not match the serverless "
            "control-plane namespace"
        )
    if workers.stack_name != names.serverless_workers_stack:
        raise AgentCoreDeploymentError(
            "serverless-workers descriptor does not match the control-plane "
            "namespace"
        )
    if production_edge is not None:
        if production_edge.stack_name != names.control_plane_stack:
            raise AgentCoreDeploymentError(
                "production edge descriptor does not match the serverless "
                "control-plane namespace"
            )
        if (
            production_edge.state_table_name
            != application_state.state_table_name
        ):
            raise AgentCoreDeploymentError(
                "production edge and serverless control plane do not share "
                "canonical state"
            )
    artifacts.validate(
        identity=aws_identity,
        region=config.aws_region,
    )
    hosted_ui = urlsplit(identity.hosted_ui_domain)
    if (
        hosted_ui.scheme != "https"
        or hosted_ui.hostname is None
        or hosted_ui.path not in {"", "/"}
        or hosted_ui.query
        or hosted_ui.fragment
    ):
        raise AgentCoreDeploymentError(
            "managed Cognito hosted UI output is invalid"
        )
    runtime_state_table = (
        ""
        if application_state.selected_state_table_name
        == application_state.state_table_name
        else application_state.selected_state_table_name
    )
    parameters = {
        **application_state.control_plane_parameters(
            selected_state_table_name=(
                application_state.selected_state_table_name
            )
        ),
        **artifacts.parameters(),
        **workers.control_plane_parameters(),
        "AllowedViewerCidrs": ",".join(
            control_plane.allowed_viewer_cidrs
        ),
        "IdentityHostedUiDomainName": hosted_ui.hostname,
        "IdentityOidcIssuer": identity.issuer,
        "IdentityUserPoolId": identity.user_pool_id,
        "OidcProjectClaim": identity.project_claim,
        "OidcTenantClaim": identity.tenant_claim,
        "PrimaryStateTableName": application_state.state_table_name,
        "RuntimeStateTableName": runtime_state_table,
    }
    if production_edge is not None:
        parameters.update(
            production_edge.serverless_attachment_parameters()
        )
    command = [
        str(_cdk_cli_path()),
        "deploy",
        names.serverless_control_plane_stack,
        "-c",
        "deployment_target=serverless-control-plane",
        "-c",
        f"region={config.aws_region}",
    ]
    command.extend(_deployment_context_arguments(names.namespace))
    if production_edge is not None:
        command.extend(["-c", "edge_attachment_enabled=true"])
    if control_plane.scim_tenants_secret_arn is not None:
        command.extend(
            [
                "-c",
                (
                    "scim_tenants_secret_arn="
                    f"{control_plane.scim_tenants_secret_arn}"
                ),
            ]
        )
    for name, value in parameters.items():
        command.extend(
            _parameter(
                name,
                value,
                stack=names.serverless_control_plane_stack,
            )
        )
    command.extend(
        [
            "--require-approval",
            "never" if assume_yes else "broadening",
            "--outputs-file",
            str(outputs_file),
        ]
    )
    return command


def serverless_workers_deploy_command(
    config: AgentCoreSetupConfig,
    application_state: ApplicationStateValues,
    artifacts: ServerlessControlArtifactValues,
    *,
    aws_identity: AwsIdentity,
    outputs_file: Path,
    assume_yes: bool,
    deployment_namespace: str | None = None,
) -> list[str]:
    """Build a serverless-workers command without executing it."""

    names = (
        _current_deployment_names()
        if deployment_namespace is None
        else deployment_names(deployment_namespace)
    )
    if application_state.stack_name != names.application_state_stack:
        raise AgentCoreDeploymentError(
            "application-state descriptor does not match the serverless "
            "workers namespace"
        )
    artifacts.validate(
        identity=aws_identity,
        region=config.aws_region,
    )
    parameters = {
        **application_state.security_event_worker_parameters(),
        **application_state.query_reconciliation_parameters(),
        **artifacts.worker_parameters(),
    }
    command = [
        str(_cdk_cli_path()),
        "deploy",
        names.serverless_workers_stack,
        "-c",
        "deployment_target=serverless-workers",
        "-c",
        f"region={config.aws_region}",
    ]
    command.extend(_deployment_context_arguments(names.namespace))
    for name, value in parameters.items():
        command.extend(
            _parameter(
                name,
                value,
                stack=names.serverless_workers_stack,
            )
        )
    _append_athena_contexts(command, config)
    command.extend(
        [
            "--require-approval",
            "never" if assume_yes else "broadening",
            "--outputs-file",
            str(outputs_file),
        ]
    )
    return command


def cdk_bootstrap_command(
    config: AgentCoreSetupConfig,
    *,
    identity: AwsIdentity,
    execution_policy_arns: tuple[str, ...],
) -> list[str]:
    qualifier = bootstrap_qualifier_for_namespace(_ACTIVE_DEPLOYMENT_NAMESPACE)
    command = [
        str(_cdk_cli_path()),
        "bootstrap",
        f"aws://{identity.account_id}/{config.aws_region}",
        "-c",
        "deployment_target=identity",
        "-c",
        f"region={config.aws_region}",
    ]
    for policy_arn in execution_policy_arns:
        command.extend(
            [
                "--cloudformation-execution-policies",
                policy_arn,
            ]
        )
    command.extend(
        [
            "--custom-permissions-boundary",
            bootstrap_role_boundary_name(
                config.aws_region,
                qualifier=qualifier,
            ),
            "--qualifier",
            qualifier,
            "--termination-protection",
            "--toolkit-stack-name",
            bootstrap_toolkit_stack_name(qualifier),
        ]
    )
    command.extend(_deployment_context_arguments(_ACTIVE_DEPLOYMENT_NAMESPACE))
    return command


def _run_command(command: list[str], cwd: Path) -> None:
    INFRA_RUN_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="deployment-",
        dir=INFRA_RUN_ROOT,
    ) as temporary:
        run_root = Path(temporary)
        environment = os.environ.copy()
        scripts_dir = INFRA_TOOLS_ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin")
        environment["PATH"] = os.pathsep.join([str(scripts_dir), environment.get("PATH", "")])
        environment["CDK_OUTDIR"] = str(run_root / "cdk.out")
        environment["JSII_RUNTIME_PACKAGE_CACHE_ROOT"] = str(run_root / "jsii-cache")
        environment["PYTHONPYCACHEPREFIX"] = str(run_root / "pycache")
        try:
            subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            return_code = getattr(exc, "returncode", "unavailable")
            raise AgentCoreDeploymentError(f"command failed with exit code {return_code}: {command[0]}") from exc


def _stack_outputs(path: Path, stack_name: str) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentCoreDeploymentError(f"cannot read CDK outputs from {path}: {exc}") from exc
    outputs = payload.get(stack_name) if isinstance(payload, dict) else None
    if not isinstance(outputs, dict):
        raise AgentCoreDeploymentError(f"CDK outputs do not contain {stack_name}")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in outputs.items()):
        raise AgentCoreDeploymentError(f"{stack_name} outputs must be string values")
    return outputs


def _required_output(outputs: dict[str, str], name: str) -> str:
    value = outputs.get(name)
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise AgentCoreDeploymentError(f"deployment output {name} is missing or invalid")
    return value


def _validated_application_state_arn(
    value: str,
    *,
    identity: AwsIdentity,
    region: str,
    service: str,
    resource_pattern: str,
    location: str,
    regional: bool = True,
) -> str:
    parts = value.split(":", 5)
    expected_region = region if regional else ""
    if (
        len(parts) != 6
        or parts[:2] != ["arn", identity.partition]
        or parts[2] != service
        or parts[3] != expected_region
        or parts[4] != identity.account_id
        or re.fullmatch(resource_pattern, parts[5]) is None
    ):
        raise AgentCoreDeploymentError(
            f"application-state output {location} is not bound to the "
            "deployment account and region"
        )
    return value


def _validated_state_queue_url(
    value: str,
    *,
    identity: AwsIdentity,
    region: str,
    location: str,
) -> str:
    suffix = "amazonaws.com.cn" if identity.partition == "aws-cn" else "amazonaws.com"
    parsed = urlsplit(value)
    expected_host = f"sqs.{region}.{suffix}"
    path_parts = parsed.path.removeprefix("/").split("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc != expected_host
        or parsed.query
        or parsed.fragment
        or len(path_parts) != 2
        or path_parts[0] != identity.account_id
        or re.fullmatch(r"[A-Za-z0-9_-]{1,75}\.fifo", path_parts[1])
        is None
    ):
        raise AgentCoreDeploymentError(
            f"application-state output {location} is not a FIFO queue URL "
            "in the deployment account and region"
        )
    return value


def application_state_values_from_outputs(
    outputs: Mapping[str, str],
    *,
    identity: AwsIdentity,
    region: str,
    expected_stack_name: str,
) -> ApplicationStateValues:
    """Validate the non-secret descriptor emitted by the state stack."""

    values = dict(outputs)
    stack_name = _required_output(values, "ApplicationStateStackName")
    if stack_name != expected_stack_name:
        raise AgentCoreDeploymentError(
            "application-state outputs are bound to an unexpected stack"
        )
    state_table_name = _required_output(values, "StateTableName")
    if re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", state_table_name) is None:
        raise AgentCoreDeploymentError(
            "application-state output StateTableName is invalid"
        )
    selected_state_table_name = _required_output(
        values,
        "SelectedRuntimeStateTableName",
    )
    if (
        selected_state_table_name != state_table_name
        and re.fullmatch(
            re.escape(state_table_name)
            + r"-restore-validation-[A-Za-z0-9_.-]{1,64}",
            selected_state_table_name,
        )
        is None
    ):
        raise AgentCoreDeploymentError(
            "application-state selected table is outside the recovery namespace"
        )

    data_key_arn = _validated_application_state_arn(
        _required_output(values, "DataKeyArn"),
        identity=identity,
        region=region,
        service="kms",
        resource_pattern=r"key/[0-9a-fA-F-]{36}",
        location="DataKeyArn",
    )
    routing_key_arn = _validated_application_state_arn(
        _required_output(values, "RoutingConfigSigningKeyArn"),
        identity=identity,
        region=region,
        service="kms",
        resource_pattern=r"key/[0-9a-fA-F-]{36}",
        location="RoutingConfigSigningKeyArn",
    )
    provider_secret_arn = _validated_application_state_arn(
        _required_output(values, "ProviderSecretArn"),
        identity=identity,
        region=region,
        service="secretsmanager",
        resource_pattern=r"secret:[A-Za-z0-9/_+=.@-]{1,512}",
        location="ProviderSecretArn",
    )
    outbox_queue_arn = _validated_application_state_arn(
        _required_output(values, "SecurityEventOutboxQueueArn"),
        identity=identity,
        region=region,
        service="sqs",
        resource_pattern=r"[A-Za-z0-9_-]{1,75}\.fifo",
        location="SecurityEventOutboxQueueArn",
    )
    dead_letter_queue_arn = _validated_application_state_arn(
        _required_output(values, "SecurityEventDeadLetterQueueArn"),
        identity=identity,
        region=region,
        service="sqs",
        resource_pattern=r"[A-Za-z0-9_-]{1,75}\.fifo",
        location="SecurityEventDeadLetterQueueArn",
    )
    security_event_topic_arn = _validated_application_state_arn(
        _required_output(values, "SecurityEventTopicArn"),
        identity=identity,
        region=region,
        service="sns",
        resource_pattern=r"[A-Za-z0-9_-]{1,251}\.fifo",
        location="SecurityEventTopicArn",
    )
    security_event_log_group_arn = _validated_application_state_arn(
        _required_output(values, "SecurityEventLogGroupArn"),
        identity=identity,
        region=region,
        service="logs",
        resource_pattern=r"log-group:[A-Za-z0-9._/#-]{1,512}",
        location="SecurityEventLogGroupArn",
    )
    backup_vault_arn = _validated_application_state_arn(
        _required_output(values, "StateBackupVaultArn"),
        identity=identity,
        region=region,
        service="backup",
        resource_pattern=r"backup-vault:[A-Za-z0-9._-]{2,50}",
        location="StateBackupVaultArn",
    )
    backup_role_arn = _validated_application_state_arn(
        _required_output(values, "StateBackupRoleArn"),
        identity=identity,
        region=region,
        service="iam",
        resource_pattern=r"role/[A-Za-z0-9+=,.@_/-]{1,512}",
        location="StateBackupRoleArn",
        regional=False,
    )
    return ApplicationStateValues(
        stack_name=stack_name,
        state_table_name=state_table_name,
        selected_state_table_name=selected_state_table_name,
        data_key_arn=data_key_arn,
        routing_config_signing_key_arn=routing_key_arn,
        provider_secret_arn=provider_secret_arn,
        event_outbox_queue_url=_validated_state_queue_url(
            _required_output(values, "SecurityEventOutboxQueueUrl"),
            identity=identity,
            region=region,
            location="SecurityEventOutboxQueueUrl",
        ),
        event_outbox_queue_arn=outbox_queue_arn,
        event_dead_letter_queue_url=_validated_state_queue_url(
            _required_output(
                values,
                "SecurityEventDeadLetterQueueUrl",
            ),
            identity=identity,
            region=region,
            location="SecurityEventDeadLetterQueueUrl",
        ),
        event_dead_letter_queue_arn=dead_letter_queue_arn,
        security_event_topic_arn=security_event_topic_arn,
        security_event_log_group_arn=security_event_log_group_arn,
        backup_vault_arn=backup_vault_arn,
        backup_role_arn=backup_role_arn,
    )


def serverless_workers_values_from_outputs(
    outputs: Mapping[str, str],
    *,
    identity: AwsIdentity,
    region: str,
    expected_stack_name: str,
) -> ServerlessWorkersValues:
    """Validate the non-secret export handoff emitted by worker CDK."""

    values = dict(outputs)
    stack_name = _required_output(
        values,
        "ServerlessWorkersStackName",
    )
    if stack_name != expected_stack_name:
        raise AgentCoreDeploymentError(
            "serverless-workers outputs are bound to an unexpected stack"
        )
    queue_arn = _validated_application_state_arn(
        _required_output(values, "ExportQueueArn"),
        identity=identity,
        region=region,
        service="sqs",
        resource_pattern=r"[A-Za-z0-9_-]{1,75}\.fifo",
        location="ExportQueueArn",
    )
    queue_url = _validated_state_queue_url(
        _required_output(values, "ExportQueueUrl"),
        identity=identity,
        region=region,
        location="ExportQueueUrl",
    )
    if queue_arn.rsplit(":", 1)[-1] != queue_url.rsplit("/", 1)[-1]:
        raise AgentCoreDeploymentError(
            "serverless-workers export queue ARN and URL do not match"
        )
    bucket_name = _required_output(values, "ExportBucketName")
    if (
        re.fullmatch(
            r"[a-z0-9](?:[a-z0-9.-]{1,61}[a-z0-9])?",
            bucket_name,
        )
        is None
        or ".." in bucket_name
        or re.fullmatch(r"[0-9.]+", bucket_name)
    ):
        raise AgentCoreDeploymentError(
            "serverless-workers export bucket name is invalid"
        )
    bucket_arn = _required_output(values, "ExportBucketArn")
    expected_bucket_arn = (
        f"arn:{identity.partition}:s3:::{bucket_name}"
    )
    if bucket_arn != expected_bucket_arn:
        raise AgentCoreDeploymentError(
            "serverless-workers export bucket ARN does not match its name"
        )
    return ServerlessWorkersValues(
        stack_name=stack_name,
        export_queue_url=queue_url,
        export_queue_arn=queue_arn,
        export_bucket_name=bucket_name,
        export_bucket_arn=bucket_arn,
    )


def production_edge_values_from_outputs(
    outputs: Mapping[str, str],
    *,
    identity: AwsIdentity,
    expected_stack_name: str,
) -> ProductionEdgeValues:
    """Validate the existing CloudFront edge without reading secrets."""

    values = dict(outputs)
    if values.get("EndpointMode") != CLOUDFRONT:
        raise AgentCoreDeploymentError(
            "production edge outputs are not in CloudFront mode"
        )
    if values.get("ControlPlaneAuthMode") != "application-oidc":
        raise AgentCoreDeploymentError(
            "production edge outputs have an unexpected authentication mode"
        )
    distribution_id = _required_output(values, "DistributionId")
    if re.fullmatch(r"[A-Z0-9]{13,32}", distribution_id) is None:
        raise AgentCoreDeploymentError(
            "production edge distribution ID is invalid"
        )
    distribution_arn = (
        f"arn:{identity.partition}:cloudfront::"
        f"{identity.account_id}:distribution/{distribution_id}"
    )
    emitted_arn = values.get("ProductionDistributionArn")
    if emitted_arn is not None and emitted_arn != distribution_arn:
        raise AgentCoreDeploymentError(
            "production edge distribution ARN does not match its account "
            "and ID"
        )
    hostname = _required_output(values, "DistributionDomainName")
    if (
        re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
            r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+",
            hostname,
        )
        is None
        or not hostname.endswith(".cloudfront.net")
        or values.get("ControlPlaneUrl") != f"https://{hostname}"
    ):
        raise AgentCoreDeploymentError(
            "production edge hostname is invalid"
        )
    web_acl_arn = _required_output(values, "WebAclArn")
    expected_waf_prefix = (
        f"arn:{identity.partition}:wafv2:us-east-1:"
        f"{identity.account_id}:global/webacl/"
    )
    if not web_acl_arn.startswith(expected_waf_prefix):
        raise AgentCoreDeploymentError(
            "production edge WebACL is not bound to the deployment account"
        )
    return ProductionEdgeValues(
        stack_name=expected_stack_name,
        distribution_id=distribution_id,
        distribution_arn=distribution_arn,
        hostname=hostname,
        state_table_name=_required_output(
            values,
            "PrimaryStateTableName",
        ),
        browser_client_id=_required_output(
            values,
            "BrowserClientId",
        ),
        web_acl_arn=web_acl_arn,
    )


def serverless_edge_values_from_outputs(
    outputs: Mapping[str, str],
    *,
    identity: AwsIdentity,
    region: str,
    expected_stack_name: str,
    production_edge: ProductionEdgeValues,
    artifacts: ServerlessControlArtifactValues,
) -> ServerlessEdgeValues:
    """Validate qualified serverless origins against reviewed receipts."""

    values = dict(outputs)
    if (
        values.get("EndpointMode") != CLOUDFRONT
        or values.get("ControlPlaneAuthMode") != "application-oidc"
    ):
        raise AgentCoreDeploymentError(
            "serverless edge outputs have an unexpected endpoint or "
            "authentication mode"
        )
    expected_bindings = {
        "ProductionDistributionArn": (
            production_edge.distribution_arn
        ),
        "ProductionDistributionId": (
            production_edge.distribution_id
        ),
        "ProductionControlPlaneHostname": production_edge.hostname,
        "PrimaryStateTableName": production_edge.state_table_name,
        "SourceRevision": artifacts.source_revision,
        "ControlApiArtifactSha256": artifacts.control_api_sha256,
        "StaticAssetsSha256": artifacts.static_assets_sha256,
    }
    mismatched = sorted(
        name
        for name, expected in expected_bindings.items()
        if values.get(name) != expected
    )
    if mismatched:
        raise AgentCoreDeploymentError(
            "serverless edge outputs do not match reviewed production "
            "bindings: "
            + ", ".join(mismatched)
        )
    api_domain = _required_output(
        values,
        "ControlApiOriginDomainName",
    )
    suffix = (
        "amazonaws.com.cn"
        if identity.partition == "aws-cn"
        else "amazonaws.com"
    )
    if (
        re.fullmatch(
            rf"[a-z0-9]+\.execute-api\.{re.escape(region)}\."
            rf"{re.escape(suffix)}",
            api_domain,
        )
        is None
    ):
        raise AgentCoreDeploymentError(
            "serverless control API origin is not in the deployment region"
        )
    origin_path = _required_output(
        values,
        "ControlApiOriginPath",
    )
    if re.fullmatch(r"/[A-Za-z0-9_-]{1,128}", origin_path) is None:
        raise AgentCoreDeploymentError(
            "serverless control API origin path is invalid"
        )
    origin_secret = _validated_application_state_arn(
        _required_output(values, "OriginCredentialSecretArn"),
        identity=identity,
        region=region,
        service="secretsmanager",
        resource_pattern=r"secret:[A-Za-z0-9/_+=.@-]{1,512}",
        location="OriginCredentialSecretArn",
    )
    static_domain = _required_output(
        values,
        "StaticSiteBucketRegionalDomainName",
    )
    if (
        re.fullmatch(
            rf"[a-z0-9][a-z0-9.-]{{1,61}}[a-z0-9]\.s3\."
            rf"{re.escape(region)}\.{re.escape(suffix)}",
            static_domain,
        )
        is None
    ):
        raise AgentCoreDeploymentError(
            "serverless static bucket origin is not in the deployment region"
        )
    qualification_url = _required_output(values, "ControlPlaneUrl")
    parsed = urlsplit(qualification_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.hostname == production_edge.hostname
        or values.get("ControlPlaneDomainName") != parsed.hostname
    ):
        raise AgentCoreDeploymentError(
            "serverless qualification URL is invalid or aliases production"
        )
    qualification_distribution_id = _required_output(
        values,
        "DistributionId",
    )
    if (
        re.fullmatch(
            r"[A-Z0-9]{13,32}",
            qualification_distribution_id,
        )
        is None
        or qualification_distribution_id
        == production_edge.distribution_id
    ):
        raise AgentCoreDeploymentError(
            "serverless qualification distribution ID is invalid"
        )
    return ServerlessEdgeValues(
        stack_name=expected_stack_name,
        production_stack_name=production_edge.stack_name,
        production_distribution_id=(
            production_edge.distribution_id
        ),
        production_distribution_arn=(
            production_edge.distribution_arn
        ),
        production_hostname=production_edge.hostname,
        qualification_distribution_id=(
            qualification_distribution_id
        ),
        qualification_url=qualification_url,
        state_table_name=production_edge.state_table_name,
        source_revision=artifacts.source_revision,
        control_api_sha256=artifacts.control_api_sha256,
        static_assets_sha256=artifacts.static_assets_sha256,
        control_api_domain_name=api_domain,
        control_api_origin_path=origin_path,
        origin_credential_secret_arn=origin_secret,
        static_bucket_domain_name=static_domain,
    )


def managed_identity_from_outputs(
    outputs: dict[str, str],
    *,
    expected_endpoint_mode: str | None = None,
) -> IdentityValues:
    endpoint_mode = outputs.get("EndpointMode", CUSTOM_DOMAIN)
    if endpoint_mode not in {CUSTOM_DOMAIN, CLOUDFRONT}:
        raise AgentCoreDeploymentError(
            "managed identity emitted an unexpected endpoint mode"
        )
    if (
        expected_endpoint_mode is not None
        and endpoint_mode != expected_endpoint_mode
    ):
        raise AgentCoreDeploymentError(
            "managed identity endpoint mode does not match the reviewed setup"
        )
    tenant_claim = _required_output(outputs, "TenantClaimName")
    project_claim = _required_output(outputs, "ProjectClaimName")
    if tenant_claim != DEFAULT_TENANT_CLAIM:
        raise AgentCoreDeploymentError("managed identity emitted an unexpected tenant claim name")
    if project_claim != DEFAULT_PROJECT_CLAIM:
        raise AgentCoreDeploymentError("managed identity emitted an unexpected project claim name")
    client_id = _required_output(outputs, "OidcClientId")
    certification_client_id = _required_output(
        outputs,
        "CertificationClientId",
    )
    audience = _required_output(outputs, "OidcAudience")
    if client_id != audience:
        raise AgentCoreDeploymentError("managed Cognito client and ID-token audience must match")
    if certification_client_id == client_id:
        raise AgentCoreDeploymentError("managed Cognito certification client must be distinct")
    issuer = _required_output(outputs, "OidcIssuer")
    discovery_url = _required_output(outputs, "OidcDiscoveryUrl")
    if discovery_url != f"{issuer}/.well-known/openid-configuration":
        raise AgentCoreDeploymentError("managed identity discovery URL does not match its issuer")
    hosted_ui = _required_output(outputs, "HostedUiDomain")
    if not issuer.startswith("https://") or not hosted_ui.startswith("https://"):
        raise AgentCoreDeploymentError("managed Cognito identity outputs must use HTTPS")
    return IdentityValues(
        issuer=issuer,
        discovery_url=discovery_url,
        client_id=client_id,
        audience=audience,
        tenant_claim=tenant_claim,
        project_claim=project_claim,
        user_pool_id=_required_output(outputs, "UserPoolId"),
        hosted_ui_domain=hosted_ui,
        certification_client_id=certification_client_id,
    )


def external_identity(config: AgentCoreSetupConfig) -> IdentityValues:
    if config.identity_mode != EXTERNAL_OIDC or config.external_oidc is None:
        raise AgentCoreDeploymentError("external OIDC settings are missing")
    oidc = config.external_oidc
    return IdentityValues(
        issuer=oidc.issuer,
        discovery_url=oidc.discovery_url,
        client_id=oidc.client_id,
        audience=oidc.audience,
        tenant_claim=oidc.tenant_claim,
        project_claim=oidc.project_claim,
    )


def _aws_error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if not isinstance(error, dict):
        return None
    code = error.get("Code")
    return code if isinstance(code, str) else None


def _aws_error_message(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if not isinstance(error, dict):
        return None
    message = error.get("Message")
    return message if isinstance(message, str) else None


def _existing_stack(
    cloudformation_client: Any,
    stack_name: str,
    *,
    allow_failed_creation: bool = False,
) -> dict[str, Any] | None:
    """Return one stable successful stack, or None before first deployment."""
    try:
        response = cloudformation_client.describe_stacks(
            StackName=stack_name,
        )
    except Exception as exc:
        if _aws_error_code(exc) == "ValidationError":
            return None
        raise AgentCoreDeploymentError(f"could not inspect the existing {stack_name} stack") from exc
    stacks = response.get("Stacks")
    if not isinstance(stacks, list) or len(stacks) != 1:
        raise AgentCoreDeploymentError(f"CloudFormation returned an ambiguous {stack_name} stack")
    stack = stacks[0]
    if not isinstance(stack, dict):
        raise AgentCoreDeploymentError(f"CloudFormation returned a malformed {stack_name} stack")
    status = stack.get("StackStatus")
    stable_statuses = {
        "CREATE_COMPLETE",
        "IMPORT_COMPLETE",
        "IMPORT_ROLLBACK_COMPLETE",
        "UPDATE_COMPLETE",
        "UPDATE_ROLLBACK_COMPLETE",
    }
    if allow_failed_creation:
        stable_statuses.update(
            {
                "CREATE_FAILED",
                "DELETE_FAILED",
                "ROLLBACK_COMPLETE",
                "ROLLBACK_FAILED",
            }
        )
    if not isinstance(status, str) or status not in stable_statuses:
        raise AgentCoreDeploymentError(f"the existing {stack_name} stack is not in a stable successful state")
    return stack


def _required_stack_id(
    stack: Mapping[str, Any],
    stack_name: str,
) -> str:
    stack_id = stack.get("StackId")
    if (
        not isinstance(stack_id, str)
        or not stack_id.startswith("arn:")
        or any(character.isspace() for character in stack_id)
    ):
        raise AgentCoreDeploymentError(f"{stack_name} has no stable stack ID")
    return stack_id


def _outputs_from_stack(
    stack: Mapping[str, Any],
    stack_name: str,
) -> dict[str, str]:
    raw_outputs = stack.get("Outputs", [])
    if not isinstance(raw_outputs, list):
        raise AgentCoreDeploymentError(f"the existing {stack_name} stack outputs are malformed")
    outputs: dict[str, str] = {}
    for item in raw_outputs:
        if not isinstance(item, dict):
            raise AgentCoreDeploymentError(f"the existing {stack_name} stack outputs are malformed")
        name = item.get("OutputKey")
        value = item.get("OutputValue")
        if not isinstance(name, str) or not isinstance(value, str) or name in outputs:
            raise AgentCoreDeploymentError(f"the existing {stack_name} stack outputs are malformed")
        outputs[name] = value
    return outputs


def _existing_stack_outputs(
    cloudformation_client: Any,
    stack_name: str,
) -> dict[str, str] | None:
    """Return stable existing outputs, or None when this is a first deployment."""
    stack = _existing_stack(cloudformation_client, stack_name)
    return None if stack is None else _outputs_from_stack(stack, stack_name)


def _existing_agentcore_outputs(
    cloudformation_client: Any,
) -> dict[str, str] | None:
    return _existing_stack_outputs(
        cloudformation_client,
        AGENTCORE_STACK,
    )


def _production_runtime_version(
    outputs: Mapping[str, str],
) -> str:
    """Return the exact live production version, including legacy stacks."""
    if "RuntimeEndpointArn" not in outputs:
        return ""
    value = outputs.get(
        "ProductionRuntimeVersion",
        outputs.get("RuntimeVersion", ""),
    )
    if not isinstance(value, str) or not value.isdigit() or value.startswith("0"):
        raise AgentCoreDeploymentError("the existing production endpoint has no valid runtime version")
    return value


def _verify_confirmed_alarm_subscription(
    session: Any,
    *,
    config: AgentCoreSetupConfig,
    outputs: Mapping[str, str],
) -> None:
    topic_arn = _required_output(
        dict(outputs),
        "AlarmTopicArn",
    )
    try:
        client = session.client(
            "sns",
            region_name=config.aws_region,
        )
        subscriptions: list[object] = []
        next_token: str | None = None
        for _ in range(100):
            arguments: dict[str, str] = {"TopicArn": topic_arn}
            if next_token is not None:
                arguments["NextToken"] = next_token
            response = client.list_subscriptions_by_topic(**arguments)
            page = response.get("Subscriptions")
            if not isinstance(page, list):
                raise AgentCoreDeploymentError("SNS returned malformed alarm subscription metadata")
            subscriptions.extend(page)
            raw_next_token = response.get("NextToken")
            if raw_next_token is None:
                break
            if not isinstance(raw_next_token, str) or not raw_next_token or raw_next_token == next_token:
                raise AgentCoreDeploymentError("SNS returned malformed alarm subscription pagination")
            next_token = raw_next_token
        else:
            raise AgentCoreDeploymentError("SNS alarm subscription pagination exceeded its safety limit")
    except AgentCoreDeploymentError:
        raise
    except Exception as exc:
        raise AgentCoreDeploymentError("could not verify the production alarm subscription") from exc

    for subscription in subscriptions:
        if not isinstance(subscription, dict):
            raise AgentCoreDeploymentError("SNS returned malformed alarm subscription metadata")
        if (
            subscription.get("TopicArn") == topic_arn
            and subscription.get("Protocol") == "email"
            and subscription.get("Endpoint") == config.admin.email
        ):
            subscription_arn = subscription.get("SubscriptionArn")
            if isinstance(subscription_arn, str) and subscription_arn.startswith("arn:"):
                return
    raise AgentCoreDeploymentError(
        "production alarm email is not confirmed; confirm the SNS "
        f"subscription for {config.admin.email} and rerun deployment"
    )


def _write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                dict(value),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _provider_environment(
    env_file: Path | None,
) -> dict[str, str]:
    values = load_provider_environment_file(env_file) if env_file is not None else {}
    for field_name in ALLOWED_SECRET_FIELDS:
        if field_name in os.environ:
            values[field_name] = os.environ[field_name]
    return values


def _sync_provider_credentials(
    session: Any,
    *,
    config: AgentCoreSetupConfig,
    provider_environment: Mapping[str, str],
    secret_arn: str,
) -> ProviderSecretVersion:
    try:
        return synchronize_provider_secret(
            session.client(
                "secretsmanager",
                region_name=config.aws_region,
            ),
            secret_arn=secret_arn,
            environ=provider_environment,
            enabled_providers=config.runtime.enabled_providers,
        )
    except ProviderSecretError as exc:
        raise AgentCoreDeploymentError(str(exc)) from exc


def _rollback_provider_credentials(
    session: Any,
    *,
    config: AgentCoreSetupConfig,
    secret_arn: str,
    version_id: str,
) -> ProviderSecretVersion:
    try:
        return rollback_provider_secret(
            session.client(
                "secretsmanager",
                region_name=config.aws_region,
            ),
            secret_arn=secret_arn,
            version_id=version_id,
            enabled_providers=config.runtime.enabled_providers,
        )
    except ProviderSecretError as exc:
        raise AgentCoreDeploymentError(str(exc)) from exc


def _attributes(items: Any) -> dict[str, str]:
    if not isinstance(items, list):
        raise AgentCoreDeploymentError("Cognito returned malformed user attributes")
    attributes: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise AgentCoreDeploymentError("Cognito returned malformed user attributes")
        name = item.get("Name")
        value = item.get("Value")
        if not isinstance(name, str) or not isinstance(value, str) or name in attributes:
            raise AgentCoreDeploymentError("Cognito returned malformed or duplicate user attributes")
        attributes[name] = value
    return attributes


def _verify_managed_admin(
    user: dict[str, Any],
    *,
    tenant_id: str,
    project_id: str,
    email: str,
) -> str:
    if user.get("Enabled") is not True:
        raise AgentCoreDeploymentError("the managed Cognito administrator is disabled")
    if user.get("UserStatus") in {"ARCHIVED", "UNKNOWN", "RESET_REQUIRED"}:
        raise AgentCoreDeploymentError("the managed Cognito administrator has an unusable status")
    attributes = _attributes(user.get("UserAttributes"))
    expected = {
        "email": email,
        "email_verified": "true",
        DEFAULT_TENANT_CLAIM: tenant_id,
        DEFAULT_PROJECT_CLAIM: project_id,
    }
    mismatches = [name for name, expected_value in expected.items() if attributes.get(name) != expected_value]
    if mismatches:
        raise AgentCoreDeploymentError(
            "existing Cognito administrator has conflicting attributes: " + ", ".join(sorted(mismatches))
        )
    subject = attributes.get("sub")
    if not isinstance(subject, str) or not subject or subject != subject.strip():
        raise AgentCoreDeploymentError("managed Cognito administrator has no stable subject")
    return subject


def ensure_managed_cognito_admin(
    cognito_client: Any,
    *,
    user_pool_id: str,
    user_name: str,
    email: str,
    tenant_id: str,
    project_id: str,
    allow_create: bool = True,
) -> ManagedAdminResult:
    """Invite the first administrator once and verify every idempotent rerun."""
    created = False
    try:
        user = cognito_client.admin_get_user(
            UserPoolId=user_pool_id,
            Username=user_name,
        )
    except Exception as exc:
        if _aws_error_code(exc) != "UserNotFoundException":
            raise AgentCoreDeploymentError("could not resolve the managed Cognito administrator") from exc
        if not allow_create:
            raise AgentCoreDeploymentError(
                "the existing production Cognito administrator is missing; "
                "routine candidate staging will not create identity authority"
            ) from exc
        try:
            cognito_client.admin_create_user(
                UserPoolId=user_pool_id,
                Username=user_name,
                DesiredDeliveryMediums=["EMAIL"],
                UserAttributes=[
                    {"Name": "email", "Value": email},
                    {"Name": "email_verified", "Value": "true"},
                    {"Name": DEFAULT_TENANT_CLAIM, "Value": tenant_id},
                    {"Name": DEFAULT_PROJECT_CLAIM, "Value": project_id},
                ],
            )
            created = True
        except Exception as create_exc:
            if _aws_error_code(create_exc) != "UsernameExistsException":
                raise AgentCoreDeploymentError("could not invite the managed Cognito administrator") from create_exc
        try:
            user = cognito_client.admin_get_user(
                UserPoolId=user_pool_id,
                Username=user_name,
            )
        except Exception as get_exc:
            raise AgentCoreDeploymentError("invited Cognito administrator could not be resolved") from get_exc

    subject = _verify_managed_admin(
        user,
        tenant_id=tenant_id,
        project_id=project_id,
        email=email,
    )
    return ManagedAdminResult(subject=subject, created=created)


@contextmanager
def _bootstrap_environment(region: str):
    updates = {
        "AWS_DEFAULT_REGION": region,
        "LLM_ROUTER_DYNAMODB_ENABLED": "true",
    }
    previous = {name: os.environ.get(name) for name in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def bootstrap_canonical_admin(
    config: AgentCoreSetupConfig,
    *,
    table_name: str,
    issuer: str,
    subject: str,
) -> dict[str, object]:
    from src.gateway.auth.tenant_bootstrap import bootstrap_tenant
    from src.gateway.persistence import DynamoPersistence

    with _bootstrap_environment(config.aws_region):
        persistence = DynamoPersistence(
            table_name=table_name,
            region=config.aws_region,
        )
        result = asyncio.run(
            bootstrap_tenant(
                persistence,
                tenant_id=config.tenant.tenant_id,
                project_id=config.tenant.project_id,
                project_name=config.tenant.project_name,
                issuer=issuer,
                subject=subject,
                user_name=config.admin.user_name,
                display_name=config.admin.display_name,
                email=config.admin.email,
                budget_limit=config.tenant.budget_limit,
            )
        )
    return result.to_dict()


def verify_canonical_admin(
    config: AgentCoreSetupConfig,
    *,
    table_name: str,
    issuer: str,
    subject: str,
) -> dict[str, object]:
    """Resolve existing production authority without performing any writes."""
    from src.gateway.auth.dynamo_principal_repository import (
        DynamoPrincipalRepository,
    )
    from src.gateway.auth.principal import CredentialIdentity
    from src.gateway.models import (
        AuthMethod,
        MembershipStatus,
        TenantRole,
    )
    from src.gateway.persistence import DynamoPersistence

    async def verify() -> dict[str, object]:
        persistence = DynamoPersistence(
            table_name=table_name,
            region=config.aws_region,
        )
        project = await persistence.get_project(
            config.tenant.project_id,
            config.tenant.tenant_id,
        )
        if (
            project is None
            or project.tenant_id != config.tenant.tenant_id
            or project.project_id != config.tenant.project_id
            or project.name != config.tenant.project_name
            or project.budget_limit != config.tenant.budget_limit
        ):
            raise AgentCoreDeploymentError("existing canonical project differs from the reviewed setup")
        principal = await DynamoPrincipalRepository(persistence).resolve(
            CredentialIdentity(
                issuer=issuer,
                subject=subject,
                auth_method=AuthMethod.OIDC_JWT,
                tenant_hint=config.tenant.tenant_id,
                project_hint=config.tenant.project_id,
            )
        )
        if (
            principal is None
            or principal.membership_status is not MembershipStatus.ACTIVE
            or principal.roles != frozenset({TenantRole.TENANT_ADMIN})
            or principal.tenant_id != config.tenant.tenant_id
            or principal.issuer != issuer
            or principal.subject != subject
            or config.tenant.project_id not in principal.project_ids
            or principal.principal_id not in project.members
            or principal.email != config.admin.email
        ):
            raise AgentCoreDeploymentError("existing canonical administrator differs from the reviewed setup")
        return {
            "principal_id": principal.principal_id,
            "project_id": project.project_id,
            "project_revision": project.revision,
            "verified_read_only": True,
        }

    with _bootstrap_environment(config.aws_region):
        return asyncio.run(verify())


def _ensure_canonical_admin(
    config: AgentCoreSetupConfig,
    *,
    table_name: str,
    issuer: str,
    subject: str,
    production_runtime_version: str,
) -> dict[str, Any]:
    if not production_runtime_version:
        print("Creating or verifying canonical tenant authority...")
        return bootstrap_canonical_admin(
            config,
            table_name=table_name,
            issuer=issuer,
            subject=subject,
        )
    print("Verifying canonical tenant authority without updating it...")
    return verify_canonical_admin(
        config,
        table_name=table_name,
        issuer=issuer,
        subject=subject,
    )


def _assert_deployment_prerequisites() -> None:
    if shutil.which("npm") is None:
        raise AgentCoreDeploymentError("npm is required for CDK deployment")
    try:
        version = subprocess.run(
            ["node", "--version"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ).stdout.strip()
        major = int(version.removeprefix("v").split(".", 1)[0])
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise AgentCoreDeploymentError("Node.js 22 or newer is required for CDK deployment") from exc
    if major < 22:
        raise AgentCoreDeploymentError(f"Node.js 22 or newer is required; found {version}")
    _ensure_cdk_environment()


def _managed_identity_for_candidate(
    config: AgentCoreSetupConfig,
    *,
    cloudformation_client: Any,
    boto3_session: Any,
    outputs_dir: Path,
    assume_yes: bool,
    runner: CommandRunner,
    existing_runtime: bool,
) -> tuple[IdentityValues, str]:
    identity_output_path = outputs_dir / "identity-outputs.json"
    identity_stack = _existing_stack(
        cloudformation_client,
        IDENTITY_STACK,
    )
    if identity_stack is None:
        if existing_runtime:
            raise AgentCoreDeploymentError("managed identity stack is missing for the existing production runtime")
        identity_output_path.unlink(missing_ok=True)
        print("Deploying retained managed Cognito identity...")
        runner(
            identity_deploy_command(
                config,
                outputs_file=identity_output_path,
                assume_yes=assume_yes,
            ),
            INFRA_ROOT,
        )
        identity_outputs = _stack_outputs(
            identity_output_path,
            IDENTITY_STACK,
        )
    else:
        _validate_stack_parameters(
            identity_stack,
            _identity_parameters(
                config,
                deployment_namespace=(_ACTIVE_DEPLOYMENT_NAMESPACE),
            ),
            stack_name=IDENTITY_STACK,
        )
        if config.control_plane is None:
            raise AgentCoreDeploymentError(
                "managed Cognito control-plane settings are missing"
            )
        _validate_stack_endpoint_mode(
            identity_stack,
            expected_endpoint_mode=config.control_plane.endpoint_mode,
            stack_name=IDENTITY_STACK,
        )
        identity_outputs = _outputs_from_stack(
            identity_stack,
            IDENTITY_STACK,
        )
        _write_private_json(
            identity_output_path,
            {IDENTITY_STACK: identity_outputs},
        )
        print("Verified retained managed Cognito identity without updating it.")

    if config.control_plane is None:
        raise AgentCoreDeploymentError(
            "managed Cognito control-plane settings are missing"
        )
    identity = managed_identity_from_outputs(
        identity_outputs,
        expected_endpoint_mode=config.control_plane.endpoint_mode,
    )
    if identity.user_pool_id is None:
        raise AgentCoreDeploymentError("managed identity did not return a user pool")
    cognito_client = boto3_session.client(
        "cognito-idp",
        region_name=config.aws_region,
    )
    admin = ensure_managed_cognito_admin(
        cognito_client,
        user_pool_id=identity.user_pool_id,
        user_name=config.admin.user_name,
        email=config.admin.email,
        tenant_id=config.tenant.tenant_id,
        project_id=config.tenant.project_id,
        allow_create=not existing_runtime,
    )
    action = "invited" if admin.created else "verified"
    print(f"Managed Cognito administrator {action}.")
    return identity, admin.subject


def deploy(
    config: AgentCoreSetupConfig,
    *,
    outputs_dir: Path,
    assume_yes: bool,
    bootstrap_cdk: bool,
    runner: CommandRunner = _run_command,
    boto3_session: Any | None = None,
    provider_environment: Mapping[str, str] | None = None,
    provider_secret_rollback_version: str | None = None,
    rehearsal_control_table_arn: str | None = None,
) -> None:
    _assert_deployment_prerequisites()
    rehearsal_control_table_arn = validate_rehearsal_control_table_arn(
        aws_region=config.aws_region,
        deployment_namespace=_ACTIVE_DEPLOYMENT_NAMESPACE,
        rehearsal_control_table_arn=(rehearsal_control_table_arn),
    )
    provider_environment = dict(os.environ if provider_environment is None else provider_environment)
    if provider_secret_rollback_version is None:
        try:
            collect_provider_secret(
                provider_environment,
                config.runtime.enabled_providers,
            )
        except ProviderSecretError as exc:
            raise AgentCoreDeploymentError(str(exc)) from exc

    outputs_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.chmod(0o700)
    if boto3_session is None:
        import boto3

        boto3_session = boto3.Session(region_name=config.aws_region)
    cloudformation_client = boto3_session.client(
        "cloudformation",
        region_name=config.aws_region,
    )
    existing_outputs = _existing_agentcore_outputs(cloudformation_client)
    production_runtime_version = "" if existing_outputs is None else _production_runtime_version(existing_outputs)
    if production_runtime_version:
        _validate_shared_runtime_configuration(
            config,
            existing_outputs,
        )
    candidate_endpoint_name = _new_candidate_endpoint_name()
    if provider_secret_rollback_version is not None and existing_outputs is None:
        raise AgentCoreDeploymentError("provider secret rollback requires an existing AgentCore stack")
    if existing_outputs is not None and existing_outputs.get("RecoveryCutoverMode") != "normal":
        raise AgentCoreDeploymentError(
            "AgentCore recovery is active; complete or abort it before running the first-adopter deployment"
        )

    _validate_prefix_list_inputs(
        boto3_session,
        config=config,
    )
    if existing_outputs is None:
        _assert_no_retained_runtime_without_stack(
            boto3_session,
            config=config,
        )
    if config.identity_mode == MANAGED_COGNITO:
        identity_stack = _existing_stack(
            cloudformation_client,
            IDENTITY_STACK,
        )
        if identity_stack is None:
            if existing_outputs is not None:
                raise AgentCoreDeploymentError(
                    f"{IDENTITY_STACK} is missing while {AGENTCORE_STACK} "
                    "exists; recover or import the retained identity stack "
                    "before deployment; no AWS resources were changed"
                )
            _assert_no_retained_identity_without_stack(
                boto3_session,
                config=config,
            )

    aws_identity, execution_policy_arns = _require_bootstrap_execution_policy(
        boto3_session,
        config=config,
        create_if_missing=bootstrap_cdk,
    )
    if bootstrap_cdk:
        print(f"Bootstrapping CDK in {config.aws_region}...")
        runner(
            cdk_bootstrap_command(
                config,
                identity=aws_identity,
                execution_policy_arns=execution_policy_arns,
            ),
            INFRA_ROOT,
        )
    _assert_cdk_execution_role_policy(
        boto3_session,
        config=config,
        identity=aws_identity,
        expected_policy_arns=execution_policy_arns,
    )

    if config.identity_mode == MANAGED_COGNITO:
        identity, subject = _managed_identity_for_candidate(
            config,
            cloudformation_client=cloudformation_client,
            boto3_session=boto3_session,
            outputs_dir=outputs_dir,
            assume_yes=assume_yes,
            runner=runner,
            existing_runtime=existing_outputs is not None,
        )
    else:
        identity = external_identity(config)
        if config.admin.subject is None:
            raise AgentCoreDeploymentError("external OIDC administrator subject is missing")
        subject = config.admin.subject

    runtime_output_path = outputs_dir / "agentcore-outputs.json"
    if existing_outputs is None:
        runtime_output_path.unlink(missing_ok=True)
        print("Creating the AgentCore runtime with its public endpoint held back...")
        runner(
            agentcore_deploy_command(
                config,
                identity,
                outputs_file=runtime_output_path,
                assume_yes=assume_yes,
                candidate_endpoint_name=candidate_endpoint_name,
                provider_secret_version="bootstrap",
                publish_candidate_endpoint=False,
                publish_production_endpoint=False,
                rehearsal_control_table_arn=(rehearsal_control_table_arn),
            ),
            INFRA_ROOT,
        )
        bootstrap_outputs = _stack_outputs(
            runtime_output_path,
            AGENTCORE_STACK,
        )
        if (
            _required_output(
                bootstrap_outputs,
                "RecoveryCutoverMode",
            )
            != "normal"
        ):
            raise AgentCoreDeploymentError("AgentCore recovery became active during bootstrap")
        secret_arn = _required_output(
            bootstrap_outputs,
            "ProviderSecretArn",
        )
    else:
        secret_arn = _required_output(
            existing_outputs,
            "ProviderSecretArn",
        )

    if provider_secret_rollback_version is None:
        print("Synchronizing the allowlisted provider credential secret...")
        secret_version = _sync_provider_credentials(
            boto3_session,
            config=config,
            provider_environment=provider_environment,
            secret_arn=secret_arn,
        )
    else:
        print("Rolling back to the reviewed provider secret version...")
        secret_version = _rollback_provider_credentials(
            boto3_session,
            config=config,
            secret_arn=secret_arn,
            version_id=provider_secret_rollback_version,
        )
    _write_private_json(
        outputs_dir / "provider-secret-version.json",
        secret_version.to_dict(),
    )
    print("Provider secret version synchronized for fields: " + ", ".join(secret_version.configured_fields))

    runtime_output_path.unlink(missing_ok=True)
    print("Deploying the credential-bound authenticated AgentCore runtime...")
    runner(
        agentcore_deploy_command(
            config,
            identity,
            outputs_file=runtime_output_path,
            assume_yes=assume_yes,
            candidate_endpoint_name=candidate_endpoint_name,
            provider_secret_version=secret_version.version_id,
            publish_candidate_endpoint=True,
            publish_production_endpoint=bool(production_runtime_version),
            production_runtime_version=production_runtime_version,
            rehearsal_control_table_arn=(rehearsal_control_table_arn),
        ),
        INFRA_ROOT,
    )
    runtime_outputs = _stack_outputs(runtime_output_path, AGENTCORE_STACK)
    recovery_mode = _required_output(
        runtime_outputs,
        "RecoveryCutoverMode",
    )
    if recovery_mode != "normal":
        raise AgentCoreDeploymentError(
            "AgentCore recovery is active; complete or abort it before running the first-adopter deployment"
        )
    if (
        _required_output(
            runtime_outputs,
            "ProviderSecretVersion",
        )
        != secret_version.version_id
    ):
        raise AgentCoreDeploymentError(
            "deployed AgentCore runtime is not bound to the synchronized provider secret version"
        )
    expected_providers = ",".join(config.runtime.enabled_providers)
    if _required_output(runtime_outputs, "EnabledProviders") != expected_providers:
        raise AgentCoreDeploymentError("deployed AgentCore provider allowlist does not match the reviewed setup")
    _validate_shared_runtime_configuration(
        config,
        runtime_outputs,
    )
    runtime_version = _required_output(
        runtime_outputs,
        "RuntimeVersion",
    )
    candidate_version = _required_output(
        runtime_outputs,
        "CandidateRuntimeVersion",
    )
    if candidate_version != runtime_version:
        raise AgentCoreDeploymentError("candidate endpoint is not bound to the current runtime version")
    if (
        _required_output(
            runtime_outputs,
            "CandidateRuntimeEndpointName",
        )
        != candidate_endpoint_name
    ):
        raise AgentCoreDeploymentError("candidate endpoint name is missing or invalid")
    _required_output(runtime_outputs, "CandidateRuntimeEndpointArn")
    _verify_confirmed_alarm_subscription(
        boto3_session,
        config=config,
        outputs=runtime_outputs,
    )
    if production_runtime_version:
        if (
            _required_output(
                runtime_outputs,
                "ProductionRuntimeVersion",
            )
            != production_runtime_version
        ):
            raise AgentCoreDeploymentError("candidate deployment changed the certified production runtime version")
        _required_output(runtime_outputs, "RuntimeEndpointArn")
    _required_output(runtime_outputs, "StateTableName")
    table_name = _required_output(
        runtime_outputs,
        "SelectedRuntimeStateTableName",
    )
    if not isinstance(runtime_outputs.get("RecoveryApprovalId"), str):
        raise AgentCoreDeploymentError("AgentCore recovery approval output is missing")

    result = _ensure_canonical_admin(
        config,
        table_name=table_name,
        issuer=identity.issuer,
        subject=subject,
        production_runtime_version=production_runtime_version,
    )
    print(f"Canonical administrator verified: {result['principal_id']} on {result['project_id']}.")
    if identity.hosted_ui_domain:
        print(f"Managed login: {identity.hosted_ui_domain}")
        print(f"OIDC client ID: {identity.client_id}")
    if config.identity_mode == MANAGED_COGNITO:
        print("Control-plane deployment is deferred until candidate certification succeeds.")
    else:
        print(
            "Web control plane: not deployed; external OIDC is currently "
            "supported on the AgentCore invocation surface only."
        )
    print(f"Runtime execution role: {_required_output(runtime_outputs, 'RuntimeExecutionRoleArn')}")
    print(f"Runtime ARN: {_required_output(runtime_outputs, 'RuntimeArn')}")
    print(f"Candidate runtime version: {candidate_version} (awaiting certification)")
    print(f"Deployment outputs: {outputs_dir}")


def _existing_identity(
    config: AgentCoreSetupConfig,
    cloudformation_client: Any,
) -> IdentityValues:
    if config.identity_mode == EXTERNAL_OIDC:
        return external_identity(config)
    outputs = _existing_stack_outputs(
        cloudformation_client,
        IDENTITY_STACK,
    )
    if outputs is None:
        raise AgentCoreDeploymentError("managed identity stack does not exist")
    if config.control_plane is None:
        raise AgentCoreDeploymentError(
            "managed Cognito control-plane settings are missing"
        )
    return managed_identity_from_outputs(
        outputs,
        expected_endpoint_mode=config.control_plane.endpoint_mode,
    )


def _control_plane_output_expectations(
    runtime_outputs: Mapping[str, str],
) -> dict[str, str]:
    return {
        "AgentCoreStackName": AGENTCORE_STACK,
        "PrimaryStateTableName": _required_output(
            dict(runtime_outputs),
            "StateTableName",
        ),
        "SelectedRuntimeStateTableName": _required_output(
            dict(runtime_outputs),
            "SelectedRuntimeStateTableName",
        ),
        "RecoveryCutoverMode": "normal",
        "RecoveryApprovalId": runtime_outputs.get(
            "RecoveryApprovalId",
            "",
        ),
    }


def _validate_control_plane_outputs(
    outputs: Mapping[str, str],
    *,
    runtime_outputs: Mapping[str, str],
    expected_image: str | None,
    expected_endpoint_mode: str,
) -> None:
    expected = _control_plane_output_expectations(runtime_outputs)
    actual = {name: outputs.get(name) for name in expected}
    if actual != expected:
        raise AgentCoreDeploymentError(
            f"control-plane recovery outputs do not match AgentCore: expected {expected}, found {actual}"
        )
    query_enabled = outputs.get("QueryPlaneEnabled")
    if query_enabled != "true":
        raise AgentCoreDeploymentError("production control plane does not expose the reviewed query configuration")
    if expected_image is not None and outputs.get("ControlPlaneImageUri") != expected_image:
        raise AgentCoreDeploymentError("control-plane output is not bound to the verified image")
    _required_output(dict(outputs), "ClusterName")
    _required_output(dict(outputs), "ServiceName")
    _required_output(dict(outputs), "TaskDefinitionArn")
    _required_output(dict(outputs), "TargetGroupArn")

    endpoint_mode = outputs.get("EndpointMode")
    if endpoint_mode is None:
        if expected_endpoint_mode != CUSTOM_DOMAIN:
            raise AgentCoreDeploymentError(
                "control-plane outputs predate the requested endpoint architecture"
            )
        return
    if endpoint_mode not in {CUSTOM_DOMAIN, CLOUDFRONT}:
        raise AgentCoreDeploymentError(
            "control-plane output has an invalid endpoint architecture"
        )
    if endpoint_mode != expected_endpoint_mode:
        raise AgentCoreDeploymentError(
            "control-plane endpoint architecture does not match the reviewed setup"
        )

    expected_auth_mode = (
        "application-oidc"
        if endpoint_mode == CLOUDFRONT
        else "alb-cognito"
    )
    if outputs.get("ControlPlaneAuthMode") != expected_auth_mode:
        raise AgentCoreDeploymentError(
            "control-plane browser authentication mode is invalid"
        )
    expected_load_balancer_scheme = (
        "internal"
        if endpoint_mode == CLOUDFRONT
        else "internet-facing"
    )
    if (
        outputs.get("LoadBalancerScheme")
        != expected_load_balancer_scheme
    ):
        raise AgentCoreDeploymentError(
            "control-plane load-balancer exposure does not match its endpoint mode"
        )

    domain_name = _required_output(
        dict(outputs),
        "ControlPlaneDomainName",
    )
    control_plane_url = _required_output(
        dict(outputs),
        "ControlPlaneUrl",
    )
    if control_plane_url != f"https://{domain_name}":
        raise AgentCoreDeploymentError(
            "control-plane URL does not match its canonical hostname"
        )

    cloudfront_outputs = (
        "BrowserClientId",
        "DistributionId",
        "DistributionDomainName",
        "VpcOriginId",
        "WebAclArn",
    )
    if endpoint_mode == CLOUDFRONT:
        if not domain_name.endswith(".cloudfront.net"):
            raise AgentCoreDeploymentError(
                "CloudFront control-plane hostname is invalid"
            )
        if outputs.get("DistributionDomainName") != domain_name:
            raise AgentCoreDeploymentError(
                "CloudFront distribution hostname is not canonical"
            )
        for output_name in cloudfront_outputs:
            _required_output(dict(outputs), output_name)
    elif any(name in outputs for name in cloudfront_outputs):
        raise AgentCoreDeploymentError(
            "custom-domain control plane emitted CloudFront-only outputs"
        )


def _validate_candidate_version(
    outputs: Mapping[str, str],
    expected_version: str,
    expected_endpoint_name: str,
    config: AgentCoreSetupConfig,
) -> CandidateBinding:
    expected_endpoint_name = _validate_candidate_endpoint_name(expected_endpoint_name)
    if not expected_version.isdigit() or expected_version.startswith("0") or len(expected_version) > 32:
        raise AgentCoreDeploymentError("candidate runtime version must be a positive integer")
    if outputs.get("RecoveryCutoverMode") != "normal":
        raise AgentCoreDeploymentError("AgentCore recovery must be in normal mode before changing endpoint publication")
    actual = outputs.get("CandidateRuntimeVersion")
    current = outputs.get("RuntimeVersion")
    if actual != expected_version or current != expected_version:
        raise AgentCoreDeploymentError("candidate runtime version no longer matches the reviewed deployment")
    _required_output(dict(outputs), "CandidateRuntimeEndpointArn")
    actual_endpoint_name = _validate_candidate_endpoint_name(
        _required_output(
            dict(outputs),
            "CandidateRuntimeEndpointName",
        )
    )
    if actual_endpoint_name != expected_endpoint_name:
        raise AgentCoreDeploymentError("candidate endpoint name no longer matches the certified deployment")
    if outputs.get("EnabledProviders") != ",".join(config.runtime.enabled_providers):
        raise AgentCoreDeploymentError("candidate provider allowlist does not match the reviewed setup")
    _validate_shared_runtime_configuration(config, outputs)
    return CandidateBinding(
        endpoint_name=actual_endpoint_name,
        provider_secret_version=_required_output(
            dict(outputs),
            "ProviderSecretVersion",
        ),
    )


def _stack_parameters(
    stack: Mapping[str, Any],
    *,
    stack_name: str = AGENTCORE_STACK,
) -> dict[str, str]:
    raw_parameters = stack.get("Parameters")
    if not isinstance(raw_parameters, list):
        raise AgentCoreDeploymentError(f"{stack_name} parameters are unavailable")
    parameters: dict[str, str] = {}
    for item in raw_parameters:
        if not isinstance(item, dict):
            raise AgentCoreDeploymentError(f"{stack_name} parameters are malformed")
        key = item.get("ParameterKey")
        value = item.get("ParameterValue")
        if not isinstance(key, str) or not isinstance(value, str) or key in parameters:
            raise AgentCoreDeploymentError(f"{stack_name} parameters are malformed")
        parameters[key] = value
    return parameters


def _validate_stack_endpoint_mode(
    stack: Mapping[str, Any],
    *,
    expected_endpoint_mode: str,
    stack_name: str,
) -> None:
    if expected_endpoint_mode not in {CUSTOM_DOMAIN, CLOUDFRONT}:
        raise AgentCoreDeploymentError(
            "reviewed control-plane endpoint mode is invalid"
        )
    parameters = _stack_parameters(stack, stack_name=stack_name)
    actual_endpoint_mode = parameters.get("EndpointMode", CUSTOM_DOMAIN)
    if actual_endpoint_mode not in {CUSTOM_DOMAIN, CLOUDFRONT}:
        raise AgentCoreDeploymentError(
            f"{stack_name} has an invalid endpoint architecture"
        )
    if actual_endpoint_mode != expected_endpoint_mode:
        raise AgentCoreDeploymentError(
            f"{stack_name} endpoint architecture cannot be changed in place"
        )


def _validate_stack_parameters(
    stack: Mapping[str, Any],
    expected: Mapping[str, str],
    *,
    stack_name: str,
) -> dict[str, str]:
    parameters = _stack_parameters(stack, stack_name=stack_name)
    missing = sorted(set(expected).difference(parameters))
    mismatched = sorted(name for name, expected_value in expected.items() if parameters.get(name) != expected_value)
    if missing or mismatched:
        details = sorted(set(missing) | set(mismatched))
        raise AgentCoreDeploymentError(
            f"existing {stack_name} configuration differs from the reviewed "
            "setup; use an approved identity/infrastructure maintenance "
            "change for: " + ", ".join(details)
        )
    return parameters


def _update_endpoint_publication(
    cloudformation_client: Any,
    *,
    candidate_endpoint_name: str,
    publish_candidate: bool,
    production_version: str,
) -> dict[str, str]:
    """Change endpoint pointers using only the deployed reviewed template."""
    stack = _existing_stack(
        cloudformation_client,
        AGENTCORE_STACK,
    )
    if stack is None:
        raise AgentCoreDeploymentError("AgentCore stack does not exist")
    current = _stack_parameters(stack, stack_name=AGENTCORE_STACK)
    overrides = {
        "CandidateEndpointName": candidate_endpoint_name,
        "ProductionRuntimeVersion": production_version,
        "PublishCandidateEndpoint": ("true" if publish_candidate else "false"),
        "PublishProductionEndpoint": ("true" if production_version else "false"),
    }
    missing = sorted(set(overrides).difference(current))
    if missing:
        raise AgentCoreDeploymentError("AgentCore stack lacks endpoint-publication parameters: " + ", ".join(missing))
    role_arn = stack.get("RoleARN")
    if (
        not isinstance(role_arn, str)
        or not role_arn.startswith("arn:")
        or any(character.isspace() for character in role_arn)
    ):
        raise AgentCoreDeploymentError("AgentCore stack has no CloudFormation execution role")
    parameters = [
        (
            {
                "ParameterKey": key,
                "ParameterValue": overrides[key],
            }
            if key in overrides
            else {
                "ParameterKey": key,
                "UsePreviousValue": True,
            }
        )
        for key in sorted(current)
    ]
    try:
        cloudformation_client.update_stack(
            StackName=AGENTCORE_STACK,
            UsePreviousTemplate=True,
            Parameters=parameters,
            Capabilities=["CAPABILITY_NAMED_IAM"],
            RoleARN=role_arn,
            ClientRequestToken=("axonllm-endpoint-" + secrets.token_hex(16)),
        )
    except Exception as exc:
        if not (
            _aws_error_code(exc) == "ValidationError" and _aws_error_message(exc) == "No updates are to be performed."
        ):
            raise AgentCoreDeploymentError("could not update AgentCore endpoint publication") from exc
    else:
        try:
            cloudformation_client.get_waiter("stack_update_complete").wait(
                StackName=AGENTCORE_STACK,
                WaiterConfig={
                    "Delay": 15,
                    "MaxAttempts": 240,
                },
            )
        except Exception as exc:
            raise AgentCoreDeploymentError("AgentCore endpoint publication did not complete") from exc
    after = _existing_stack(
        cloudformation_client,
        AGENTCORE_STACK,
    )
    if after is None:
        raise AgentCoreDeploymentError("AgentCore stack disappeared during endpoint publication")
    return _outputs_from_stack(after, AGENTCORE_STACK)


def _validated_transition_identity(
    value: Mapping[str, Any],
) -> dict[str, str]:
    expected = {
        "changeId",
        "deploymentCommit",
        "repository",
        "rollbackNotBefore",
        "runAttempt",
        "runId",
        "transitionId",
    }
    if set(value) != expected or any(not isinstance(value.get(name), str) for name in expected):
        raise AgentCoreDeploymentError("promotion transition identity is malformed")
    normalized = {name: str(value[name]) for name in expected}
    if (
        _TRANSITION_CHANGE_PATTERN.fullmatch(normalized["changeId"]) is None
        or _TRANSITION_COMMIT_PATTERN.fullmatch(normalized["deploymentCommit"]) is None
        or _TRANSITION_REPOSITORY_PATTERN.fullmatch(normalized["repository"]) is None
        or _parse_transition_timestamp(normalized["rollbackNotBefore"]) is None
        or _TRANSITION_RUN_PATTERN.fullmatch(normalized["runAttempt"]) is None
        or _TRANSITION_RUN_PATTERN.fullmatch(normalized["runId"]) is None
        or _TRANSITION_ID_PATTERN.fullmatch(normalized["transitionId"]) is None
    ):
        raise AgentCoreDeploymentError("promotion transition identity is malformed")
    return normalized


def _parse_transition_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _load_transition_identity(path: Path) -> dict[str, str]:
    try:
        before = path.lstat()
        if path.is_symlink() or not path.is_file():
            raise OSError("transition context is not a regular file")
        if before.st_size > 64 * 1024:
            raise OSError("transition context is too large")
        value = json.loads(path.read_text(encoding="utf-8"))
        after = path.stat()
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise AgentCoreDeploymentError("cannot read promotion transition context") from exc
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or not isinstance(value, dict)
    ):
        raise AgentCoreDeploymentError("promotion transition context is malformed")
    return _validated_transition_identity(value)


def _expected_promotion_metadata(
    before: Mapping[str, str],
    *,
    candidate_version: str,
    binding: CandidateBinding,
    config: AgentCoreSetupConfig,
    control_plane_stack: Mapping[str, Any] | None,
    transition: Mapping[str, str] | None,
) -> dict[str, Any]:
    normalized_transition = None if transition is None else _validated_transition_identity(transition)
    runtime_arn = _required_output(
        dict(before),
        "RuntimeArn",
    )
    control_plane: dict[str, Any] | None = None
    if config.identity_mode == MANAGED_COGNITO:
        if config.control_plane is None:
            raise AgentCoreDeploymentError("control-plane settings are missing")
        control_plane = {
            "previousParameters": (
                None
                if control_plane_stack is None
                else _stack_parameters(
                    control_plane_stack,
                    stack_name=CONTROL_PLANE_STACK,
                )
            ),
            "previousStackId": (
                None
                if control_plane_stack is None
                else _required_stack_id(
                    control_plane_stack,
                    CONTROL_PLANE_STACK,
                )
            ),
            "stackExisted": control_plane_stack is not None,
            "targetEndpointMode": config.control_plane.endpoint_mode,
            "targetImage": config.control_plane.verified_image_uri,
        }
    metadata: dict[str, Any] = {
        "candidateRuntimeVersion": candidate_version,
        "candidateEndpointName": binding.endpoint_name,
        "controlPlane": control_plane,
        "enabledProviders": _required_output(
            dict(before),
            "EnabledProviders",
        ),
        "previousProductionRuntimeVersion": (_production_runtime_version(before) or None),
        "productionEndpointArn": (f"{runtime_arn}/runtime-endpoint/production"),
        "productionRuntimeVersion": candidate_version,
        "providerSecretVersion": binding.provider_secret_version,
        "region": config.aws_region,
        "runtimeArn": runtime_arn,
        "schemaVersion": (3 if normalized_transition is not None else 2),
        "sharedRuntimeConfiguration": {
            name: _required_output(dict(before), name) for name in _shared_runtime_configuration(config)
        },
    }
    if normalized_transition is not None:
        metadata["transition"] = normalized_transition
    return metadata


def _prepare_candidate_promotion(
    config: AgentCoreSetupConfig,
    *,
    candidate_version: str,
    candidate_endpoint_name: str,
    outputs_dir: Path,
    boto3_session: Any,
    transition: Mapping[str, str] | None,
) -> tuple[
    Any,
    dict[str, str],
    CandidateBinding,
    dict[str, Any],
]:
    cloudformation_client = boto3_session.client(
        "cloudformation",
        region_name=config.aws_region,
    )
    before = _existing_agentcore_outputs(cloudformation_client)
    if before is None:
        raise AgentCoreDeploymentError("AgentCore stack does not exist")
    binding = _validate_candidate_version(
        before,
        candidate_version,
        candidate_endpoint_name,
        config,
    )
    _verify_confirmed_alarm_subscription(
        boto3_session,
        config=config,
        outputs=before,
    )
    control_plane_stack = (
        _existing_stack(
            cloudformation_client,
            CONTROL_PLANE_STACK,
        )
        if config.identity_mode == MANAGED_COGNITO
        else None
    )
    if control_plane_stack is not None:
        if config.control_plane is None:
            raise AgentCoreDeploymentError(
                "managed Cognito control-plane settings are missing"
            )
        _validate_stack_endpoint_mode(
            control_plane_stack,
            expected_endpoint_mode=config.control_plane.endpoint_mode,
            stack_name=CONTROL_PLANE_STACK,
        )
        _validate_control_plane_outputs(
            _outputs_from_stack(
                control_plane_stack,
                CONTROL_PLANE_STACK,
            ),
            runtime_outputs=before,
            expected_image=None,
            expected_endpoint_mode=config.control_plane.endpoint_mode,
        )
    metadata = _expected_promotion_metadata(
        before,
        candidate_version=candidate_version,
        binding=binding,
        config=config,
        control_plane_stack=control_plane_stack,
        transition=transition,
    )
    metadata_path = outputs_dir / "promotion.json"
    if metadata_path.exists() or metadata_path.is_symlink():
        if _promotion_metadata(metadata_path) != metadata:
            raise AgentCoreDeploymentError("existing promotion metadata describes a different transition")
    else:
        _write_private_json(metadata_path, metadata)
    return cloudformation_client, before, binding, metadata


def prepare_candidate_promotion(
    config: AgentCoreSetupConfig,
    *,
    candidate_version: str,
    candidate_endpoint_name: str,
    outputs_dir: Path,
    boto3_session: Any | None = None,
    transition: Mapping[str, str] | None = None,
) -> None:
    """Persist exact rollback metadata before the live endpoint changes."""
    outputs_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.chmod(0o700)
    if boto3_session is None:
        import boto3

        boto3_session = boto3.Session(region_name=config.aws_region)
    _prepare_candidate_promotion(
        config,
        candidate_version=candidate_version,
        candidate_endpoint_name=candidate_endpoint_name,
        outputs_dir=outputs_dir,
        boto3_session=boto3_session,
        transition=transition,
    )
    print(f"Promotion metadata prepared for AgentCore runtime version {candidate_version}.")


def promote_candidate(
    config: AgentCoreSetupConfig,
    *,
    candidate_version: str,
    candidate_endpoint_name: str,
    outputs_dir: Path,
    assume_yes: bool,
    runner: CommandRunner = _run_command,
    boto3_session: Any | None = None,
    transition: Mapping[str, str] | None = None,
) -> None:
    """Atomically point production at one already-certified candidate."""
    del assume_yes, runner
    outputs_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.chmod(0o700)
    if boto3_session is None:
        import boto3

        boto3_session = boto3.Session(region_name=config.aws_region)
    (
        cloudformation_client,
        before,
        binding,
        metadata,
    ) = _prepare_candidate_promotion(
        config,
        candidate_version=candidate_version,
        candidate_endpoint_name=candidate_endpoint_name,
        outputs_dir=outputs_dir,
        boto3_session=boto3_session,
        transition=transition,
    )
    provider_secret_version = binding.provider_secret_version
    previous_production_version = _production_runtime_version(before)

    print(f"Promoting certified AgentCore runtime version {candidate_version}...")
    after = _update_endpoint_publication(
        cloudformation_client,
        candidate_endpoint_name=binding.endpoint_name,
        publish_candidate=True,
        production_version=candidate_version,
    )
    try:
        if (
            _required_output(after, "RuntimeVersion") != candidate_version
            or _required_output(
                after,
                "CandidateRuntimeVersion",
            )
            != candidate_version
            or _required_output(
                after,
                "CandidateRuntimeEndpointName",
            )
            != binding.endpoint_name
            or _required_output(
                after,
                "ProductionRuntimeVersion",
            )
            != candidate_version
            or _required_output(after, "RuntimeEndpointName") != "production"
            or _required_output(
                after,
                "ProviderSecretVersion",
            )
            != provider_secret_version
            or _required_output(after, "EnabledProviders") != ",".join(config.runtime.enabled_providers)
        ):
            raise AgentCoreDeploymentError(
                "production promotion did not preserve the certified runtime and provider secret versions"
            )
        endpoint_arn = _required_output(after, "RuntimeEndpointArn")
        if endpoint_arn != metadata["productionEndpointArn"]:
            raise AgentCoreDeploymentError("production endpoint ARN changed during promotion")
        _validate_shared_runtime_configuration(config, after)
        _verify_confirmed_alarm_subscription(
            boto3_session,
            config=config,
            outputs=after,
        )
    except AgentCoreDeploymentError:
        try:
            restored = _update_endpoint_publication(
                cloudformation_client,
                candidate_endpoint_name=binding.endpoint_name,
                publish_candidate=True,
                production_version=previous_production_version,
            )
            if (
                _production_runtime_version(restored) != previous_production_version
                or restored.get("CandidateRuntimeVersion") != candidate_version
                or restored.get("CandidateRuntimeEndpointName") != binding.endpoint_name
            ):
                raise AgentCoreDeploymentError("compensating rollback returned an unexpected runtime binding")
        except Exception as rollback_exc:
            raise AgentCoreDeploymentError(
                "production promotion verification failed and compensating rollback also failed"
            ) from rollback_exc
        raise
    _write_private_json(
        outputs_dir / "agentcore-outputs.json",
        {AGENTCORE_STACK: after},
    )
    print(f"Production now targets certified runtime version {candidate_version}.")


def discard_candidate(
    config: AgentCoreSetupConfig,
    *,
    candidate_version: str,
    candidate_endpoint_name: str,
    outputs_dir: Path,
    assume_yes: bool,
    runner: CommandRunner = _run_command,
    boto3_session: Any | None = None,
) -> None:
    """Remove a failed candidate endpoint without changing production."""
    del assume_yes, runner
    outputs_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.chmod(0o700)
    if boto3_session is None:
        import boto3

        boto3_session = boto3.Session(region_name=config.aws_region)
    cloudformation_client = boto3_session.client(
        "cloudformation",
        region_name=config.aws_region,
    )
    before = _existing_agentcore_outputs(cloudformation_client)
    if before is None:
        raise AgentCoreDeploymentError("AgentCore stack does not exist")
    binding = _validate_candidate_version(
        before,
        candidate_version,
        candidate_endpoint_name,
        config,
    )
    production_version = _production_runtime_version(before)
    if production_version == candidate_version:
        raise AgentCoreDeploymentError("cannot discard a candidate that is already in production")
    after = _update_endpoint_publication(
        cloudformation_client,
        candidate_endpoint_name=binding.endpoint_name,
        publish_candidate=False,
        production_version=production_version,
    )
    if "CandidateRuntimeEndpointArn" in after:
        raise AgentCoreDeploymentError("failed candidate endpoint is still published")
    if _production_runtime_version(after) != production_version:
        raise AgentCoreDeploymentError("discarding the candidate changed production")
    if _required_output(after, "EnabledProviders") != ",".join(config.runtime.enabled_providers):
        raise AgentCoreDeploymentError("discarding the candidate changed the provider allowlist")
    _validate_shared_runtime_configuration(config, after)
    _write_private_json(
        outputs_dir / "agentcore-outputs.json",
        {AGENTCORE_STACK: after},
    )
    print(f"Candidate runtime version {candidate_version} is no longer published.")


def _runtime_matches_promotion(
    outputs: Mapping[str, str],
    metadata: Mapping[str, Any],
) -> bool:
    if (
        outputs.get("RuntimeArn") != metadata.get("runtimeArn")
        or outputs.get("RuntimeVersion") != metadata.get("candidateRuntimeVersion")
        or outputs.get("ProviderSecretVersion") != metadata.get("providerSecretVersion")
    ):
        return False
    if metadata.get("schemaVersion") in {2, 3}:
        if outputs.get("EnabledProviders") != metadata.get("enabledProviders"):
            return False
        shared = metadata.get("sharedRuntimeConfiguration")
        if not isinstance(shared, dict) or any(outputs.get(name) != value for name, value in shared.items()):
            return False
    return True


def _control_plane_command_inputs(
    config: AgentCoreSetupConfig,
    runtime_outputs: Mapping[str, str],
    *,
    deployment_transition_id: str,
) -> dict[str, Any]:
    primary = _required_output(
        dict(runtime_outputs),
        "StateTableName",
    )
    selected = _required_output(
        dict(runtime_outputs),
        "SelectedRuntimeStateTableName",
    )
    approval = runtime_outputs.get("RecoveryApprovalId")
    if not isinstance(approval, str):
        raise AgentCoreDeploymentError("AgentCore recovery approval output is missing")
    return {
        "config": config,
        "primary_state_table_name": primary,
        "runtime_state_table_name": ("" if selected == primary else selected),
        "recovery_approval_id": approval,
        "deployment_transition_id": deployment_transition_id,
    }


def deploy_control_plane(
    config: AgentCoreSetupConfig,
    *,
    outputs_dir: Path,
    assume_yes: bool,
    runner: CommandRunner = _run_command,
    boto3_session: Any | None = None,
    rehearsal_control_table_arn: str | None = None,
) -> None:
    """Deploy the reviewed control plane only after runtime promotion."""
    _assert_deployment_prerequisites()
    if config.identity_mode != MANAGED_COGNITO:
        raise AgentCoreDeploymentError("control-plane deployment requires managed-cognito")
    if config.runtime.athena_query is None:
        raise AgentCoreDeploymentError("production control-plane deployment requires Athena query configuration")
    rehearsal_control_table_arn = validate_rehearsal_control_table_arn(
        aws_region=config.aws_region,
        deployment_namespace=_ACTIVE_DEPLOYMENT_NAMESPACE,
        rehearsal_control_table_arn=(rehearsal_control_table_arn),
    )
    outputs_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.chmod(0o700)
    metadata = _promotion_metadata(outputs_dir / "promotion.json")
    control_metadata = metadata.get("controlPlane")
    if (
        metadata.get("schemaVersion") not in {2, 3}
        or not isinstance(control_metadata, dict)
        or config.control_plane is None
        or control_metadata.get("targetImage") != config.control_plane.verified_image_uri
        or control_metadata.get(
            "targetEndpointMode",
            CUSTOM_DOMAIN,
        )
        != config.control_plane.endpoint_mode
        or metadata.get("region") != config.aws_region
    ):
        raise AgentCoreDeploymentError("promotion metadata is not bound to this control-plane deployment")
    if boto3_session is None:
        import boto3

        boto3_session = boto3.Session(region_name=config.aws_region)
    cloudformation_client = boto3_session.client(
        "cloudformation",
        region_name=config.aws_region,
    )
    _validate_prefix_list_inputs(
        boto3_session,
        config=config,
    )
    aws_identity, execution_policy_arns = _require_bootstrap_execution_policy(
        boto3_session,
        config=config,
        create_if_missing=False,
    )
    _assert_cdk_execution_role_policy(
        boto3_session,
        config=config,
        identity=aws_identity,
        expected_policy_arns=execution_policy_arns,
    )
    runtime_outputs = _existing_agentcore_outputs(cloudformation_client)
    if (
        runtime_outputs is None
        or not _runtime_matches_promotion(
            runtime_outputs,
            metadata,
        )
        or _production_runtime_version(runtime_outputs) != metadata["candidateRuntimeVersion"]
    ):
        raise AgentCoreDeploymentError("production runtime does not match the prepared transition")
    deployment_transition_id = _UNBOUND_DEPLOYMENT_TRANSITION_ID
    if metadata.get("schemaVersion") == 3:
        transition = metadata.get("transition")
        deployment_transition_id = (
            transition.get("transitionId")
            if isinstance(transition, dict)
            else None
        )
        if (
            not isinstance(deployment_transition_id, str)
            or _TRANSITION_ID_PATTERN.fullmatch(deployment_transition_id) is None
        ):
            raise AgentCoreDeploymentError(
                "promotion metadata has no valid deployment transition owner"
            )
    command_inputs = _control_plane_command_inputs(
        config,
        runtime_outputs,
        deployment_transition_id=deployment_transition_id,
    )
    target_parameters = _control_plane_parameters(
        config,
        primary_state_table_name=command_inputs["primary_state_table_name"],
        runtime_state_table_name=command_inputs["runtime_state_table_name"],
        recovery_approval_id=command_inputs["recovery_approval_id"],
        deployment_transition_id=command_inputs["deployment_transition_id"],
        deployment_namespace=_ACTIVE_DEPLOYMENT_NAMESPACE,
        rehearsal_control_table_arn=(rehearsal_control_table_arn),
    )
    current_stack = _existing_stack(
        cloudformation_client,
        CONTROL_PLANE_STACK,
    )
    if current_stack is not None:
        _validate_stack_endpoint_mode(
            current_stack,
            expected_endpoint_mode=config.control_plane.endpoint_mode,
            stack_name=CONTROL_PLANE_STACK,
        )
    if control_metadata["stackExisted"]:
        if (
            current_stack is None
            or _required_stack_id(
                current_stack,
                CONTROL_PLANE_STACK,
            )
            != control_metadata["previousStackId"]
        ):
            raise AgentCoreDeploymentError("the existing control-plane stack changed after transition preparation")
    elif current_stack is not None:
        current_parameters = _stack_parameters(
            current_stack,
            stack_name=CONTROL_PLANE_STACK,
        )
        current_outputs = _outputs_from_stack(
            current_stack,
            CONTROL_PLANE_STACK,
        )
        if all(current_parameters.get(name) == value for name, value in target_parameters.items()):
            _validate_control_plane_outputs(
                current_outputs,
                runtime_outputs=runtime_outputs,
                expected_image=control_metadata["targetImage"],
                expected_endpoint_mode=(
                    config.control_plane.endpoint_mode
                ),
            )
            _write_private_json(
                outputs_dir / "control-plane-outputs.json",
                {CONTROL_PLANE_STACK: current_outputs},
            )
            print("Control plane already targets the reviewed image.")
            return
        raise AgentCoreDeploymentError("an unexpected control-plane stack appeared after transition preparation")

    if current_stack is not None:
        current_parameters = _stack_parameters(
            current_stack,
            stack_name=CONTROL_PLANE_STACK,
        )
        previous_parameters = control_metadata["previousParameters"]
        if all(current_parameters.get(name) == value for name, value in previous_parameters.items()):
            pass
        elif all(current_parameters.get(name) == value for name, value in target_parameters.items()):
            current_outputs = _outputs_from_stack(
                current_stack,
                CONTROL_PLANE_STACK,
            )
            _validate_control_plane_outputs(
                current_outputs,
                runtime_outputs=runtime_outputs,
                expected_image=control_metadata["targetImage"],
                expected_endpoint_mode=(
                    config.control_plane.endpoint_mode
                ),
            )
            _write_private_json(
                outputs_dir / "control-plane-outputs.json",
                {CONTROL_PLANE_STACK: current_outputs},
            )
            print("Control plane already targets the reviewed image.")
            return
        else:
            raise AgentCoreDeploymentError("control-plane parameters changed after transition preparation")

    output_path = outputs_dir / "control-plane-outputs.json"
    output_path.unlink(missing_ok=True)
    print("Deploying the certified shared-state web control plane...")
    runner(
        control_plane_deploy_command(
            outputs_file=output_path,
            assume_yes=assume_yes,
            deployment_namespace=_ACTIVE_DEPLOYMENT_NAMESPACE,
            rehearsal_control_table_arn=(rehearsal_control_table_arn),
            **command_inputs,
        ),
        INFRA_ROOT,
    )
    outputs = _stack_outputs(
        output_path,
        CONTROL_PLANE_STACK,
    )
    _validate_control_plane_outputs(
        outputs,
        runtime_outputs=runtime_outputs,
        expected_image=control_metadata["targetImage"],
        expected_endpoint_mode=config.control_plane.endpoint_mode,
    )
    after_stack = _existing_stack(
        cloudformation_client,
        CONTROL_PLANE_STACK,
    )
    if after_stack is None:
        raise AgentCoreDeploymentError("control-plane stack disappeared after deployment")
    if (
        control_metadata["stackExisted"]
        and _required_stack_id(
            after_stack,
            CONTROL_PLANE_STACK,
        )
        != control_metadata["previousStackId"]
    ):
        raise AgentCoreDeploymentError("control-plane stack identity changed during deployment")
    after_parameters = _stack_parameters(
        after_stack,
        stack_name=CONTROL_PLANE_STACK,
    )
    if any(after_parameters.get(name) != value for name, value in target_parameters.items()):
        raise AgentCoreDeploymentError("control-plane stack is not bound to the reviewed parameters")
    print(
        "Control plane: "
        + _required_output(
            dict(outputs),
            "ControlPlaneUrl",
        )
    )


def _cloudformation_role(
    stack: Mapping[str, Any],
    stack_name: str,
) -> str:
    role_arn = stack.get("RoleARN")
    if (
        not isinstance(role_arn, str)
        or not role_arn.startswith("arn:")
        or any(character.isspace() for character in role_arn)
    ):
        raise AgentCoreDeploymentError(f"{stack_name} has no CloudFormation execution role")
    return role_arn


def _restore_control_plane_parameters(
    cloudformation_client: Any,
    *,
    stack: Mapping[str, Any],
    previous_parameters: Mapping[str, str],
) -> dict[str, Any]:
    current = _stack_parameters(
        stack,
        stack_name=CONTROL_PLANE_STACK,
    )
    missing = sorted(set(previous_parameters).difference(current))
    if missing:
        raise AgentCoreDeploymentError("control-plane rollback parameters are unavailable: " + ", ".join(missing))
    parameters = [
        (
            {
                "ParameterKey": name,
                "ParameterValue": previous_parameters[name],
            }
            if name in previous_parameters
            else {
                "ParameterKey": name,
                "UsePreviousValue": True,
            }
        )
        for name in sorted(current)
    ]
    try:
        cloudformation_client.update_stack(
            StackName=CONTROL_PLANE_STACK,
            UsePreviousTemplate=True,
            Parameters=parameters,
            Capabilities=["CAPABILITY_NAMED_IAM"],
            RoleARN=_cloudformation_role(
                stack,
                CONTROL_PLANE_STACK,
            ),
            ClientRequestToken=("axonllm-control-rollback-" + secrets.token_hex(16)),
        )
    except Exception as exc:
        if not (
            _aws_error_code(exc) == "ValidationError" and _aws_error_message(exc) == "No updates are to be performed."
        ):
            raise AgentCoreDeploymentError("could not restore control-plane parameters") from exc
    else:
        try:
            cloudformation_client.get_waiter("stack_update_complete").wait(
                StackName=CONTROL_PLANE_STACK,
                WaiterConfig={"Delay": 15, "MaxAttempts": 240},
            )
        except Exception as exc:
            raise AgentCoreDeploymentError("control-plane rollback did not complete") from exc
    after = _existing_stack(
        cloudformation_client,
        CONTROL_PLANE_STACK,
    )
    if after is None:
        raise AgentCoreDeploymentError("control-plane stack disappeared during rollback")
    return after


def _stack_load_balancer_arn(
    cloudformation_client: Any,
    *,
    stack_name: str,
) -> str | None:
    resources: list[object] = []
    next_token: str | None = None
    for _ in range(100):
        arguments: dict[str, str] = {"StackName": stack_name}
        if next_token is not None:
            arguments["NextToken"] = next_token
        try:
            response = cloudformation_client.list_stack_resources(**arguments)
        except Exception as exc:
            raise AgentCoreDeploymentError("could not inspect control-plane resources before deletion") from exc
        page = response.get("StackResourceSummaries")
        if not isinstance(page, list):
            raise AgentCoreDeploymentError("CloudFormation returned malformed control-plane resources")
        resources.extend(page)
        raw_next = response.get("NextToken")
        if raw_next is None:
            break
        if not isinstance(raw_next, str) or not raw_next:
            raise AgentCoreDeploymentError("CloudFormation returned malformed resource pagination")
        next_token = raw_next
    else:
        raise AgentCoreDeploymentError("CloudFormation returned too many control-plane resource pages")
    load_balancers = [
        item.get("PhysicalResourceId")
        for item in resources
        if isinstance(item, dict)
        and item.get("ResourceType") == "AWS::ElasticLoadBalancingV2::LoadBalancer"
        and item.get("ResourceStatus") != "DELETE_COMPLETE"
    ]
    if not load_balancers:
        return None
    if len(load_balancers) != 1 or not isinstance(load_balancers[0], str) or not load_balancers[0].startswith("arn:"):
        raise AgentCoreDeploymentError("control-plane stack has ambiguous load-balancer ownership")
    return load_balancers[0]


def _disable_load_balancer_deletion_protection(
    cloudformation_client: Any,
    elbv2_client: Any,
    *,
    stack_name: str,
) -> None:
    load_balancer_arn = _stack_load_balancer_arn(
        cloudformation_client,
        stack_name=stack_name,
    )
    if load_balancer_arn is None:
        return

    def deletion_protection_enabled() -> bool:
        try:
            response = elbv2_client.describe_load_balancer_attributes(LoadBalancerArn=load_balancer_arn)
        except Exception as exc:
            raise AgentCoreDeploymentError("could not inspect control-plane ALB deletion protection") from exc
        attributes = response.get("Attributes")
        if not isinstance(attributes, list):
            raise AgentCoreDeploymentError("ELB returned malformed load-balancer attributes")
        values = [
            item.get("Value")
            for item in attributes
            if isinstance(item, dict) and item.get("Key") == "deletion_protection.enabled"
        ]
        if len(values) != 1 or values[0] not in {"true", "false"}:
            raise AgentCoreDeploymentError("ELB did not return one deletion-protection attribute")
        return values[0] == "true"

    if deletion_protection_enabled():
        try:
            elbv2_client.modify_load_balancer_attributes(
                LoadBalancerArn=load_balancer_arn,
                Attributes=[
                    {
                        "Key": "deletion_protection.enabled",
                        "Value": "false",
                    }
                ],
            )
        except Exception as exc:
            raise AgentCoreDeploymentError("could not disable control-plane ALB deletion protection") from exc
    if deletion_protection_enabled():
        raise AgentCoreDeploymentError("control-plane ALB deletion protection remained enabled")


def _delete_new_control_plane(
    cloudformation_client: Any,
    elbv2_client: Any,
    stack: Mapping[str, Any],
) -> None:
    _disable_load_balancer_deletion_protection(
        cloudformation_client,
        elbv2_client,
        stack_name=CONTROL_PLANE_STACK,
    )
    try:
        cloudformation_client.delete_stack(
            StackName=CONTROL_PLANE_STACK,
            RoleARN=_cloudformation_role(
                stack,
                CONTROL_PLANE_STACK,
            ),
            ClientRequestToken=("axonllm-control-delete-" + secrets.token_hex(16)),
        )
        cloudformation_client.get_waiter("stack_delete_complete").wait(
            StackName=CONTROL_PLANE_STACK,
            WaiterConfig={"Delay": 15, "MaxAttempts": 240},
        )
    except Exception as exc:
        raise AgentCoreDeploymentError("could not remove the newly created control-plane stack") from exc


def _reconcile_control_plane_transition(
    cloudformation_client: Any,
    elbv2_client: Any,
    *,
    metadata: Mapping[str, Any],
    runtime_outputs: Mapping[str, str],
    outputs_dir: Path,
) -> str:
    control = metadata.get("controlPlane")
    if not isinstance(control, dict):
        return "not-applicable"
    stack = _existing_stack(
        cloudformation_client,
        CONTROL_PLANE_STACK,
        allow_failed_creation=not control["stackExisted"],
    )
    target_endpoint_mode = control.get(
        "targetEndpointMode",
        CUSTOM_DOMAIN,
    )
    if stack is not None:
        _validate_stack_endpoint_mode(
            stack,
            expected_endpoint_mode=target_endpoint_mode,
            stack_name=CONTROL_PLANE_STACK,
        )
    if control["stackExisted"]:
        if (
            stack is None
            or _required_stack_id(
                stack,
                CONTROL_PLANE_STACK,
            )
            != control["previousStackId"]
        ):
            raise AgentCoreDeploymentError("control-plane stack cannot be matched to rollback metadata")
        previous_parameters = control["previousParameters"]
        current = _stack_parameters(
            stack,
            stack_name=CONTROL_PLANE_STACK,
        )
        if all(current.get(name) == value for name, value in previous_parameters.items()):
            outputs = _outputs_from_stack(
                stack,
                CONTROL_PLANE_STACK,
            )
            _write_private_json(
                outputs_dir / "control-plane-outputs.json",
                {CONTROL_PLANE_STACK: outputs},
            )
            return "already-restored"
        if current.get("ControlPlaneVerifiedImageUri") != control["targetImage"]:
            raise AgentCoreDeploymentError(
                "control-plane stack no longer matches either side of the prepared transition"
            )
        restored = _restore_control_plane_parameters(
            cloudformation_client,
            stack=stack,
            previous_parameters=previous_parameters,
        )
        restored_parameters = _stack_parameters(
            restored,
            stack_name=CONTROL_PLANE_STACK,
        )
        if any(restored_parameters.get(name) != value for name, value in previous_parameters.items()):
            raise AgentCoreDeploymentError("control-plane rollback did not restore prior parameters")
        outputs = _outputs_from_stack(
            restored,
            CONTROL_PLANE_STACK,
        )
        _write_private_json(
            outputs_dir / "control-plane-outputs.json",
            {CONTROL_PLANE_STACK: outputs},
        )
        return "restored"
    if stack is None:
        return "already-absent"
    parameters = _stack_parameters(
        stack,
        stack_name=CONTROL_PLANE_STACK,
    )
    if parameters.get("ControlPlaneVerifiedImageUri") != control["targetImage"]:
        raise AgentCoreDeploymentError("unexpected control-plane stack cannot be removed")
    transition = metadata.get("transition")
    transition_id = transition.get("transitionId") if isinstance(transition, dict) else None
    if not isinstance(transition_id, str) or parameters.get("DeploymentTransitionId") != transition_id:
        raise AgentCoreDeploymentError("unexpected control-plane stack is not owned by this transition")
    if stack.get("StackStatus") not in {
        "CREATE_FAILED",
        "DELETE_FAILED",
        "ROLLBACK_COMPLETE",
        "ROLLBACK_FAILED",
    }:
        outputs = _outputs_from_stack(
            stack,
            CONTROL_PLANE_STACK,
        )
        _validate_control_plane_outputs(
            outputs,
            runtime_outputs=runtime_outputs,
            expected_image=control["targetImage"],
            expected_endpoint_mode=target_endpoint_mode,
        )
    _delete_new_control_plane(
        cloudformation_client,
        elbv2_client,
        stack,
    )
    (outputs_dir / "control-plane-outputs.json").unlink(missing_ok=True)
    return "removed"


def rollback_control_plane(
    config: AgentCoreSetupConfig,
    *,
    outputs_dir: Path,
    boto3_session: Any | None = None,
) -> None:
    """Restore the exact pre-transition control-plane parameters."""
    metadata = _promotion_metadata(outputs_dir / "promotion.json")
    if metadata.get("schemaVersion") not in {2, 3}:
        raise AgentCoreDeploymentError("control-plane rollback requires transition schema 2")
    if metadata.get("region") != config.aws_region:
        raise AgentCoreDeploymentError("control-plane rollback region differs from transition metadata")
    if boto3_session is None:
        import boto3

        boto3_session = boto3.Session(region_name=config.aws_region)
    cloudformation_client = boto3_session.client(
        "cloudformation",
        region_name=config.aws_region,
    )
    control_metadata = metadata.get("controlPlane")
    elbv2_client = (
        boto3_session.client(
            "elbv2",
            region_name=config.aws_region,
        )
        if isinstance(control_metadata, dict) and control_metadata.get("stackExisted") is False
        else None
    )
    runtime_outputs = _existing_agentcore_outputs(cloudformation_client)
    if runtime_outputs is None or not _runtime_matches_promotion(
        runtime_outputs,
        metadata,
    ):
        raise AgentCoreDeploymentError("runtime no longer matches control-plane rollback metadata")
    outcome = _reconcile_control_plane_transition(
        cloudformation_client,
        elbv2_client,
        metadata=metadata,
        runtime_outputs=runtime_outputs,
        outputs_dir=outputs_dir,
    )
    _write_private_json(
        outputs_dir / "control-plane-rollback.json",
        {
            "outcome": outcome,
            "runtimeArn": metadata["runtimeArn"],
            "schemaVersion": 1,
        },
    )
    print(f"Control-plane transition rollback: {outcome}.")


def _promotion_metadata(path: Path) -> dict[str, Any]:
    try:
        file_stat = path.lstat()
        if path.is_symlink() or not path.is_file():
            raise OSError("promotion metadata is not a regular file")
        value = json.loads(path.read_text(encoding="utf-8"))
        final_stat = path.stat()
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise AgentCoreDeploymentError("cannot read trusted promotion metadata") from exc
    if (
        file_stat.st_dev != final_stat.st_dev
        or file_stat.st_ino != final_stat.st_ino
        or file_stat.st_size != final_stat.st_size
        or not isinstance(value, dict)
    ):
        raise AgentCoreDeploymentError("promotion metadata is malformed")
    schema_version = value.get("schemaVersion")
    common_fields = {
        "candidateEndpointName",
        "candidateRuntimeVersion",
        "previousProductionRuntimeVersion",
        "productionEndpointArn",
        "productionRuntimeVersion",
        "providerSecretVersion",
        "runtimeArn",
        "schemaVersion",
    }
    enhanced_fields = {
        "controlPlane",
        "enabledProviders",
        "region",
        "sharedRuntimeConfiguration",
    }
    expected_fields = common_fields
    if schema_version in {2, 3}:
        expected_fields |= enhanced_fields
    if schema_version == 3:
        expected_fields |= {"transition"}
    if schema_version not in {1, 2, 3} or set(value) != expected_fields:
        raise AgentCoreDeploymentError("promotion metadata is malformed")
    for name in (
        "candidateEndpointName",
        "candidateRuntimeVersion",
        "productionEndpointArn",
        "productionRuntimeVersion",
        "providerSecretVersion",
        "runtimeArn",
    ):
        item = value.get(name)
        if (
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or any(character.isspace() for character in item)
        ):
            raise AgentCoreDeploymentError("promotion metadata is malformed")
    if (
        _CANDIDATE_ENDPOINT_PATTERN.fullmatch(value["candidateEndpointName"]) is None
        or not value["candidateRuntimeVersion"].isdigit()
        or value["candidateRuntimeVersion"].startswith("0")
        or value["productionRuntimeVersion"] != value["candidateRuntimeVersion"]
        or value["productionEndpointArn"] != f"{value['runtimeArn']}/runtime-endpoint/production"
    ):
        raise AgentCoreDeploymentError("promotion metadata is malformed")
    previous = value.get("previousProductionRuntimeVersion")
    if previous is not None and (
        not isinstance(previous, str) or not previous.isdigit() or previous.startswith("0") or len(previous) > 32
    ):
        raise AgentCoreDeploymentError("promotion metadata is malformed")
    if schema_version in {2, 3}:
        for name in ("enabledProviders", "region"):
            item = value.get(name)
            if (
                not isinstance(item, str)
                or not item
                or item != item.strip()
                or any(character.isspace() for character in item)
            ):
                raise AgentCoreDeploymentError("promotion metadata is malformed")
        shared = value.get("sharedRuntimeConfiguration")
        if (
            not isinstance(shared, dict)
            or set(shared)
            != {
                "AlarmNotificationEmail",
                "ApprovedHttpsPrefixListId",
                "AthenaConfigurationFingerprint",
                "BedrockInvokeResourceArns",
            }
            or any(
                not isinstance(item, str) or item != item.strip() or any(character.isspace() for character in item)
                for item in shared.values()
            )
        ):
            raise AgentCoreDeploymentError("promotion metadata is malformed")
        control = value.get("controlPlane")
        if control is not None:
            required_control_fields = {
                "previousParameters",
                "previousStackId",
                "stackExisted",
                "targetImage",
            }
            if (
                not isinstance(control, dict)
                or frozenset(control)
                not in {
                    frozenset(required_control_fields),
                    frozenset(
                        {
                            *required_control_fields,
                            "targetEndpointMode",
                        }
                    ),
                }
                or not isinstance(control.get("stackExisted"), bool)
                or not isinstance(control.get("targetImage"), str)
                or not control["targetImage"]
                or any(character.isspace() for character in control["targetImage"])
                or control.get(
                    "targetEndpointMode",
                    CUSTOM_DOMAIN,
                )
                not in {CUSTOM_DOMAIN, CLOUDFRONT}
            ):
                raise AgentCoreDeploymentError("promotion metadata is malformed")
            previous_parameters = control.get("previousParameters")
            previous_stack_id = control.get("previousStackId")
            if control["stackExisted"]:
                if (
                    not isinstance(previous_parameters, dict)
                    or not previous_parameters
                    or any(
                        not isinstance(name, str)
                        or not name
                        or not isinstance(item, str)
                        or any(character.isspace() for character in name)
                        for name, item in previous_parameters.items()
                    )
                    or not isinstance(previous_stack_id, str)
                    or not previous_stack_id.startswith("arn:")
                    or any(character.isspace() for character in previous_stack_id)
                ):
                    raise AgentCoreDeploymentError("promotion metadata is malformed")
            elif previous_parameters is not None or previous_stack_id is not None:
                raise AgentCoreDeploymentError("promotion metadata is malformed")
    if schema_version == 3:
        transition = value.get("transition")
        if not isinstance(transition, dict):
            raise AgentCoreDeploymentError("promotion metadata is malformed")
        _validated_transition_identity(transition)
    return value


def finalize_promotion(
    config: AgentCoreSetupConfig,
    *,
    outputs_dir: Path,
    boto3_session: Any | None = None,
) -> None:
    """Idempotently remove the candidate qualifier after a committed release."""
    outputs_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.chmod(0o700)
    metadata = _promotion_metadata(outputs_dir / "promotion.json")
    if metadata.get("schemaVersion") not in {2, 3}:
        raise AgentCoreDeploymentError("promotion finalization requires versioned transition metadata")
    if metadata["region"] != config.aws_region:
        raise AgentCoreDeploymentError("promotion finalization region differs from transition metadata")
    if boto3_session is None:
        import boto3

        boto3_session = boto3.Session(region_name=config.aws_region)
    cloudformation_client = boto3_session.client(
        "cloudformation",
        region_name=config.aws_region,
    )
    before = _existing_agentcore_outputs(cloudformation_client)
    if before is None:
        raise AgentCoreDeploymentError("AgentCore stack does not exist")
    candidate_version = metadata["candidateRuntimeVersion"]
    if (
        not _runtime_matches_promotion(before, metadata)
        or _production_runtime_version(before) != candidate_version
        or before.get("ProductionRuntimeVersion") != candidate_version
        or before.get("RuntimeEndpointName") != "production"
        or before.get("RuntimeEndpointArn") != metadata["productionEndpointArn"]
    ):
        raise AgentCoreDeploymentError("production no longer points to the certified promotion")

    candidate_outputs = {
        "CandidateRuntimeEndpointArn": (
            f"{metadata['runtimeArn']}/runtime-endpoint/{metadata['candidateEndpointName']}"
        ),
        "CandidateRuntimeEndpointName": metadata["candidateEndpointName"],
        "CandidateRuntimeVersion": candidate_version,
    }
    present_candidate_outputs = {name for name in candidate_outputs if name in before}
    if present_candidate_outputs and (
        present_candidate_outputs != set(candidate_outputs)
        or any(before.get(name) != value for name, value in candidate_outputs.items())
    ):
        raise AgentCoreDeploymentError("candidate endpoint no longer matches the certified promotion")

    if present_candidate_outputs:
        after = _update_endpoint_publication(
            cloudformation_client,
            candidate_endpoint_name=metadata["candidateEndpointName"],
            publish_candidate=False,
            production_version=candidate_version,
        )
        outcome = "finalized"
    else:
        after = before
        outcome = "already-finalized"

    if (
        not _runtime_matches_promotion(after, metadata)
        or _production_runtime_version(after) != candidate_version
        or after.get("ProductionRuntimeVersion") != candidate_version
        or after.get("RuntimeEndpointName") != "production"
        or after.get("RuntimeEndpointArn") != metadata["productionEndpointArn"]
        or any(name in after for name in candidate_outputs)
    ):
        raise AgentCoreDeploymentError("promotion finalization changed the certified production binding")
    _write_private_json(
        outputs_dir / "agentcore-outputs.json",
        {AGENTCORE_STACK: after},
    )
    _write_private_json(
        outputs_dir / "promotion-finalization.json",
        {
            "candidateEndpointName": metadata["candidateEndpointName"],
            "candidateRuntimeVersion": candidate_version,
            "outcome": outcome,
            "productionEndpointArn": metadata["productionEndpointArn"],
            "productionRuntimeVersion": candidate_version,
            "providerSecretVersion": metadata["providerSecretVersion"],
            "runtimeArn": metadata["runtimeArn"],
            "schemaVersion": 1,
        },
    )
    print(f"Production promotion finalized at runtime version {candidate_version} ({outcome}).")


def rollback_promotion(
    config: AgentCoreSetupConfig,
    *,
    outputs_dir: Path,
    assume_yes: bool,
    runner: CommandRunner = _run_command,
    boto3_session: Any | None = None,
) -> None:
    """Idempotently restore every live component in a prepared transition."""
    del assume_yes, runner
    metadata = _promotion_metadata(outputs_dir / "promotion.json")
    candidate_version = metadata["candidateRuntimeVersion"]
    candidate_endpoint_name = metadata["candidateEndpointName"]
    previous_version = metadata["previousProductionRuntimeVersion"] or ""
    if metadata["productionRuntimeVersion"] != candidate_version or previous_version == candidate_version:
        raise AgentCoreDeploymentError("promotion metadata does not describe a reversible transition")
    if boto3_session is None:
        import boto3

        boto3_session = boto3.Session(region_name=config.aws_region)
    cloudformation_client = boto3_session.client(
        "cloudformation",
        region_name=config.aws_region,
    )
    control_metadata = metadata.get("controlPlane")
    elbv2_client = (
        boto3_session.client(
            "elbv2",
            region_name=config.aws_region,
        )
        if isinstance(control_metadata, dict) and control_metadata.get("stackExisted") is False
        else None
    )
    before = _existing_agentcore_outputs(cloudformation_client)
    if before is None:
        raise AgentCoreDeploymentError("AgentCore stack does not exist")
    if metadata.get("schemaVersion") in {2, 3} and (metadata.get("region") != config.aws_region):
        raise AgentCoreDeploymentError("promotion rollback region differs from transition metadata")
    if not _runtime_matches_promotion(before, metadata):
        raise AgentCoreDeploymentError("production no longer matches the promotion being rolled back")
    current_production = _production_runtime_version(before)
    if current_production not in {
        candidate_version,
        previous_version,
    }:
        raise AgentCoreDeploymentError("production points outside the prepared transition")
    control_outcome = _reconcile_control_plane_transition(
        cloudformation_client,
        elbv2_client,
        metadata=metadata,
        runtime_outputs=before,
        outputs_dir=outputs_dir,
    )
    candidate_published = (
        before.get("CandidateRuntimeVersion") == candidate_version
        and before.get("CandidateRuntimeEndpointName") == candidate_endpoint_name
        and before.get("CandidateRuntimeEndpointArn")
        == (f"{metadata['runtimeArn']}/runtime-endpoint/{candidate_endpoint_name}")
    )
    if current_production == candidate_version and not candidate_published:
        raise AgentCoreDeploymentError("promoted runtime has no matching candidate endpoint")
    if candidate_published:
        after = _update_endpoint_publication(
            cloudformation_client,
            candidate_endpoint_name=candidate_endpoint_name,
            publish_candidate=False,
            production_version=previous_version,
        )
    else:
        after = before
    if _production_runtime_version(after) != previous_version:
        raise AgentCoreDeploymentError("promotion rollback did not restore the prior production version")
    if (
        "CandidateRuntimeEndpointArn" in after
        or "CandidateRuntimeEndpointName" in after
        or "CandidateRuntimeVersion" in after
    ):
        raise AgentCoreDeploymentError("promotion rollback left the candidate endpoint published")
    if (
        _required_output(after, "ProviderSecretVersion") != metadata["providerSecretVersion"]
        or _required_output(after, "RuntimeArn") != metadata["runtimeArn"]
    ):
        raise AgentCoreDeploymentError("promotion rollback changed the reviewed runtime binding")
    if metadata.get("schemaVersion") in {2, 3}:
        if after.get("EnabledProviders") != metadata["enabledProviders"] or any(
            after.get(name) != value for name, value in metadata["sharedRuntimeConfiguration"].items()
        ):
            raise AgentCoreDeploymentError("promotion rollback changed the reviewed runtime configuration")
    else:
        if after.get("EnabledProviders") != ",".join(config.runtime.enabled_providers):
            raise AgentCoreDeploymentError("promotion rollback changed the provider allowlist")
        _validate_shared_runtime_configuration(config, after)
    _write_private_json(
        outputs_dir / "agentcore-outputs.json",
        {AGENTCORE_STACK: after},
    )
    _write_private_json(
        outputs_dir / "promotion-rollback.json",
        {
            "candidateEndpointName": candidate_endpoint_name,
            "removedCandidateRuntimeVersion": candidate_version,
            "restoredProductionRuntimeVersion": (previous_version or None),
            "controlPlaneOutcome": control_outcome,
            "runtimeArn": metadata["runtimeArn"],
            "schemaVersion": 1,
        },
    )
    restored = previous_version or "no published production endpoint"
    print(f"Production promotion rolled back to {restored}.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deploy a validated AxonLLM AgentCore setup",
    )
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--outputs-dir",
        help=("Private deployment evidence directory; defaults to a namespace-specific path"),
    )
    parser.add_argument(
        "--deployment-namespace",
        help=("Bounded namespace for an isolated qualification deployment; omit for production"),
    )
    parser.add_argument(
        "--rehearsal-control-table-arn",
        help=(
            "Exact axonllm-rehearsal-control-ledger table ARN; required "
            "for qualification namespaces and forbidden for production"
        ),
    )
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--bootstrap-cdk", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--show-config", action="store_true")
    parser.add_argument(
        "--provider-env-file",
        help=(
            "Owner-only env file read as data for allowlisted provider "
            "credentials; process environment values take precedence"
        ),
    )
    parser.add_argument(
        "--rollback-provider-secret-version",
        help=("Move AWSCURRENT to a reviewed prior version and publish a fresh runtime version"),
    )
    endpoint_action = parser.add_mutually_exclusive_group()
    endpoint_action.add_argument(
        "--prepare-candidate-promotion-version",
        help=("Validate this exact candidate and write promotion.json before the live endpoint changes"),
    )
    endpoint_action.add_argument(
        "--promote-candidate-version",
        help=("Publish production only after this exact candidate version has passed certification"),
    )
    endpoint_action.add_argument(
        "--discard-candidate-version",
        help=("Remove this failed candidate endpoint while preserving the current production version"),
    )
    endpoint_action.add_argument(
        "--finalize-promotion",
        action="store_true",
        help=(
            "Use trusted promotion.json metadata to remove only the temporary "
            "candidate endpoint after immutable deployment evidence exists"
        ),
    )
    endpoint_action.add_argument(
        "--rollback-promotion",
        action="store_true",
        help=(
            "Use promotion.json to restore the prior production version and "
            "remove the promoted candidate after a later gate fails"
        ),
    )
    endpoint_action.add_argument(
        "--deploy-control-plane",
        action="store_true",
        help=("Deploy the reviewed control plane after candidate certification and production promotion"),
    )
    endpoint_action.add_argument(
        "--rollback-control-plane",
        action="store_true",
        help=("Restore only the pre-transition control-plane parameters from promotion.json"),
    )
    parser.add_argument(
        "--candidate-endpoint-name",
        help=("Exact high-entropy candidate qualifier recorded by certification; required for promotion or discard"),
    )
    parser.add_argument(
        "--transition-context",
        help=(
            "Owner-only JSON identity for a protected promotion journal; valid "
            "only while preparing or applying the same candidate promotion"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    previous_names: DeploymentNames | None = None
    try:
        previous_names = _activate_deployment_namespace(args.deployment_namespace)
        config = load_agentcore_setup(args.config)
        rehearsal_control_table_arn = validate_rehearsal_control_table_arn(
            aws_region=config.aws_region,
            deployment_namespace=(_ACTIVE_DEPLOYMENT_NAMESPACE),
            rehearsal_control_table_arn=(args.rehearsal_control_table_arn),
        )
        print(f"Validated authenticated AgentCore configuration: {config.identity_mode}, {config.aws_region}.")
        if args.show_config:
            print(
                json.dumps(
                    redact_sensitive(config.to_dict()),
                    indent=2,
                    sort_keys=True,
                )
            )
        if args.validate_only:
            return 0
        if not args.yes and not sys.stdin.isatty():
            raise AgentCoreDeploymentError("non-interactive deployment requires --yes after reviewing the CDK diff")
        default_outputs_dir = (
            ".axonllm/agentcore"
            if not _ACTIVE_DEPLOYMENT_NAMESPACE
            else (f".axonllm/agentcore-{_ACTIVE_DEPLOYMENT_NAMESPACE}")
        )
        outputs_dir = Path(args.outputs_dir or default_outputs_dir).expanduser().resolve()
        transition = (
            _load_transition_identity(Path(args.transition_context).expanduser().resolve())
            if args.transition_context is not None
            else None
        )
        if transition is not None and not (args.prepare_candidate_promotion_version or args.promote_candidate_version):
            raise AgentCoreDeploymentError(
                "--transition-context is valid only with promotion preparation or candidate promotion"
            )
        if args.prepare_candidate_promotion_version:
            if args.candidate_endpoint_name is None:
                raise AgentCoreDeploymentError("promotion preparation requires --candidate-endpoint-name")
            if args.bootstrap_cdk or args.provider_env_file or args.rollback_provider_secret_version:
                raise AgentCoreDeploymentError(
                    "promotion preparation does not accept bootstrap or provider-secret mutation options"
                )
            prepare_candidate_promotion(
                config,
                candidate_version=(args.prepare_candidate_promotion_version),
                candidate_endpoint_name=args.candidate_endpoint_name,
                outputs_dir=outputs_dir,
                transition=transition,
            )
        elif args.promote_candidate_version:
            if args.candidate_endpoint_name is None:
                raise AgentCoreDeploymentError("candidate promotion requires --candidate-endpoint-name")
            if args.bootstrap_cdk or args.provider_env_file or args.rollback_provider_secret_version:
                raise AgentCoreDeploymentError(
                    "candidate promotion does not accept bootstrap or provider-secret mutation options"
                )
            promote_candidate(
                config,
                candidate_version=args.promote_candidate_version,
                candidate_endpoint_name=args.candidate_endpoint_name,
                outputs_dir=outputs_dir,
                assume_yes=args.yes,
                transition=transition,
            )
        elif args.discard_candidate_version:
            if args.candidate_endpoint_name is None:
                raise AgentCoreDeploymentError("candidate discard requires --candidate-endpoint-name")
            if args.bootstrap_cdk or args.provider_env_file or args.rollback_provider_secret_version:
                raise AgentCoreDeploymentError(
                    "candidate discard does not accept bootstrap or provider-secret mutation options"
                )
            discard_candidate(
                config,
                candidate_version=args.discard_candidate_version,
                candidate_endpoint_name=args.candidate_endpoint_name,
                outputs_dir=outputs_dir,
                assume_yes=args.yes,
            )
        elif args.finalize_promotion:
            if args.candidate_endpoint_name is not None:
                raise AgentCoreDeploymentError(
                    "promotion finalization reads the candidate endpoint from trusted promotion metadata"
                )
            if args.bootstrap_cdk or args.provider_env_file or args.rollback_provider_secret_version:
                raise AgentCoreDeploymentError(
                    "promotion finalization does not accept bootstrap or provider-secret mutation options"
                )
            finalize_promotion(
                config,
                outputs_dir=outputs_dir,
            )
        elif args.rollback_promotion:
            if args.candidate_endpoint_name is not None:
                raise AgentCoreDeploymentError(
                    "promotion rollback reads the candidate endpoint from trusted promotion metadata"
                )
            if args.bootstrap_cdk or args.provider_env_file or args.rollback_provider_secret_version:
                raise AgentCoreDeploymentError(
                    "promotion rollback does not accept bootstrap or provider-secret mutation options"
                )
            rollback_promotion(
                config,
                outputs_dir=outputs_dir,
                assume_yes=args.yes,
            )
        elif args.deploy_control_plane:
            if args.candidate_endpoint_name is not None:
                raise AgentCoreDeploymentError(
                    "control-plane deployment reads the transition from trusted promotion metadata"
                )
            if args.bootstrap_cdk or args.provider_env_file or args.rollback_provider_secret_version:
                raise AgentCoreDeploymentError(
                    "control-plane deployment does not accept bootstrap or provider-secret mutation options"
                )
            deploy_control_plane(
                config,
                outputs_dir=outputs_dir,
                assume_yes=args.yes,
                rehearsal_control_table_arn=(rehearsal_control_table_arn),
            )
        elif args.rollback_control_plane:
            if args.candidate_endpoint_name is not None:
                raise AgentCoreDeploymentError(
                    "control-plane rollback reads the transition from trusted promotion metadata"
                )
            if args.bootstrap_cdk or args.provider_env_file or args.rollback_provider_secret_version:
                raise AgentCoreDeploymentError(
                    "control-plane rollback does not accept bootstrap or provider-secret mutation options"
                )
            rollback_control_plane(
                config,
                outputs_dir=outputs_dir,
            )
        else:
            if args.candidate_endpoint_name is not None:
                raise AgentCoreDeploymentError(
                    "--candidate-endpoint-name is only valid with candidate "
                    "promotion preparation, promotion, or discard"
                )
            deploy(
                config,
                outputs_dir=outputs_dir,
                assume_yes=args.yes,
                bootstrap_cdk=args.bootstrap_cdk,
                provider_environment=_provider_environment(
                    (Path(args.provider_env_file) if args.provider_env_file else None)
                ),
                provider_secret_rollback_version=(args.rollback_provider_secret_version),
                rehearsal_control_table_arn=(rehearsal_control_table_arn),
            )
    except (
        AgentCoreSetupError,
        AgentCoreDeploymentError,
        ProviderSecretError,
    ) as exc:
        parser.error(str(exc))
    finally:
        if previous_names is not None:
            _activate_deployment_namespace(previous_names.namespace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
