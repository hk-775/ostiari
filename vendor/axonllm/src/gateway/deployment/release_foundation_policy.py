"""Bounded CDK execution policy for the AxonLLM release foundation."""

from __future__ import annotations

import json
from typing import Any


FOUNDATION_QUALIFIER = "axrel"
TOOLKIT_STACK_NAME = f"AxonLLMToolkit-{FOUNDATION_QUALIFIER}"
EXECUTION_POLICY_NAME_PREFIX = (
    "AxonLLMReleaseFoundationCloudFormationExecution"
)
SERVICE_BOUNDARY_NAME_PREFIX = "AxonLLMReleaseFoundationRoleBoundary"
BOOTSTRAP_BOUNDARY_NAME_PREFIX = (
    "AxonLLMReleaseFoundationBootstrapBoundary"
)
EXECUTION_POLICY_PART_COUNT = 5
IAM_MANAGED_POLICY_SIZE_LIMIT = 6_144
_EXECUTION_POLICY_TARGET_SIZE = 5_900

_APPLICATION_TAG = "Application"
_TRUST_DOMAIN_TAG = "AxonLLMTrustDomain"

FOUNDATION_ROLE_NAMES = (
    "AxonLLMAgentCoreDeployRole",
    "AxonLLMAgentCoreQualificationRole",
    "AxonLLMAgentCoreRehearsalEvidenceRole",
    "AxonLLMAgentCoreTransitionWatchdogRole",
    "AxonLLMExternalOidcCertificationRole",
    "AxonLLMLaunchActionWorkerRole",
    "AxonLLMLaunchCleanupWorkerRole",
    "AxonLLMLaunchCoordinatorExecutionRole",
    "AxonLLMLaunchCoordinatorSchedulerRole",
    "AxonLLMLaunchGatesRole",
    "AxonLLMOperationsAudit",
    "AxonLLMOperationsRecovery",
    "AxonLLMProductionTransitionMutationBrokerRole",
    "AxonLLMQualificationMutationBrokerRole",
    "AxonLLMReleaseFoundationDeployRole",
    "AxonLLMReleasePublisher",
    "AxonLLMReleaseSigner",
    "AxonLLMReleaseVerifier",
)

_REGIONAL_ACTIONS = (
    "cloudwatch:DeleteAlarms",
    "cloudwatch:DescribeAlarms",
    "cloudwatch:ListTagsForResource",
    "cloudwatch:PutMetricAlarm",
    "cloudwatch:TagResource",
    "cloudwatch:UntagResource",
    "dynamodb:CreateTable",
    "dynamodb:DeleteResourcePolicy",
    "dynamodb:DeleteTable",
    "dynamodb:DescribeContinuousBackups",
    "dynamodb:DescribeTable",
    "dynamodb:DescribeTimeToLive",
    "dynamodb:GetResourcePolicy",
    "dynamodb:ListTagsOfResource",
    "dynamodb:PutResourcePolicy",
    "dynamodb:DescribeContributorInsights",
    "dynamodb:TagResource",
    "dynamodb:UntagResource",
    "dynamodb:UpdateContributorInsights",
    "dynamodb:UpdateContinuousBackups",
    "dynamodb:UpdateTable",
    "dynamodb:UpdateTimeToLive",
    "ecr:CreateRepository",
    "ecr:DeleteLifecyclePolicy",
    "ecr:DeleteRepository",
    "ecr:DeleteRepositoryPolicy",
    "ecr:DescribeRepositories",
    "ecr:GetLifecyclePolicy",
    "ecr:GetRepositoryPolicy",
    "ecr:ListTagsForResource",
    "ecr:PutImageScanningConfiguration",
    "ecr:PutImageTagMutability",
    "ecr:PutLifecyclePolicy",
    "ecr:SetRepositoryPolicy",
    "ecr:TagResource",
    "ecr:UntagResource",
    "kms:CancelKeyDeletion",
    "kms:CreateAlias",
    "kms:CreateGrant",
    "kms:CreateKey",
    "kms:DeleteAlias",
    "kms:DescribeKey",
    "kms:DisableKey",
    "kms:EnableKey",
    "kms:EnableKeyRotation",
    "kms:GetKeyPolicy",
    "kms:GetKeyRotationStatus",
    "kms:ListAliases",
    "kms:ListGrants",
    "kms:ListResourceTags",
    "kms:PutKeyPolicy",
    "kms:RetireGrant",
    "kms:RevokeGrant",
    "kms:ScheduleKeyDeletion",
    "kms:TagResource",
    "kms:UntagResource",
    "kms:UpdateAlias",
    "kms:UpdateKeyDescription",
    "lambda:AddPermission",
    "lambda:CreateFunction",
    "lambda:DeleteFunction",
    "lambda:GetFunction",
    "lambda:GetFunctionConfiguration",
    "lambda:GetPolicy",
    "lambda:ListTags",
    "lambda:ListVersionsByFunction",
    "lambda:PublishVersion",
    "lambda:RemovePermission",
    "lambda:TagResource",
    "lambda:UntagResource",
    "lambda:UpdateFunctionCode",
    "lambda:UpdateFunctionConfiguration",
    "logs:AssociateKmsKey",
    "logs:CreateLogGroup",
    "logs:DeleteLogGroup",
    "logs:DeleteRetentionPolicy",
    "logs:DescribeLogGroups",
    "logs:DisassociateKmsKey",
    "logs:ListTagsForResource",
    "logs:PutLogGroupDeletionProtection",
    "logs:PutRetentionPolicy",
    "logs:TagResource",
    "logs:UntagResource",
    "scheduler:CreateSchedule",
    "scheduler:CreateScheduleGroup",
    "scheduler:DeleteSchedule",
    "scheduler:DeleteScheduleGroup",
    "scheduler:GetSchedule",
    "scheduler:GetScheduleGroup",
    "scheduler:ListTagsForResource",
    "scheduler:TagResource",
    "scheduler:UntagResource",
    "scheduler:UpdateSchedule",
    "secretsmanager:CreateSecret",
    "secretsmanager:DeleteResourcePolicy",
    "secretsmanager:DeleteSecret",
    "secretsmanager:DescribeSecret",
    "secretsmanager:GetRandomPassword",
    "secretsmanager:GetResourcePolicy",
    "secretsmanager:GetSecretValue",
    "secretsmanager:PutResourcePolicy",
    "secretsmanager:PutSecretValue",
    "secretsmanager:TagResource",
    "secretsmanager:UntagResource",
    "secretsmanager:UpdateSecret",
    "secretsmanager:UpdateSecretVersionStage",
    "sns:CreateTopic",
    "sns:DeleteTopic",
    "sns:GetSubscriptionAttributes",
    "sns:GetTopicAttributes",
    "sns:ListSubscriptionsByTopic",
    "sns:ListTagsForResource",
    "sns:SetSubscriptionAttributes",
    "sns:SetTopicAttributes",
    "sns:Subscribe",
    "sns:TagResource",
    "sns:Unsubscribe",
    "sns:UntagResource",
    "sqs:CreateQueue",
    "sqs:DeleteQueue",
    "sqs:GetQueueAttributes",
    "sqs:GetQueueUrl",
    "sqs:ListQueueTags",
    "sqs:SetQueueAttributes",
    "sqs:TagQueue",
    "sqs:UntagQueue",
    "states:CreateActivity",
    "states:CreateStateMachine",
    "states:DeleteActivity",
    "states:DeleteStateMachine",
    "states:DeleteStateMachineVersion",
    "states:DescribeActivity",
    "states:DescribeStateMachine",
    "states:ListActivities",
    "states:ListStateMachineVersions",
    "states:ListTagsForResource",
    "states:PublishStateMachineVersion",
    "states:TagResource",
    "states:UntagResource",
    "states:UpdateStateMachine",
    "states:ValidateStateMachineDefinition",
)

_KMS_SERVICE_CRYPTO_ACTIONS = (
    "kms:Decrypt",
    "kms:Encrypt",
    "kms:GenerateDataKey",
    "kms:GenerateDataKeyWithoutPlaintext",
)

_BUCKET_ACTIONS = (
    "s3:CreateBucket",
    "s3:DeleteBucketPolicy",
    "s3:GetBucketAcl",
    "s3:GetBucketLocation",
    "s3:GetBucketObjectLockConfiguration",
    "s3:GetBucketOwnershipControls",
    "s3:GetBucketPolicy",
    "s3:GetBucketPolicyStatus",
    "s3:GetBucketPublicAccessBlock",
    "s3:GetBucketTagging",
    "s3:GetBucketVersioning",
    "s3:GetEncryptionConfiguration",
    "s3:ListBucket",
    "s3:PutBucketObjectLockConfiguration",
    "s3:PutBucketOwnershipControls",
    "s3:PutBucketPolicy",
    "s3:PutBucketPublicAccessBlock",
    "s3:PutBucketTagging",
    "s3:PutBucketVersioning",
    "s3:PutEncryptionConfiguration",
)

_ROLE_MANAGEMENT_ACTIONS = (
    "iam:DeleteRole",
    "iam:DeleteRolePermissionsBoundary",
    "iam:DeleteRolePolicy",
    "iam:GetRole",
    "iam:GetRolePolicy",
    "iam:ListAttachedRolePolicies",
    "iam:ListRolePolicies",
    "iam:ListRoleTags",
    "iam:PutRolePolicy",
    "iam:TagRole",
    "iam:UntagRole",
    "iam:UpdateAssumeRolePolicy",
    "iam:UpdateRole",
    "iam:UpdateRoleDescription",
)

_MANAGED_POLICY_ACTIONS = (
    "iam:CreatePolicyVersion",
    "iam:DeletePolicy",
    "iam:DeletePolicyVersion",
    "iam:GetPolicy",
    "iam:GetPolicyVersion",
    "iam:ListEntitiesForPolicy",
    "iam:ListPolicyTags",
    "iam:ListPolicyVersions",
    "iam:SetDefaultPolicyVersion",
)


def execution_policy_name(
    region: str,
    *,
    part: int,
) -> str:
    """Return one deterministic release-foundation execution policy name."""
    if not isinstance(part, int) or isinstance(part, bool) or not (
        1 <= part <= EXECUTION_POLICY_PART_COUNT
    ):
        raise ValueError(
            "execution policy part must be between 1 and "
            f"{EXECUTION_POLICY_PART_COUNT}"
        )
    return (
        f"{EXECUTION_POLICY_NAME_PREFIX}-{FOUNDATION_QUALIFIER}-"
        f"{region}-part{part}"
    )


def execution_policy_arn(
    *,
    partition: str,
    account_id: str,
    region: str,
    part: int,
) -> str:
    """Return one deterministic release-foundation policy ARN."""
    return (
        f"arn:{partition}:iam::{account_id}:policy/"
        f"{execution_policy_name(region, part=part)}"
    )


def service_boundary_name(region: str) -> str:
    """Return the boundary required on release-foundation service roles."""
    return (
        f"{SERVICE_BOUNDARY_NAME_PREFIX}-{FOUNDATION_QUALIFIER}-{region}"
    )


def service_boundary_arn(
    *,
    partition: str,
    account_id: str,
    region: str,
) -> str:
    """Return the release-foundation service-role boundary ARN."""
    return (
        f"arn:{partition}:iam::{account_id}:policy/"
        f"{service_boundary_name(region)}"
    )


def bootstrap_boundary_name(region: str) -> str:
    """Return the boundary required on release-foundation CDK roles."""
    return (
        f"{BOOTSTRAP_BOUNDARY_NAME_PREFIX}-{FOUNDATION_QUALIFIER}-{region}"
    )


def bootstrap_boundary_arn(
    *,
    partition: str,
    account_id: str,
    region: str,
) -> str:
    """Return the release-foundation CDK-role boundary ARN."""
    return (
        f"arn:{partition}:iam::{account_id}:policy/"
        f"{bootstrap_boundary_name(region)}"
    )


def _role_arns(*, partition: str, account_id: str) -> list[str]:
    return [
        f"arn:{partition}:iam::{account_id}:role/{name}"
        for name in FOUNDATION_ROLE_NAMES
    ]


def _managed_policy_arns(
    *,
    partition: str,
    account_id: str,
) -> list[str]:
    return [
        (
            f"arn:{partition}:iam::{account_id}:policy/"
            "AxonLLMReleaseFoundationStack-*"
        )
    ]


def _regional_infrastructure_statements(
    *,
    partition: str,
    account_id: str,
    region: str,
) -> list[dict[str, Any]]:
    condition = {"StringEquals": {"aws:RequestedRegion": region}}
    actions_by_service: dict[str, list[str]] = {}
    for action in _REGIONAL_ACTIONS:
        service = action.split(":", maxsplit=1)[0]
        actions_by_service.setdefault(service, []).append(action)

    unscoped_actions = {
        "kms:ListAliases",
        "logs:DescribeLogGroups",
        "secretsmanager:GetRandomPassword",
        "states:ListActivities",
        "states:ValidateStateMachineDefinition",
    }
    for actions in actions_by_service.values():
        actions[:] = [
            action for action in actions if action not in unscoped_actions
        ]

    resource_arns = {
        "cloudwatch": [
            (
                f"arn:{partition}:cloudwatch:{region}:{account_id}:alarm:"
                "axonllm-launch-*"
            )
        ],
        "dynamodb": [
            (
                f"arn:{partition}:dynamodb:{region}:{account_id}:table/"
                f"{name}"
            )
            for name in (
                "axonllm-launch-rehearsal-leases",
                "axonllm-qualification-mutation-authorizations",
                "axonllm-rehearsal-control-ledger",
            )
        ],
        "ecr": [
            (
                f"arn:{partition}:ecr:{region}:{account_id}:repository/"
                f"axonllm/{target}"
            )
            for target in ("agentcore", "fargate", "standalone")
        ],
        "lambda": [
            (
                f"arn:{partition}:lambda:{region}:{account_id}:function:"
                f"{name}{suffix}"
            )
            for name in (
                "axonllm-production-transition-mutation-broker",
                "axonllm-qualification-selector-mutation-broker",
            )
            for suffix in ("", ":*")
        ],
        "logs": [
            (
                f"arn:{partition}:logs:{region}:{account_id}:log-group:"
                f"{name}{suffix}"
            )
            for name in (
                "/aws/lambda/axonllm-production-transition-mutation-broker",
                "/aws/lambda/axonllm-qualification-selector-mutation-broker",
                "/aws/vendedlogs/states/AxonLLMLaunchCoordinator",
            )
            for suffix in ("", ":*")
        ],
        "scheduler": [
            (
                f"arn:{partition}:scheduler:{region}:{account_id}:"
                "schedule-group/axonllm-launch-coordinator"
            ),
            (
                f"arn:{partition}:scheduler:{region}:{account_id}:"
                "schedule/axonllm-launch-coordinator/"
                "axonllm-launch-coordinator-*"
            ),
        ],
        "secretsmanager": [
            (
                f"arn:{partition}:secretsmanager:{region}:{account_id}:"
                "secret:axonllm/launch/runtime-identity-*"
            )
        ],
        "sns": [
            (
                f"arn:{partition}:sns:{region}:{account_id}:"
                "axonllm-launch-coordinator-alarms"
            ),
            (
                f"arn:{partition}:sns:{region}:{account_id}:"
                "axonllm-launch-coordinator-alarms:*"
            ),
        ],
        "sqs": [
            (
                f"arn:{partition}:sqs:{region}:{account_id}:"
                f"{name}"
            )
            for name in (
                "axonllm-launch-coordinator-alarm-receipts",
                "axonllm-launch-coordinator-scheduler-dlq",
            )
        ],
        "states": [
            (
                f"arn:{partition}:states:{region}:{account_id}:activity:"
                f"{name}"
            )
            for name in (
                "axonllm-agentcore-launch-actions",
                "axonllm-agentcore-launch-cleanup",
            )
        ]
        + [
            (
                f"arn:{partition}:states:{region}:{account_id}:stateMachine:"
                f"AxonLLMLaunchCoordinator{suffix}"
            )
            for suffix in ("", ":*")
        ],
    }
    statements = [
        {
            "Sid": f"ManageReleaseFoundation{service.title()}",
            "Effect": "Allow",
            "Action": actions_by_service[service],
            "Resource": resources,
            "Condition": condition,
        }
        for service, resources in resource_arns.items()
        if service != "kms" and actions_by_service[service]
    ]

    kms_aliases = [
        (
            f"arn:{partition}:kms:{region}:{account_id}:"
            f"alias/{alias}"
        )
        for alias in (
            "axonllm/agentcore-launch-coordinator",
            "axonllm/agentcore-launch-prerequisite-signing",
            "axonllm/agentcore-production-transition-signing",
            "axonllm/agentcore-production-transition-terminal-signing",
            "axonllm/deployment-evidence",
            "axonllm/release-ecr",
            "axonllm/release-signing",
            "axonllm/release-signing-v*",
        )
    ]
    kms_alias_actions = [
        action
        for action in actions_by_service["kms"]
        if action.endswith("Alias")
    ]
    kms_key_actions = [
        action
        for action in actions_by_service["kms"]
        if action not in {*kms_alias_actions, "kms:CreateKey"}
    ]
    key_resource = (
        f"arn:{partition}:kms:{region}:{account_id}:key/*"
    )
    statements.extend(
        [
            {
                "Sid": "CreateTaggedReleaseFoundationKeys",
                "Effect": "Allow",
                "Action": "kms:CreateKey",
                "Resource": "*",
                "Condition": {
                    "StringEquals": {
                        "aws:RequestedRegion": region,
                        f"aws:RequestTag/{_APPLICATION_TAG}": "AxonLLM",
                        f"aws:RequestTag/{_TRUST_DOMAIN_TAG}": (
                            FOUNDATION_QUALIFIER
                        ),
                    }
                },
            },
            {
                "Sid": "ManageAliasedReleaseFoundationKeys",
                "Effect": "Allow",
                "Action": [*kms_key_actions, *kms_alias_actions],
                "Resource": key_resource,
                "Condition": {
                    "ForAnyValue:StringLike": {
                        "kms:ResourceAliases": [
                            value.rsplit(":", maxsplit=1)[-1]
                            for value in kms_aliases
                        ]
                    },
                    **condition,
                },
            },
            {
                "Sid": "ManageTaggedReleaseFoundationKeys",
                "Effect": "Allow",
                "Action": [*kms_key_actions, *kms_alias_actions],
                "Resource": key_resource,
                "Condition": {
                    "StringEquals": {
                        "aws:RequestedRegion": region,
                        f"aws:ResourceTag/{_APPLICATION_TAG}": "AxonLLM",
                        f"aws:ResourceTag/{_TRUST_DOMAIN_TAG}": (
                            FOUNDATION_QUALIFIER
                        ),
                    }
                },
            },
            {
                "Sid": "ManageExactReleaseFoundationAliases",
                "Effect": "Allow",
                "Action": kms_alias_actions,
                "Resource": kms_aliases,
                "Condition": condition,
            },
            {
                "Sid": "InspectRegionalReleaseFoundationCollections",
                "Effect": "Allow",
                "Action": sorted(unscoped_actions),
                "Resource": "*",
                "Condition": condition,
            },
        ]
    )
    dns_suffix = (
        "amazonaws.com.cn"
        if partition == "aws-cn"
        else "amazonaws.com"
    )
    via_services = [
        f"{service}.{region}.{dns_suffix}"
        for service in (
            "dynamodb",
            "ecr",
            "logs",
            "s3",
            "scheduler",
            "secretsmanager",
            "sns",
            "sqs",
            "states",
        )
    ]
    statements.extend(
        [
            {
                "Sid": "UseAliasedReleaseFoundationKeysViaServices",
                "Effect": "Allow",
                "Action": list(_KMS_SERVICE_CRYPTO_ACTIONS),
                "Resource": key_resource,
                "Condition": {
                    "ForAnyValue:StringLike": {
                        "kms:ResourceAliases": [
                            value.rsplit(":", maxsplit=1)[-1]
                            for value in kms_aliases
                        ]
                    },
                    "StringEquals": {
                        "aws:RequestedRegion": region,
                        "kms:ViaService": via_services,
                    },
                },
            },
            {
                "Sid": "UseTaggedReleaseFoundationKeysViaServices",
                "Effect": "Allow",
                "Action": list(_KMS_SERVICE_CRYPTO_ACTIONS),
                "Resource": key_resource,
                "Condition": {
                    "StringEquals": {
                        "aws:RequestedRegion": region,
                        f"aws:ResourceTag/{_APPLICATION_TAG}": "AxonLLM",
                        f"aws:ResourceTag/{_TRUST_DOMAIN_TAG}": (
                            FOUNDATION_QUALIFIER
                        ),
                        "kms:ViaService": via_services,
                    }
                },
            },
        ]
    )
    return statements


def service_boundary_document(
    *,
    partition: str,
    account_id: str,
    region: str,
) -> dict[str, Any]:
    """Build the anti-escalation boundary for foundation-created roles."""
    assumable_roles = [
        (
            f"arn:{partition}:iam::{account_id}:role/"
            f"cdk-{qualifier}-{purpose}-role-{account_id}-{region}"
        )
        for qualifier in ("axext", "axprod", "axqual", FOUNDATION_QUALIFIER)
        for purpose in (
            "deploy",
            "file-publishing",
            "image-publishing",
            "lookup",
        )
        if not (
            qualifier == FOUNDATION_QUALIFIER
            and purpose in {"image-publishing", "lookup"}
        )
    ]
    passable_roles = [
        (
            f"arn:{partition}:iam::{account_id}:role/"
            f"cdk-{qualifier}-cfn-exec-role-{account_id}-{region}"
        )
        for qualifier in ("axext", "axprod", "axqual")
    ]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowReviewedNonIdentityPermissions",
                "Effect": "Allow",
                "NotAction": [
                    "account:*",
                    "iam:*",
                    "organizations:*",
                    "sso:*",
                    "sts:*",
                ],
                "Resource": "*",
            },
            {
                "Sid": "AllowIdentityMetadataInspection",
                "Effect": "Allow",
                "Action": ["iam:Get*", "iam:List*"],
                "Resource": "*",
            },
            {
                "Sid": "AllowExpectedRoleAssumption",
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Resource": assumable_roles,
            },
            {
                "Sid": "AllowExpectedRolePassing",
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": passable_roles,
            },
            {
                "Sid": "AllowCallerIdentityInspection",
                "Effect": "Allow",
                "Action": "sts:GetCallerIdentity",
                "Resource": "*",
            },
            {
                "Sid": "DenyAccountAdministration",
                "Effect": "Deny",
                "Action": [
                    "account:*",
                    "organizations:*",
                    "sso:*",
                ],
                "Resource": "*",
            },
            {
                "Sid": "DenyIdentityMutation",
                "Effect": "Deny",
                "NotAction": [
                    "iam:Get*",
                    "iam:List*",
                    "iam:PassRole",
                    "sts:AssumeRole",
                ],
                "Resource": f"arn:{partition}:iam::*:*",
            },
            {
                "Sid": "DenyUnexpectedRoleAssumption",
                "Effect": "Deny",
                "Action": "sts:AssumeRole",
                "NotResource": assumable_roles,
            },
            {
                "Sid": "DenyUnexpectedRolePassing",
                "Effect": "Deny",
                "Action": "iam:PassRole",
                "NotResource": passable_roles,
            },
        ],
    }


def bootstrap_boundary_document(
    *,
    partition: str,
    account_id: str,
    region: str,
) -> dict[str, Any]:
    """Build defense-in-depth limits for the dedicated CDK roles."""
    role_arns = _role_arns(
        partition=partition,
        account_id=account_id,
    )
    managed_policy_arns = _managed_policy_arns(
        partition=partition,
        account_id=account_id,
    )
    oidc_provider = (
        f"arn:{partition}:iam::{account_id}:oidc-provider/"
        "token.actions.githubusercontent.com"
    )
    evidence_objects = (
        f"arn:{partition}:s3:::"
        f"axonllm-deployment-evidence-{account_id}-{region}/*"
    )
    asset_bucket = (
        f"arn:{partition}:s3:::"
        f"cdk-{FOUNDATION_QUALIFIER}-assets-{account_id}-{region}"
    )
    runtime_identity_secret = (
        f"arn:{partition}:secretsmanager:{region}:{account_id}:"
        "secret:axonllm/launch/runtime-identity-*"
    )
    cloudformation_role = (
        f"arn:{partition}:iam::{account_id}:role/"
        f"cdk-{FOUNDATION_QUALIFIER}-cfn-exec-role-"
        f"{account_id}-{region}"
    )
    allowed_stacks = [
        (
            f"arn:{partition}:cloudformation:{region}:{account_id}:"
            "stack/AxonLLMReleaseFoundationStack/*"
        )
    ]
    allowed_change_sets = [
        (
            f"arn:{partition}:cloudformation:{region}:{account_id}:"
            "changeSet/AxonLLMReleaseFoundation-*/*"
        )
    ]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowReviewedNonIdentityPermissions",
                "Effect": "Allow",
                "NotAction": [
                    "account:*",
                    "iam:*",
                    "organizations:*",
                    "sso:*",
                    "sts:*",
                ],
                "Resource": "*",
            },
            {
                "Sid": "AllowIdentityMetadataInspection",
                "Effect": "Allow",
                "Action": ["iam:Get*", "iam:List*"],
                "Resource": "*",
            },
            {
                "Sid": "AllowExpectedRoleManagement",
                "Effect": "Allow",
                "Action": [
                    "iam:AttachRolePolicy",
                    "iam:CreateRole",
                    "iam:DeleteRole",
                    "iam:DeleteRolePermissionsBoundary",
                    "iam:DeleteRolePolicy",
                    "iam:DetachRolePolicy",
                    "iam:PutRolePermissionsBoundary",
                    "iam:PutRolePolicy",
                    "iam:TagRole",
                    "iam:UntagRole",
                    "iam:UpdateAssumeRolePolicy",
                    "iam:UpdateRole",
                    "iam:UpdateRoleDescription",
                ],
                "Resource": role_arns,
            },
            {
                "Sid": "AllowExpectedManagedPolicyManagement",
                "Effect": "Allow",
                "Action": [
                    "iam:CreatePolicy",
                    "iam:CreatePolicyVersion",
                    "iam:DeletePolicy",
                    "iam:DeletePolicyVersion",
                    "iam:SetDefaultPolicyVersion",
                    "iam:TagPolicy",
                    "iam:UntagPolicy",
                ],
                "Resource": managed_policy_arns,
            },
            {
                "Sid": "AllowExactOidcManagement",
                "Effect": "Allow",
                "Action": [
                    "iam:AddClientIDToOpenIDConnectProvider",
                    "iam:CreateOpenIDConnectProvider",
                    "iam:DeleteOpenIDConnectProvider",
                    "iam:RemoveClientIDFromOpenIDConnectProvider",
                    "iam:TagOpenIDConnectProvider",
                    "iam:UntagOpenIDConnectProvider",
                    "iam:UpdateOpenIDConnectProviderThumbprint",
                ],
                "Resource": oidc_provider,
            },
            {
                "Sid": "AllowExpectedRolePassing",
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": [*role_arns, cloudformation_role],
            },
            {
                "Sid": "AllowCallerIdentityInspection",
                "Effect": "Allow",
                "Action": "sts:GetCallerIdentity",
                "Resource": "*",
            },
            {
                "Sid": "DenyHumanIdentityAdministration",
                "Effect": "Deny",
                "Action": [
                    "account:*",
                    "iam:AddUserToGroup",
                    "iam:AttachGroupPolicy",
                    "iam:AttachUserPolicy",
                    "iam:CreateAccessKey",
                    "iam:CreateGroup",
                    "iam:CreateLoginProfile",
                    "iam:CreateUser",
                    "iam:DeleteAccessKey",
                    "iam:DeleteGroup",
                    "iam:DeleteLoginProfile",
                    "iam:DeleteUser",
                    "iam:PutGroupPolicy",
                    "iam:PutUserPolicy",
                    "organizations:*",
                    "sso:*",
                ],
                "Resource": "*",
            },
            # IAM and STS are excluded from the baseline allow. Identity
            # operations not explicitly scoped above remain implicitly denied;
            # duplicating every ARN in deny statements exceeds IAM's boundary
            # size quota without changing effective access.
            {
                "Sid": "DenyUnexpectedCloudFormationMutation",
                "Effect": "Deny",
                "Action": [
                    "cloudformation:ContinueUpdateRollback",
                    "cloudformation:CreateChangeSet",
                    "cloudformation:CreateStack",
                    "cloudformation:CreateStackRefactor",
                    "cloudformation:DeleteChangeSet",
                    "cloudformation:DeleteStack",
                    "cloudformation:ExecuteChangeSet",
                    "cloudformation:ExecuteStackRefactor",
                    "cloudformation:RollbackStack",
                    "cloudformation:UpdateStack",
                    "cloudformation:UpdateTerminationProtection",
                ],
                "NotResource": [*allowed_stacks, *allowed_change_sets],
            },
            {
                "Sid": "DenyUnexpectedObjectAccess",
                "Effect": "Deny",
                "Action": [
                    "s3:AbortMultipartUpload",
                    "s3:DeleteObject*",
                    "s3:GetObject*",
                    "s3:PutObject*",
                ],
                "NotResource": f"{asset_bucket}/*",
            },
            {
                "Sid": "DenyCredentialAndSigningAccess",
                "Effect": "Deny",
                "Action": [
                    "kms:GenerateDataKeyPair",
                    "kms:Sign",
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:PutSecretValue",
                ],
                "NotResource": runtime_identity_secret,
            },
            {
                "Sid": "DenyEvidenceObjectAccess",
                "Effect": "Deny",
                "Action": "s3:*",
                "Resource": evidence_objects,
            },
            {
                "Sid": "DenyCrossRegionUse",
                "Effect": "Deny",
                "Action": "*",
                "Resource": "*",
                "Condition": {
                    "Null": {"aws:RequestedRegion": "false"},
                    "StringNotEquals": {"aws:RequestedRegion": region},
                },
            },
        ],
    }


def execution_policy_document(
    *,
    partition: str,
    account_id: str,
    region: str,
) -> dict[str, Any]:
    """Build the complete bounded CloudFormation execution policy."""
    role_arns = _role_arns(
        partition=partition,
        account_id=account_id,
    )
    managed_policy_arns = _managed_policy_arns(
        partition=partition,
        account_id=account_id,
    )
    boundary = service_boundary_arn(
        partition=partition,
        account_id=account_id,
        region=region,
    )
    evidence_bucket = (
        f"arn:{partition}:s3:::"
        f"axonllm-deployment-evidence-{account_id}-{region}"
    )
    oidc_provider = (
        f"arn:{partition}:iam::{account_id}:oidc-provider/"
        "token.actions.githubusercontent.com"
    )
    asset_bucket = (
        f"arn:{partition}:s3:::"
        f"cdk-{FOUNDATION_QUALIFIER}-assets-{account_id}-{region}"
    )
    bootstrap_parameter = (
        f"arn:{partition}:ssm:{region}:{account_id}:"
        f"parameter/cdk-bootstrap/{FOUNDATION_QUALIFIER}/version"
    )
    return {
        "Version": "2012-10-17",
        "Statement": [
            *_regional_infrastructure_statements(
                partition=partition,
                account_id=account_id,
                region=region,
            ),
            {
                "Sid": "ManageImmutableEvidenceBucketConfiguration",
                "Effect": "Allow",
                "Action": list(_BUCKET_ACTIONS),
                "Resource": evidence_bucket,
            },
            {
                "Sid": "ReadDedicatedCdkTemplateAssets",
                "Effect": "Allow",
                "Action": [
                    "s3:GetBucketLocation",
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:ListBucket",
                ],
                "Resource": [asset_bucket, f"{asset_bucket}/*"],
            },
            {
                "Sid": "ReadDedicatedCdkBootstrapVersion",
                "Effect": "Allow",
                "Action": [
                    "ssm:GetParameter",
                    "ssm:GetParameters",
                ],
                "Resource": bootstrap_parameter,
            },
            {
                "Sid": "CreateBoundedFoundationRoles",
                "Effect": "Allow",
                "Action": "iam:CreateRole",
                "Resource": role_arns,
                "Condition": {
                    "StringEquals": {
                        "iam:PermissionsBoundary": boundary,
                        f"aws:RequestTag/{_APPLICATION_TAG}": "AxonLLM",
                        f"aws:RequestTag/{_TRUST_DOMAIN_TAG}": (
                            FOUNDATION_QUALIFIER
                        ),
                    }
                },
            },
            {
                "Sid": "TagBoundedFoundationRoles",
                "Effect": "Allow",
                "Action": "iam:TagRole",
                "Resource": role_arns,
                "Condition": {
                    "StringEquals": {
                        f"aws:RequestTag/{_APPLICATION_TAG}": "AxonLLM",
                        f"aws:RequestTag/{_TRUST_DOMAIN_TAG}": (
                            FOUNDATION_QUALIFIER
                        ),
                    }
                },
            },
            {
                "Sid": "SetFoundationRoleBoundary",
                "Effect": "Allow",
                "Action": "iam:PutRolePermissionsBoundary",
                "Resource": role_arns,
                "Condition": {
                    "StringEquals": {
                        "iam:PermissionsBoundary": boundary,
                    }
                },
            },
            {
                "Sid": "ManageBoundedFoundationRoles",
                "Effect": "Allow",
                "Action": list(_ROLE_MANAGEMENT_ACTIONS),
                "Resource": role_arns,
            },
            {
                "Sid": "CreateFoundationOverflowPolicies",
                "Effect": "Allow",
                "Action": "iam:CreatePolicy",
                "Resource": managed_policy_arns,
            },
            {
                "Sid": "ManageFoundationOverflowPolicies",
                "Effect": "Allow",
                "Action": list(_MANAGED_POLICY_ACTIONS),
                "Resource": managed_policy_arns,
            },
            {
                "Sid": "AttachFoundationOverflowPolicies",
                "Effect": "Allow",
                "Action": [
                    "iam:AttachRolePolicy",
                    "iam:DetachRolePolicy",
                ],
                "Resource": role_arns,
                "Condition": {
                    "ArnLike": {
                        "iam:PolicyARN": managed_policy_arns,
                    }
                },
            },
            {
                "Sid": "PassFoundationServiceRoles",
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": role_arns,
                "Condition": {
                    "StringEquals": {
                        "iam:PassedToService": [
                            "lambda.amazonaws.com",
                            "scheduler.amazonaws.com",
                            "states.amazonaws.com",
                        ]
                    }
                },
            },
            {
                "Sid": "ManageExactGitHubOidcProvider",
                "Effect": "Allow",
                "Action": [
                    "iam:AddClientIDToOpenIDConnectProvider",
                    "iam:CreateOpenIDConnectProvider",
                    "iam:DeleteOpenIDConnectProvider",
                    "iam:GetOpenIDConnectProvider",
                    "iam:ListOpenIDConnectProviderTags",
                    "iam:RemoveClientIDFromOpenIDConnectProvider",
                    "iam:TagOpenIDConnectProvider",
                    "iam:UntagOpenIDConnectProvider",
                    "iam:UpdateOpenIDConnectProviderThumbprint",
                ],
                "Resource": oidc_provider,
            },
            {
                "Sid": "DenyUnexpectedIdentityCreation",
                "Effect": "Deny",
                "Action": [
                    "iam:CreatePolicy",
                    "iam:CreateRole",
                    "iam:PutRolePermissionsBoundary",
                    "iam:PutRolePolicy",
                ],
                "NotResource": [*role_arns, *managed_policy_arns],
            },
            {
                "Sid": "DenyHumanIdentityAdministration",
                "Effect": "Deny",
                "Action": [
                    "iam:AddUserToGroup",
                    "iam:AttachGroupPolicy",
                    "iam:AttachUserPolicy",
                    "iam:CreateAccessKey",
                    "iam:CreateGroup",
                    "iam:CreateLoginProfile",
                    "iam:CreateUser",
                    "iam:PutGroupPolicy",
                    "iam:PutUserPolicy",
                ],
                "Resource": "*",
            },
        ],
    }


def _policy_document_size(statements: list[dict[str, Any]]) -> int:
    return len(
        json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": statements,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _split_oversized_statement(
    statement: dict[str, Any],
) -> list[dict[str, Any]]:
    if _policy_document_size([statement]) <= _EXECUTION_POLICY_TARGET_SIZE:
        return [statement]
    actions = statement.get("Action")
    sid = statement.get("Sid")
    if not isinstance(actions, list) or not actions or not isinstance(sid, str):
        raise ValueError(
            "an oversized foundation-policy statement cannot be partitioned"
        )

    chunks: list[list[str]] = []
    current: list[str] = []
    for action in actions:
        candidate = {
            **statement,
            "Sid": f"{sid}Part{len(chunks) + 1}",
            "Action": [*current, action],
        }
        if (
            current
            and _policy_document_size([candidate])
            > _EXECUTION_POLICY_TARGET_SIZE
        ):
            chunks.append(current)
            current = [action]
        else:
            current.append(action)
    chunks.append(current)
    return [
        {
            **statement,
            "Sid": f"{sid}Part{index}",
            "Action": chunk,
        }
        for index, chunk in enumerate(chunks, start=1)
    ]


def execution_policy_documents(
    *,
    partition: str,
    account_id: str,
    region: str,
) -> tuple[dict[str, Any], ...]:
    """Partition the execution policy into fixed IAM-sized documents."""
    complete = execution_policy_document(
        partition=partition,
        account_id=account_id,
        region=region,
    )
    statements: list[dict[str, Any]] = []
    for statement in complete["Statement"]:
        statements.extend(_split_oversized_statement(statement))

    documents: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for statement in statements:
        if (
            current
            and _policy_document_size([*current, statement])
            > _EXECUTION_POLICY_TARGET_SIZE
        ):
            documents.append(
                {
                    "Version": "2012-10-17",
                    "Statement": current,
                }
            )
            current = [statement]
        else:
            current.append(statement)
    if current:
        documents.append(
            {
                "Version": "2012-10-17",
                "Statement": current,
            }
        )

    if len(documents) != EXECUTION_POLICY_PART_COUNT or any(
        _policy_document_size(document["Statement"])
        > IAM_MANAGED_POLICY_SIZE_LIMIT
        for document in documents
    ):
        raise ValueError(
            "release-foundation execution policy no longer fits its reviewed "
            f"{EXECUTION_POLICY_PART_COUNT}-part IAM contract"
        )
    return tuple(documents)
