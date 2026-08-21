"""Install and verify the dedicated release-foundation CDK trust domain."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Sequence

from src.gateway.deployment.release_foundation_policy import (
    EXECUTION_POLICY_PART_COUNT,
    FOUNDATION_QUALIFIER,
    IAM_MANAGED_POLICY_SIZE_LIMIT,
    TOOLKIT_STACK_NAME,
    bootstrap_boundary_arn,
    bootstrap_boundary_document,
    bootstrap_boundary_name,
    execution_policy_arn,
    execution_policy_documents,
    execution_policy_name,
    service_boundary_arn,
    service_boundary_document,
    service_boundary_name,
)


_REPO_ROOT = Path(__file__).resolve().parents[3]
_INFRA_ROOT = _REPO_ROOT / "infra"
_DEFAULT_CDK_CLI = (
    _REPO_ROOT
    / "src"
    / "gateway"
    / "deployment"
    / "infra"
    / "node_modules"
    / ".bin"
    / "cdk"
)
_POLICY_TAGS = {
    "Application": "AxonLLM",
    "Qualifier": FOUNDATION_QUALIFIER,
}
_BOOTSTRAP_TEMPLATE_VERSION = "32"
_BOOTSTRAP_ROLE_PURPOSES = (
    "cfn-exec",
    "deploy",
    "file-publishing",
    "image-publishing",
    "lookup",
)


class ReleaseFoundationBootstrapError(RuntimeError):
    """Raised when the bounded release-foundation authority is invalid."""


@dataclass(frozen=True)
class AwsIdentity:
    """Caller identity required to derive exact IAM resource names."""

    account_id: str
    partition: str


def _canonical(document: object) -> str:
    if not isinstance(document, dict):
        raise ReleaseFoundationBootstrapError(
            "IAM returned a malformed policy document"
        )
    return json.dumps(
        document,
        separators=(",", ":"),
        sort_keys=True,
    )


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    error = response.get("Error")
    if not isinstance(error, dict):
        return ""
    code = error.get("Code")
    return code if isinstance(code, str) else ""


def _identity(session: Any, *, region: str) -> AwsIdentity:
    try:
        response = session.client(
            "sts",
            region_name=region,
        ).get_caller_identity()
    except Exception as exc:
        raise ReleaseFoundationBootstrapError(
            "could not resolve the AWS caller identity"
        ) from exc
    account_id = response.get("Account")
    arn = response.get("Arn")
    if (
        not isinstance(account_id, str)
        or len(account_id) != 12
        or not account_id.isdigit()
        or not isinstance(arn, str)
        or not arn.startswith("arn:")
    ):
        raise ReleaseFoundationBootstrapError(
            "AWS returned an invalid caller identity"
        )
    partition = arn.split(":", 2)[1]
    if partition not in {"aws", "aws-us-gov", "aws-cn"}:
        raise ReleaseFoundationBootstrapError(
            "AWS returned an unsupported partition"
        )
    return AwsIdentity(
        account_id=account_id,
        partition=partition,
    )


def _policy_tags(iam_client: Any, *, policy_arn: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    marker: str | None = None
    for _ in range(100):
        arguments: dict[str, str] = {"PolicyArn": policy_arn}
        if marker is not None:
            arguments["Marker"] = marker
        response = iam_client.list_policy_tags(**arguments)
        values = response.get("Tags")
        if not isinstance(values, list):
            raise ReleaseFoundationBootstrapError(
                "IAM returned malformed managed-policy tags"
            )
        for value in values:
            key = value.get("Key") if isinstance(value, dict) else None
            item = value.get("Value") if isinstance(value, dict) else None
            if not isinstance(key, str) or not isinstance(item, str):
                raise ReleaseFoundationBootstrapError(
                    "IAM returned malformed managed-policy tags"
                )
            tags[key] = item
        if not response.get("IsTruncated"):
            return tags
        raw_marker = response.get("Marker")
        if not isinstance(raw_marker, str) or not raw_marker:
            raise ReleaseFoundationBootstrapError(
                "IAM truncated managed-policy tags without a marker"
            )
        marker = raw_marker
    raise ReleaseFoundationBootstrapError(
        "IAM returned too many managed-policy tag pages"
    )


def _replace_policy_version(
    iam_client: Any,
    *,
    policy_arn: str,
    document: dict[str, Any],
) -> None:
    versions = iam_client.list_policy_versions(
        PolicyArn=policy_arn,
    ).get("Versions")
    if not isinstance(versions, list):
        raise ReleaseFoundationBootstrapError(
            "IAM returned malformed managed-policy versions"
        )
    version_ids: set[str] = set()
    previous_default_id: str | None = None
    for value in versions:
        version_id = (
            value.get("VersionId")
            if isinstance(value, dict)
            else None
        )
        if (
            not isinstance(version_id, str)
            or not version_id
            or not version_id.startswith("v")
            or not version_id[1:].isdigit()
            or version_id in version_ids
        ):
            raise ReleaseFoundationBootstrapError(
                "IAM returned malformed managed-policy versions"
            )
        version_ids.add(version_id)
        if value.get("IsDefaultVersion"):
            if previous_default_id is not None:
                raise ReleaseFoundationBootstrapError(
                    "IAM policy has multiple default versions"
                )
            previous_default_id = version_id
    if previous_default_id is None:
        raise ReleaseFoundationBootstrapError(
            "IAM policy has no default version"
        )
    nondefault = [
        value
        for value in versions
        if not value.get("IsDefaultVersion")
    ]
    if len(versions) >= 5:
        oldest = min(
            nondefault,
            key=lambda value: int(value["VersionId"][1:]),
            default=None,
        )
        version_id = (
            oldest.get("VersionId")
            if isinstance(oldest, dict)
            else None
        )
        if not isinstance(version_id, str) or not version_id:
            raise ReleaseFoundationBootstrapError(
                "IAM policy has no removable nondefault version"
            )
        iam_client.delete_policy_version(
            PolicyArn=policy_arn,
            VersionId=version_id,
        )
    created = iam_client.create_policy_version(
        PolicyArn=policy_arn,
        PolicyDocument=_canonical(document),
        SetAsDefault=True,
    ).get("PolicyVersion")
    if (
        not isinstance(created, dict)
        or not isinstance(created.get("VersionId"), str)
    ):
        raise ReleaseFoundationBootstrapError(
            "IAM did not return the new managed-policy version"
        )
    refreshed = iam_client.list_policy_versions(
        PolicyArn=policy_arn,
    ).get("Versions")
    if not isinstance(refreshed, list):
        raise ReleaseFoundationBootstrapError(
            "IAM returned malformed managed-policy versions"
        )
    for value in refreshed:
        if not isinstance(value, dict):
            raise ReleaseFoundationBootstrapError(
                "IAM returned a malformed managed-policy version"
            )
        if value.get("IsDefaultVersion"):
            continue
        version_id = value.get("VersionId")
        if not isinstance(version_id, str) or not version_id:
            raise ReleaseFoundationBootstrapError(
                "IAM returned a malformed managed-policy version"
            )
        if version_id == previous_default_id:
            continue
        iam_client.delete_policy_version(
            PolicyArn=policy_arn,
            VersionId=version_id,
        )


def _ensure_managed_policy(
    iam_client: Any,
    *,
    policy_arn: str,
    policy_name: str,
    description: str,
    document: dict[str, Any],
    purpose: str,
    apply: bool,
) -> None:
    expected_tags = {
        **_POLICY_TAGS,
        "Purpose": purpose,
    }
    try:
        policy = iam_client.get_policy(
            PolicyArn=policy_arn,
        ).get("Policy")
    except Exception as exc:
        if _error_code(exc) != "NoSuchEntity":
            raise ReleaseFoundationBootstrapError(
                f"could not inspect {purpose}"
            ) from exc
        if not apply:
            raise ReleaseFoundationBootstrapError(
                f"{purpose} is absent: {policy_arn}"
            ) from exc
        created = iam_client.create_policy(
            PolicyName=policy_name,
            Description=description,
            PolicyDocument=_canonical(document),
            Tags=[
                {"Key": key, "Value": value}
                for key, value in sorted(expected_tags.items())
            ],
        )
        policy = created.get("Policy")
    if not isinstance(policy, dict) or policy.get("Arn") != policy_arn:
        raise ReleaseFoundationBootstrapError(
            f"IAM returned invalid {purpose} metadata"
        )
    version_id = policy.get("DefaultVersionId")
    if not isinstance(version_id, str) or not version_id:
        raise ReleaseFoundationBootstrapError(
            f"IAM returned invalid {purpose} version metadata"
        )
    version = iam_client.get_policy_version(
        PolicyArn=policy_arn,
        VersionId=version_id,
    ).get("PolicyVersion")
    actual = version.get("Document") if isinstance(version, dict) else None
    if _canonical(actual) != _canonical(document):
        if not apply:
            raise ReleaseFoundationBootstrapError(
                f"{purpose} differs from the repository contract"
            )
        _replace_policy_version(
            iam_client,
            policy_arn=policy_arn,
            document=document,
        )

    actual_tags = _policy_tags(
        iam_client,
        policy_arn=policy_arn,
    )
    if actual_tags != expected_tags:
        if not apply:
            raise ReleaseFoundationBootstrapError(
                f"{purpose} tags differ from the repository contract"
            )
        remove = sorted(set(actual_tags) - set(expected_tags))
        if remove:
            iam_client.untag_policy(
                PolicyArn=policy_arn,
                TagKeys=remove,
            )
        iam_client.tag_policy(
            PolicyArn=policy_arn,
            Tags=[
                {"Key": key, "Value": value}
                for key, value in sorted(expected_tags.items())
            ],
        )


def ensure_policy_set(
    session: Any,
    *,
    identity: AwsIdentity,
    region: str,
    apply: bool,
) -> tuple[str, ...]:
    """Create or verify the exact repository-owned IAM policy set."""
    service_document = service_boundary_document(
        partition=identity.partition,
        account_id=identity.account_id,
        region=region,
    )
    bootstrap_document = bootstrap_boundary_document(
        partition=identity.partition,
        account_id=identity.account_id,
        region=region,
    )
    documents = execution_policy_documents(
        partition=identity.partition,
        account_id=identity.account_id,
        region=region,
    )
    for purpose, document in (
        ("service-role boundary", service_document),
        ("bootstrap-role boundary", bootstrap_document),
        *(
            (f"execution policy part {part}", document)
            for part, document in enumerate(documents, start=1)
        ),
    ):
        if len(_canonical(document)) > IAM_MANAGED_POLICY_SIZE_LIMIT:
            raise ReleaseFoundationBootstrapError(
                f"{purpose} exceeds IAM's managed-policy size quota"
            )

    iam_client = session.client("iam", region_name=region)
    service_arn = service_boundary_arn(
        partition=identity.partition,
        account_id=identity.account_id,
        region=region,
    )
    _ensure_managed_policy(
        iam_client,
        policy_arn=service_arn,
        policy_name=service_boundary_name(region),
        description=(
            "Mandatory anti-escalation boundary for AxonLLM "
            "release-foundation roles"
        ),
        document=service_document,
        purpose="ServiceRoleBoundary",
        apply=apply,
    )
    bootstrap_arn = bootstrap_boundary_arn(
        partition=identity.partition,
        account_id=identity.account_id,
        region=region,
    )
    _ensure_managed_policy(
        iam_client,
        policy_arn=bootstrap_arn,
        policy_name=bootstrap_boundary_name(region),
        description=(
            "Mandatory anti-escalation boundary for AxonLLM "
            "release-foundation CDK roles"
        ),
        document=bootstrap_document,
        purpose="BootstrapRoleBoundary",
        apply=apply,
    )
    arns: list[str] = []
    for part, document in enumerate(documents, start=1):
        policy_arn = execution_policy_arn(
            partition=identity.partition,
            account_id=identity.account_id,
            region=region,
            part=part,
        )
        arns.append(policy_arn)
        _ensure_managed_policy(
            iam_client,
            policy_arn=policy_arn,
            policy_name=execution_policy_name(region, part=part),
            description=(
                "Bounded CloudFormation execution for the AxonLLM release "
                f"foundation (part {part} of "
                f"{EXECUTION_POLICY_PART_COUNT})"
            ),
            document=document,
            purpose=f"CloudFormationExecutionPart{part}",
            apply=apply,
        )
    return tuple(arns)


def cdk_bootstrap_command(
    *,
    cdk_cli: Path,
    identity: AwsIdentity,
    region: str,
    execution_policy_arns: tuple[str, ...],
) -> list[str]:
    """Build the deterministic dedicated CDK bootstrap command."""
    command = [
        str(cdk_cli),
        "bootstrap",
        f"aws://{identity.account_id}/{region}",
        "-c",
        "deployment_target=release-foundation",
        "-c",
        f"region={region}",
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
            bootstrap_boundary_name(region),
            "--qualifier",
            FOUNDATION_QUALIFIER,
            "--termination-protection",
            "--toolkit-stack-name",
            TOOLKIT_STACK_NAME,
        ]
    )
    return command


def _apply_deploy_role_boundary(
    session: Any,
    *,
    identity: AwsIdentity,
    region: str,
) -> None:
    role_name = (
        f"cdk-{FOUNDATION_QUALIFIER}-deploy-role-"
        f"{identity.account_id}-{region}"
    )
    try:
        session.client(
            "iam",
            region_name=region,
        ).put_role_permissions_boundary(
            RoleName=role_name,
            PermissionsBoundary=bootstrap_boundary_arn(
                partition=identity.partition,
                account_id=identity.account_id,
                region=region,
            ),
        )
    except Exception as exc:
        raise ReleaseFoundationBootstrapError(
            "could not bound the dedicated CDK deploy role"
        ) from exc


def _attached_policy_arns(
    iam_client: Any,
    *,
    role_name: str,
) -> list[str]:
    values: list[str] = []
    marker: str | None = None
    for _ in range(100):
        arguments: dict[str, str] = {"RoleName": role_name}
        if marker is not None:
            arguments["Marker"] = marker
        response = iam_client.list_attached_role_policies(**arguments)
        policies = response.get("AttachedPolicies")
        if not isinstance(policies, list):
            raise ReleaseFoundationBootstrapError(
                "IAM returned malformed attached role policies"
            )
        for policy in policies:
            arn = policy.get("PolicyArn") if isinstance(policy, dict) else None
            if not isinstance(arn, str) or not arn:
                raise ReleaseFoundationBootstrapError(
                    "IAM returned malformed attached role policies"
                )
            values.append(arn)
        if not response.get("IsTruncated"):
            return values
        raw_marker = response.get("Marker")
        if not isinstance(raw_marker, str) or not raw_marker:
            raise ReleaseFoundationBootstrapError(
                "IAM truncated attached policies without a marker"
            )
        marker = raw_marker
    raise ReleaseFoundationBootstrapError(
        "IAM returned too many attached-policy pages"
    )


def _inline_policy_names(
    iam_client: Any,
    *,
    role_name: str,
) -> list[str]:
    values: list[str] = []
    marker: str | None = None
    for _ in range(100):
        arguments: dict[str, str] = {"RoleName": role_name}
        if marker is not None:
            arguments["Marker"] = marker
        response = iam_client.list_role_policies(**arguments)
        policies = response.get("PolicyNames")
        if not isinstance(policies, list) or not all(
            isinstance(value, str) and value
            for value in policies
        ):
            raise ReleaseFoundationBootstrapError(
                "IAM returned malformed inline role policies"
            )
        values.extend(policies)
        if not response.get("IsTruncated"):
            return values
        raw_marker = response.get("Marker")
        if not isinstance(raw_marker, str) or not raw_marker:
            raise ReleaseFoundationBootstrapError(
                "IAM truncated inline policies without a marker"
            )
        marker = raw_marker
    raise ReleaseFoundationBootstrapError(
        "IAM returned too many inline-policy pages"
    )


def _role_tags(
    iam_client: Any,
    *,
    role_name: str,
) -> dict[str, str]:
    values: dict[str, str] = {}
    marker: str | None = None
    for _ in range(100):
        arguments: dict[str, str] = {"RoleName": role_name}
        if marker is not None:
            arguments["Marker"] = marker
        response = iam_client.list_role_tags(**arguments)
        tags = response.get("Tags")
        if not isinstance(tags, list):
            raise ReleaseFoundationBootstrapError(
                "IAM returned malformed role tags"
            )
        for tag in tags:
            key = tag.get("Key") if isinstance(tag, dict) else None
            value = tag.get("Value") if isinstance(tag, dict) else None
            if (
                not isinstance(key, str)
                or not key
                or not isinstance(value, str)
                or key in values
            ):
                raise ReleaseFoundationBootstrapError(
                    "IAM returned malformed role tags"
                )
            values[key] = value
        if not response.get("IsTruncated"):
            return values
        raw_marker = response.get("Marker")
        if not isinstance(raw_marker, str) or not raw_marker:
            raise ReleaseFoundationBootstrapError(
                "IAM truncated role tags without a marker"
            )
        marker = raw_marker
    raise ReleaseFoundationBootstrapError(
        "IAM returned too many role-tag pages"
    )


def _actions(statement: dict[str, Any]) -> set[str]:
    raw = statement.get("Action")
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list) and all(
        isinstance(value, str) and value
        for value in raw
    ):
        return set(raw)
    raise ReleaseFoundationBootstrapError(
        "IAM returned malformed role trust"
    )


def _verify_role_trust(
    role: dict[str, Any],
    *,
    identity: AwsIdentity,
    purpose: str,
) -> None:
    document = role.get("AssumeRolePolicyDocument")
    statements = (
        document.get("Statement")
        if isinstance(document, dict)
        else None
    )
    if not isinstance(statements, list):
        raise ReleaseFoundationBootstrapError(
            f"dedicated CDK {purpose} role has malformed trust"
        )
    if purpose == "cfn-exec":
        valid = (
            len(statements) == 1
            and isinstance(statements[0], dict)
            and statements[0].get("Effect") == "Allow"
            and _actions(statements[0]) == {"sts:AssumeRole"}
            and statements[0].get("Principal")
            == {"Service": "cloudformation.amazonaws.com"}
            and "Condition" not in statements[0]
        )
    else:
        account_principals = {
            identity.account_id,
            (
                f"arn:{identity.partition}:iam::"
                f"{identity.account_id}:root"
            ),
        }
        by_action = {
            next(iter(_actions(statement))): statement
            for statement in statements
            if isinstance(statement, dict)
            and len(_actions(statement)) == 1
        }
        assume = by_action.get("sts:AssumeRole")
        tag = by_action.get("sts:TagSession")
        valid = (
            len(statements) == 2
            and isinstance(assume, dict)
            and isinstance(tag, dict)
            and assume.get("Effect") == "Allow"
            and tag.get("Effect") == "Allow"
            and assume.get("Principal", {}).get("AWS")
            in account_principals
            and tag.get("Principal", {}).get("AWS")
            in account_principals
            and assume.get("Condition")
            == {"Null": {"sts:ExternalId": "true"}}
            and "Condition" not in tag
        )
    if not valid:
        raise ReleaseFoundationBootstrapError(
            f"dedicated CDK {purpose} role has unexpected trust"
        )


def verify_bootstrap(
    session: Any,
    *,
    identity: AwsIdentity,
    region: str,
    expected_policy_arns: tuple[str, ...],
) -> None:
    """Require exact policies, boundaries, and termination protection."""
    iam_client = session.client("iam", region_name=region)
    expected_boundary = bootstrap_boundary_arn(
        partition=identity.partition,
        account_id=identity.account_id,
        region=region,
    )
    expected_inline = {
        "cfn-exec": set(),
        "deploy": {"default"},
        "file-publishing": {
            (
                f"cdk-{FOUNDATION_QUALIFIER}-file-publishing-role-"
                f"default-policy-{identity.account_id}-{region}"
            )
        },
        "image-publishing": {
            (
                f"cdk-{FOUNDATION_QUALIFIER}-image-publishing-role-"
                f"default-policy-{identity.account_id}-{region}"
            )
        },
        "lookup": {"LookupRolePolicy"},
    }
    aws_managed_prefix = (
        f"arn:{identity.partition}:iam::aws:policy/"
    )
    expected_attached = {
        "cfn-exec": set(expected_policy_arns),
        "deploy": {
            f"{aws_managed_prefix}AWSCloudFormationReadOnlyAccess"
        },
        "file-publishing": set(),
        "image-publishing": set(),
        "lookup": {f"{aws_managed_prefix}ReadOnlyAccess"},
    }
    for purpose in _BOOTSTRAP_ROLE_PURPOSES:
        role_name = (
            f"cdk-{FOUNDATION_QUALIFIER}-{purpose}-role-"
            f"{identity.account_id}-{region}"
        )
        try:
            role = iam_client.get_role(RoleName=role_name).get("Role")
            inline = set(
                _inline_policy_names(
                    iam_client,
                    role_name=role_name,
                )
            )
            attached = set(
                _attached_policy_arns(
                    iam_client,
                    role_name=role_name,
                )
            )
            tags = _role_tags(
                iam_client,
                role_name=role_name,
            )
        except Exception as exc:
            raise ReleaseFoundationBootstrapError(
                f"could not inspect dedicated CDK role {role_name}"
            ) from exc
        boundary = (
            role.get("PermissionsBoundary")
            if isinstance(role, dict)
            else None
        )
        expected_role_boundary = (
            expected_boundary
            if purpose in {"cfn-exec", "deploy"}
            else None
        )
        actual_role_boundary = (
            boundary.get("PermissionsBoundaryArn")
            if isinstance(boundary, dict)
            else None
        )
        if (
            not isinstance(role, dict)
            or actual_role_boundary != expected_role_boundary
            or (
                purpose in {"cfn-exec", "deploy"}
                and boundary.get("PermissionsBoundaryType") != "Policy"
            )
            or inline != expected_inline[purpose]
            or attached != expected_attached[purpose]
            or any(
                value.endswith("/AdministratorAccess")
                for value in attached
            )
            or (
                purpose != "cfn-exec"
                and tags.get("aws-cdk:bootstrap-role") != purpose
            )
        ):
            raise ReleaseFoundationBootstrapError(
                f"dedicated CDK role {role_name} has unexpected authority"
            )
        _verify_role_trust(
            role,
            identity=identity,
            purpose=purpose,
        )

    cloudformation = session.client(
        "cloudformation",
        region_name=region,
    )
    try:
        stacks = cloudformation.describe_stacks(
            StackName=TOOLKIT_STACK_NAME,
        ).get("Stacks")
    except Exception as exc:
        raise ReleaseFoundationBootstrapError(
            "could not inspect the dedicated CDK toolkit stack"
        ) from exc
    if (
        not isinstance(stacks, list)
        or len(stacks) != 1
        or stacks[0].get("EnableTerminationProtection") is not True
        or stacks[0].get("StackStatus")
        not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
    ):
        raise ReleaseFoundationBootstrapError(
            "dedicated CDK toolkit stack is not protected and stable"
        )
    parameter_name = f"/cdk-bootstrap/{FOUNDATION_QUALIFIER}/version"
    try:
        parameter = session.client(
            "ssm",
            region_name=region,
        ).get_parameter(Name=parameter_name).get("Parameter")
    except Exception as exc:
        raise ReleaseFoundationBootstrapError(
            "could not inspect the dedicated CDK bootstrap version"
        ) from exc
    if (
        not isinstance(parameter, dict)
        or parameter.get("Name") != parameter_name
        or parameter.get("Value") != _BOOTSTRAP_TEMPLATE_VERSION
    ):
        raise ReleaseFoundationBootstrapError(
            "dedicated CDK bootstrap version is unexpected"
        )


def _install(
    *,
    session: Any,
    region: str,
    cdk_cli: Path,
) -> None:
    if region != "us-east-1":
        raise ReleaseFoundationBootstrapError(
            "release foundation must be bootstrapped in us-east-1"
        )
    if not cdk_cli.is_file():
        raise ReleaseFoundationBootstrapError(
            f"pinned CDK CLI is missing: {cdk_cli}"
        )
    identity = _identity(session, region=region)
    policy_arns = ensure_policy_set(
        session,
        identity=identity,
        region=region,
        apply=True,
    )
    command = cdk_bootstrap_command(
        cdk_cli=cdk_cli,
        identity=identity,
        region=region,
        execution_policy_arns=policy_arns,
    )
    completed = subprocess.run(
        command,
        cwd=_INFRA_ROOT,
        check=False,
    )
    if completed.returncode != 0:
        raise ReleaseFoundationBootstrapError(
            "dedicated CDK bootstrap failed"
        )
    _apply_deploy_role_boundary(
        session,
        identity=identity,
        region=region,
    )
    verify_bootstrap(
        session,
        identity=identity,
        region=region,
        expected_policy_arns=policy_arns,
    )


def _verify(*, session: Any, region: str) -> None:
    if region != "us-east-1":
        raise ReleaseFoundationBootstrapError(
            "release foundation must be verified in us-east-1"
        )
    identity = _identity(session, region=region)
    policy_arns = ensure_policy_set(
        session,
        identity=identity,
        region=region,
        apply=False,
    )
    verify_bootstrap(
        session,
        identity=identity,
        region=region,
        expected_policy_arns=policy_arns,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Install or verify the bounded AxonLLM release-foundation "
            "CDK trust domain"
        )
    )
    parser.add_argument(
        "operation",
        choices=("install", "verify"),
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
    )
    parser.add_argument(
        "--cdk-cli",
        type=Path,
        default=_DEFAULT_CDK_CLI,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="required confirmation for the install operation",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    session: Any | None = None,
) -> int:
    """Run the release-foundation bootstrap command."""
    args = _parser().parse_args(argv)
    if args.operation == "install" and not args.apply:
        print(
            "release-foundation bootstrap install requires --apply",
            file=sys.stderr,
        )
        return 2
    if args.operation == "verify" and args.apply:
        print("--apply is valid only with install", file=sys.stderr)
        return 2
    if session is None:
        import boto3

        session = boto3.Session(region_name=args.region)
    try:
        if args.operation == "install":
            _install(
                session=session,
                region=args.region,
                cdk_cli=args.cdk_cli,
            )
            print(
                "release-foundation CDK bootstrap installed and verified"
            )
        else:
            _verify(
                session=session,
                region=args.region,
            )
            print("release-foundation CDK bootstrap verified")
    except ReleaseFoundationBootstrapError as exc:
        print(
            f"release-foundation bootstrap failed: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
