"""Production ECS control plane sharing AgentCore's canonical authority."""

import re

from aws_cdk import (
    ArnFormat,
    CfnCondition,
    CfnOutput,
    CfnParameter,
    CfnResource,
    CfnRule,
    CfnRuleAssertion,
    CustomResource,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    Tags,
    Token,
    custom_resources as cr,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_route53 as route53,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
    aws_sns as sns,
    aws_sqs as sqs,
    aws_wafv2 as wafv2,
)
from constructs import Construct

if __package__:
    from .agentcore_stack import load_athena_infrastructure_config
    from .application_state import (
        application_state_mode,
        external_application_state_access,
    )
else:
    from agentcore_stack import load_athena_infrastructure_config
    from application_state import (
        application_state_mode,
        external_application_state_access,
    )


_DYNAMODB_STANDARD_ACTIONS = [
    "dynamodb:BatchGetItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:ConditionCheckItem",
    "dynamodb:DeleteItem",
    "dynamodb:DescribeTable",
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:UpdateItem",
]
_DYNAMODB_TRANSACTION_ACTIONS = ["dynamodb:TransactWriteItems"]
_DYNAMODB_ACTIONS = [
    *_DYNAMODB_STANDARD_ACTIONS,
    *_DYNAMODB_TRANSACTION_ACTIONS,
]
_SQS_ACTIONS = [
    "sqs:ChangeMessageVisibility",
    "sqs:DeleteMessage",
    "sqs:GetQueueAttributes",
    "sqs:ReceiveMessage",
    "sqs:SendMessage",
]
_ECR_PULL_ACTIONS = [
    "ecr:BatchCheckLayerAvailability",
    "ecr:BatchGetImage",
    "ecr:GetDownloadUrlForLayer",
]
_LAUNCH_ACTION_ROLE_NAME = "AxonLLMLaunchActionWorkerRole"
_LAUNCH_CLEANUP_ROLE_NAME = "AxonLLMLaunchCleanupWorkerRole"
_LAUNCH_EXECUTION_ROLE_NAME = "AxonLLMLaunchWorkerExecutionRole"
_LAUNCH_WORKER_DYNAMODB_ACTIONS = [
    "dynamodb:BatchWriteItem",
    "dynamodb:DeleteItem",
    "dynamodb:DeleteTable",
    "dynamodb:DescribeContinuousBackups",
    "dynamodb:DescribeTable",
    "dynamodb:DescribeTimeToLive",
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:Query",
    "dynamodb:RestoreTableToPointInTime",
    "dynamodb:Scan",
    "dynamodb:UpdateContinuousBackups",
    "dynamodb:UpdateItem",
    "dynamodb:UpdateTable",
    "dynamodb:UpdateTimeToLive",
]
_LAUNCH_WORKER_SQS_ACTIONS = [
    "sqs:ChangeMessageVisibility",
    "sqs:DeleteMessage",
    "sqs:GetQueueAttributes",
    "sqs:ReceiveMessage",
    "sqs:SendMessage",
]


_CONTROL_PLANE_RECOVERY_GUARD = """\
import boto3


cloudformation = boto3.client("cloudformation")
ecs = boto3.client("ecs")
autoscaling = boto3.client("application-autoscaling")

_PHYSICAL_ID = "AxonLLMControlPlaneRecoveryGuard"
_BLOCKED_MODES = {"quiesced", "selected"}
_SUSPENSION_KEYS = (
    "DynamicScalingInSuspended",
    "DynamicScalingOutSuspended",
    "ScheduledScalingSuspended",
)


def _stack_outputs(stack_name):
    response = cloudformation.describe_stacks(StackName=stack_name)
    stacks = response.get("Stacks", [])
    if len(stacks) != 1:
        raise RuntimeError(
            f"recovery guard could not resolve stack {stack_name}"
        )
    stack = stacks[0]
    outputs = {
        item.get("OutputKey"): item.get("OutputValue")
        for item in stack.get("Outputs", [])
    }
    return stack, outputs


def _assert_table_namespace(primary, selected):
    if selected == primary:
        return
    if not selected.startswith(f"{primary}-restore-validation-"):
        raise RuntimeError(
            "control-plane recovery table is outside the AgentCore "
            "restore-validation namespace"
        )


def _agentcore_state(properties):
    stack, outputs = _stack_outputs(properties["AgentCoreStackName"])
    required = {
        "RecoveryApprovalId",
        "RecoveryCutoverMode",
        "SelectedRuntimeStateTableName",
        "StateTableName",
    }
    missing = sorted(required.difference(outputs))
    if missing:
        raise RuntimeError(
            "AgentCore stack is missing recovery outputs: "
            + ", ".join(missing)
        )
    if outputs["StateTableName"] != properties["PrimaryTable"]:
        raise RuntimeError(
            "control-plane primary table is not owned by the selected "
            "AgentCore stack"
        )
    return stack, outputs


def _assert_agentcore(outputs, *, mode, selected, approval):
    actual = (
        outputs["RecoveryCutoverMode"],
        outputs["SelectedRuntimeStateTableName"],
        outputs["RecoveryApprovalId"],
    )
    expected = (mode, selected, approval)
    if actual != expected:
        raise RuntimeError(
            "AgentCore recovery state does not authorize this control-plane "
            f"transition: expected {expected}, found {actual}"
        )


def _assert_control_plane_quiesced(stack_name):
    _, outputs = _stack_outputs(stack_name)
    cluster_name = outputs.get("ClusterName")
    service_name = outputs.get("ServiceName")
    if not cluster_name or not service_name:
        raise RuntimeError(
            "control-plane stack is missing ClusterName or ServiceName"
        )
    resource_id = f"service/{cluster_name}/{service_name}"
    targets = autoscaling.describe_scalable_targets(
        ServiceNamespace="ecs",
        ResourceIds=[resource_id],
        ScalableDimension="ecs:service:DesiredCount",
    ).get("ScalableTargets", [])
    if len(targets) != 1:
        raise RuntimeError(
            "recovery requires exactly one control-plane scalable target"
        )
    target = targets[0]
    suspended = target.get("SuspendedState", {})
    if target.get("MinCapacity") != 0 or not all(
        suspended.get(key) is True for key in _SUSPENSION_KEYS
    ):
        raise RuntimeError(
            "recovery requires the control plane at minimum capacity zero "
            "with every scaling path suspended"
        )
    response = ecs.describe_services(
        cluster=cluster_name,
        services=[service_name],
    )
    if response.get("failures") or len(response.get("services", [])) != 1:
        raise RuntimeError(
            "recovery guard could not resolve the control-plane service"
        )
    service = response["services"][0]
    counts = {
        name: service.get(name)
        for name in ("desiredCount", "pendingCount", "runningCount")
    }
    if any(value != 0 for value in counts.values()):
        raise RuntimeError(
            "recovery requires a fully quiesced control plane: "
            f"{counts}"
        )


def _result():
    return {"PhysicalResourceId": _PHYSICAL_ID}


def handler(event, _context):
    if event["RequestType"] == "Delete":
        return _result()

    current = event["ResourceProperties"]
    mode = current.get("Mode")
    selected = current.get("SelectedTable", "")
    primary = current.get("PrimaryTable", "")
    approval = current.get("ApprovalId", "")
    if not primary or not selected:
        raise RuntimeError("control-plane recovery table ownership is missing")
    _assert_table_namespace(primary, selected)
    _, agentcore = _agentcore_state(current)

    if event["RequestType"] == "Create":
        if mode != "normal":
            raise RuntimeError(
                "a new control-plane stack must start in normal mode"
            )
        _assert_agentcore(
            agentcore,
            mode="normal",
            selected=selected,
            approval=approval,
        )
        return _result()

    previous = event.get("OldResourceProperties", {})
    for immutable in (
        "AgentCoreStackName",
        "ControlPlaneStackName",
        "PrimaryTable",
    ):
        if current.get(immutable) != previous.get(immutable):
            raise RuntimeError(
                f"control-plane recovery ownership changed: {immutable}"
            )

    old_mode = previous.get("Mode")
    old_selected = previous.get("SelectedTable", "")
    old_approval = previous.get("ApprovalId", "")
    transition = (old_mode, mode)
    allowed = {
        ("normal", "normal"),
        ("normal", "quiesced"),
        ("quiesced", "quiesced"),
        ("quiesced", "normal"),
        ("quiesced", "selected"),
        ("selected", "selected"),
        ("selected", "quiesced"),
        ("selected", "normal"),
    }
    if transition not in allowed:
        raise RuntimeError(
            "unsupported control-plane recovery transition: "
            f"{old_mode} -> {mode}"
        )

    table_changed = selected != old_selected
    if table_changed and transition not in {
        ("quiesced", "selected"),
        ("selected", "quiesced"),
    }:
        raise RuntimeError(
            "control-plane table changes require a blocked "
            "quiesced <-> selected transition"
        )
    if transition in {
        ("quiesced", "selected"),
        ("selected", "quiesced"),
    } and not table_changed:
        raise RuntimeError(
            "control-plane selection transition requires a table change"
        )

    if transition == ("normal", "quiesced"):
        if not approval or approval == old_approval:
            raise RuntimeError(
                "entering control-plane recovery requires a new approval ID"
            )
    elif mode in _BLOCKED_MODES and approval != old_approval:
        raise RuntimeError(
            "control-plane recovery approval changed during a blocked phase"
        )
    elif transition == ("selected", "normal") and approval != old_approval:
        raise RuntimeError(
            "control-plane promotion changed its recovery approval"
        )

    if mode in _BLOCKED_MODES or old_mode in _BLOCKED_MODES:
        _assert_control_plane_quiesced(current["ControlPlaneStackName"])

    if transition == ("normal", "quiesced"):
        _assert_agentcore(
            agentcore,
            mode="normal",
            selected=old_selected,
            approval=old_approval,
        )
    elif transition == ("quiesced", "selected"):
        _assert_agentcore(
            agentcore,
            mode="quiesced",
            selected=old_selected,
            approval=approval,
        )
    elif transition == ("selected", "quiesced"):
        _assert_agentcore(
            agentcore,
            mode="quiesced",
            selected=selected,
            approval=approval,
        )
    elif transition == ("quiesced", "normal"):
        _assert_agentcore(
            agentcore,
            mode="normal",
            selected=selected,
            approval=approval,
        )
    elif transition == ("selected", "normal"):
        _assert_agentcore(
            agentcore,
            mode="normal",
            selected=selected,
            approval=approval,
        )
    elif mode == "normal":
        _assert_agentcore(
            agentcore,
            mode="normal",
            selected=selected,
            approval=approval,
        )
    elif mode == "quiesced":
        _assert_agentcore(
            agentcore,
            mode="quiesced",
            selected=selected,
            approval=approval,
        )
    else:
        _assert_agentcore(
            agentcore,
            mode="selected",
            selected=selected,
            approval=approval,
        )
    return _result()
"""


class AxonLLMControlPlaneStack(Stack):
    """Private Fargate control plane backed by AgentCore-owned state."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        deployment_namespace: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        physical_suffix = f"-{deployment_namespace}" if deployment_namespace else ""
        removal_policy = RemovalPolicy.DESTROY if deployment_namespace else RemovalPolicy.RETAIN
        deletion_protection = not bool(deployment_namespace)
        edge_cutover_enabled = self.node.try_get_context(
            "edge_cutover_enabled"
        )
        if edge_cutover_enabled is None:
            edge_cutover_enabled = False
        if not isinstance(edge_cutover_enabled, bool):
            raise ValueError("edge_cutover_enabled must be a boolean")
        agentcore_stack_default = f"AxonLLMAgentCoreStack{physical_suffix}"
        state_stack_default = (
            f"AxonLLMApplicationStateStack{physical_suffix}"
        )
        identity_stack_default = f"AxonLLMIdentityStack{physical_suffix}"
        state_mode = application_state_mode(self)
        query_config = load_athena_infrastructure_config(self)
        rehearsal_control_table_arn = (
            CfnParameter(
                self,
                "RehearsalControlTableArn",
                type="String",
                allowed_pattern=(
                    rf"^arn:aws:dynamodb:{re.escape(self.region)}:"
                    rf"{('[0-9]{12}' if Token.is_unresolved(self.account) else re.escape(self.account))}:"
                    r"table/axonllm-rehearsal-control-ledger$"
                ),
                constraint_description=(
                    "must be the retained rehearsal-control ledger ARN in this stack's AWS region and account"
                ),
                description=(
                    "Exact retained rehearsal-control ledger ARN used only by an isolated qualification control plane"
                ),
            )
            if deployment_namespace
            else None
        )
        secret_arn_pattern = re.compile(
            rf"^arn:(?:aws|aws-us-gov|aws-cn):secretsmanager:"
            rf"{re.escape(self.region)}:[0-9]{{12}}:"
            r"secret:[A-Za-z0-9/_+=.@-]{1,512}$"
        )

        def secret_context(name: str) -> str | None:
            value = self.node.try_get_context(name)
            if value in (None, ""):
                return None
            if not isinstance(value, str) or secret_arn_pattern.fullmatch(value) is None:
                raise ValueError(f"{name} must be a complete Secrets Manager ARN in {self.region}")
            return value

        scim_tenants_secret_arn = secret_context("scim_tenants_secret_arn")

        endpoint_mode = CfnParameter(
            self,
            "EndpointMode",
            type="String",
            default="custom-domain",
            allowed_values=["custom-domain", "cloudfront"],
            description=(
                "Control-plane endpoint architecture. Existing deployments "
                "default to custom-domain."
            ),
        )
        custom_domain_mode = CfnCondition(
            self,
            "CustomDomainEndpoint",
            expression=Fn.condition_equals(
                endpoint_mode.value_as_string,
                "custom-domain",
            ),
        )
        cloudfront_mode = CfnCondition(
            self,
            "CloudFrontEndpoint",
            expression=Fn.condition_equals(
                endpoint_mode.value_as_string,
                "cloudfront",
            ),
        )
        edge_backend_mode = None
        edge_migration_id = None
        serverless_api_domain_name = None
        serverless_api_origin_path = None
        serverless_origin_credential_secret_arn = None
        serverless_static_bucket_domain_name = None
        serverless_source_revision = None
        serverless_control_api_sha256 = None
        serverless_static_assets_sha256 = None
        if edge_cutover_enabled:
            edge_backend_mode = CfnParameter(
                self,
                "EdgeBackendMode",
                type="String",
                default="fargate",
                allowed_values=["fargate", "serverless"],
                description=(
                    "Reviewed control-plane backend selected by the existing "
                    "CloudFront distribution"
                ),
            )
            edge_migration_id = CfnParameter(
                self,
                "EdgeMigrationId",
                type="String",
                allowed_pattern=r"^[0-9a-f]{64}$",
                constraint_description=(
                    "must be the reviewed 64-character edge migration ID"
                ),
                description=(
                    "Content-addressed qualification and rollback plan that "
                    "owns this edge selector"
                ),
            )
            serverless_api_domain_name = CfnParameter(
                self,
                "ServerlessControlApiDomainName",
                type="String",
                allowed_pattern=(
                    r"^[a-z0-9]+\.execute-api\.[a-z0-9-]+\."
                    r"(?:amazonaws\.com|amazonaws\.com\.cn)$"
                ),
                constraint_description=(
                    "must be the exact Regional API Gateway hostname"
                ),
                description=(
                    "Qualified serverless control API origin hostname"
                ),
            )
            serverless_api_origin_path = CfnParameter(
                self,
                "ServerlessControlApiOriginPath",
                type="String",
                allowed_pattern=r"^/[A-Za-z0-9_-]{1,128}$",
                constraint_description=(
                    "must be one API Gateway stage path"
                ),
                description=(
                    "Qualified serverless control API stage path"
                ),
            )
            serverless_origin_credential_secret_arn = CfnParameter(
                self,
                "ServerlessOriginCredentialSecretArn",
                type="String",
                allowed_pattern=(
                    rf"^arn:(?:aws|aws-us-gov|aws-cn):secretsmanager:"
                    rf"{re.escape(self.region)}:"
                    rf"{('[0-9]{12}' if Token.is_unresolved(self.account) else re.escape(self.account))}:"
                    r"secret:[A-Za-z0-9/_+=.@-]{1,512}$"
                ),
                constraint_description=(
                    "must be the exact serverless origin-credential secret ARN"
                ),
                description=(
                    "Secret containing the CloudFront-only API Gateway "
                    "origin credential"
                ),
            )
            serverless_static_bucket_domain_name = CfnParameter(
                self,
                "ServerlessStaticBucketRegionalDomainName",
                type="String",
                allowed_pattern=(
                    r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\."
                    r"s3\.[a-z0-9-]+\."
                    r"(?:amazonaws\.com|amazonaws\.com\.cn)$"
                ),
                constraint_description=(
                    "must be the exact private static bucket regional hostname"
                ),
                description=(
                    "Qualified serverless static-site S3 origin hostname"
                ),
            )
            serverless_source_revision = CfnParameter(
                self,
                "ServerlessSourceRevision",
                type="String",
                allowed_pattern=r"^[0-9a-f]{40}$",
                description=(
                    "Reviewed source commit shared by the serverless API and "
                    "static assets"
                ),
            )
            serverless_control_api_sha256 = CfnParameter(
                self,
                "ServerlessControlApiSha256",
                type="String",
                allowed_pattern=r"^[0-9a-f]{64}$",
                description="Verified serverless control API ZIP SHA-256",
            )
            serverless_static_assets_sha256 = CfnParameter(
                self,
                "ServerlessStaticAssetsSha256",
                type="String",
                allowed_pattern=r"^[0-9a-f]{64}$",
                description="Verified serverless static-site ZIP SHA-256",
            )
            CfnRule(
                self,
                "EdgeCutoverRequiresCloudFrontEndpoint",
                assertions=[
                    CfnRuleAssertion(
                        assert_=Fn.condition_equals(
                            endpoint_mode.value_as_string,
                            "cloudfront",
                        ),
                        assert_description=(
                            "edge cutover requires EndpointMode=cloudfront"
                        ),
                    )
                ],
            )
        agentcore_stack_name = CfnParameter(
            self,
            "AgentCoreStackName",
            type="String",
            default=agentcore_stack_default,
            min_length=1,
            max_length=128,
            allowed_pattern=r"^[A-Za-z][A-Za-z0-9-]*$",
            description=("Deployed AgentCore stack exporting canonical state and audit resources"),
        )
        identity_stack_name = CfnParameter(
            self,
            "IdentityStackName",
            type="String",
            default=identity_stack_default,
            min_length=1,
            max_length=128,
            allowed_pattern=r"^[A-Za-z][A-Za-z0-9-]*$",
            description=("Deployed AxonLLM identity stack exporting the ALB client"),
        )
        certificate_arn = CfnParameter(
            self,
            "CertificateArn",
            type="String",
            default="",
            allowed_pattern=(
                r"^$|^arn:(?:aws|aws-us-gov|aws-cn):acm:[a-z0-9-]+:"
                r"[0-9]{12}:certificate/[0-9a-fA-F-]+$"
            ),
            description=("Regional ACM certificate ARN for the control-plane HTTPS listener"),
        )
        control_plane_domain_input = CfnParameter(
            self,
            "ControlPlaneDomainInput",
            type="String",
            default="",
            max_length=253,
            allowed_pattern=(
                r"^(?:|(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}"
                r"[a-z0-9])?\.)+[a-z]{2,63})$"
            ),
            constraint_description=(
                "must be empty or a lowercase fully qualified DNS hostname"
            ),
            description="Stable custom-domain control-plane hostname",
        )
        control_plane_domain_input.override_logical_id(
            "ControlPlaneDomainName"
        )
        approved_ingress_prefix_list_id = CfnParameter(
            self,
            "ApprovedIngressPrefixListId",
            type="String",
            default="",
            allowed_pattern=r"^$|^pl-[0-9a-fA-F]+$",
            constraint_description="must be an EC2 managed prefix list ID",
            description=("Managed prefix list containing approved control-plane clients"),
        )
        approved_https_prefix_list_id = CfnParameter(
            self,
            "ApprovedHttpsPrefixListId",
            type="String",
            allowed_pattern=r"^pl-[0-9a-fA-F]+$",
            constraint_description="must be an EC2 managed prefix list ID",
            description=("Managed prefix list containing Cognito, ALB key, and other approved HTTPS destinations"),
        )
        public_hosted_zone_id = CfnParameter(
            self,
            "PublicHostedZoneId",
            type="String",
            default="",
            allowed_pattern=r"^$|^Z[A-Z0-9]+$",
            constraint_description=("must be a Route 53 public hosted-zone ID"),
            description=("Public hosted-zone ID containing the identity stack's control-plane hostname"),
        )
        allowed_viewer_cidrs = CfnParameter(
            self,
            "AllowedViewerCidrs",
            type="CommaDelimitedList",
            default="192.0.2.0/32",
            description=(
                "Reviewed public viewer CIDRs allowed by the CloudFront WAF. "
                "The documentation-only default permits no production viewer."
            ),
        )
        saml_login_path = CfnParameter(
            self,
            "SamlLoginPath",
            type="String",
            default="/admin/dashboard",
            min_length=2,
            max_length=2048,
            allowed_pattern=(
                r"^/(?!/)(?!$)(?!.*//)(?!.*[/]\.{1,2}(?:/|$))"
                r"(?!(?:[Ss][Aa][Mm][Ll]|[Ss][Cc][Ii][Mm]|"
                r"[Oo][Aa][Uu][Tt][Hh]2)(?:/|$))"
                r"(?!(?:[Hh][Ee][Aa][Ll][Tt][Hh]|"
                r"[Rr][Ee][Aa][Dd][Yy])$)"
                r"[A-Za-z0-9._~!$&'()*+,;=:@/-]+$"
            ),
            constraint_description=(
                "must be a protected application-local path without a "
                "scheme, authority, query, fragment, encoding, empty or dot "
                "segments, or SAML, SCIM, OAuth, health, or readiness targets"
            ),
            description=("Protected local route used after the ALB and Cognito start managed enterprise login"),
        )
        verified_image_uri = CfnParameter(
            self,
            "ControlPlaneVerifiedImageUri",
            type="String",
            allowed_pattern=(
                rf"^[0-9]{{12}}\.dkr\.ecr\.{self.region}\.amazonaws\.com/"
                r"[a-z0-9]+(?:[._/-][a-z0-9]+)*@"
                r"sha256:[0-9a-f]{64}$"
            ),
            constraint_description=(
                f"must be an immutable private ECR URI in {self.region} ending in @sha256:<64 lowercase hex characters>"
            ),
            description=(
                "Immutable x86_64 AxonLLM server image emitted by control-plane "
                "release verification; this is distinct from the ARM64 "
                "AgentCore image"
            ),
        )
        deployment_transition_id = CfnParameter(
            self,
            "DeploymentTransitionId",
            type="String",
            default="unbound",
            max_length=64,
            allowed_pattern=r"^(?:unbound|[0-9a-f]{64})$",
            constraint_description=(
                "must be 'unbound' or the signed 64-character deployment "
                "transition identifier"
            ),
            description=(
                "Signed production transition that owns this control-plane "
                "deployment, or 'unbound' for a reviewed first deployment "
                "outside the protected promotion workflow"
            ),
        )
        Tags.of(self).add(
            "AxonLLMDeploymentTransitionId",
            deployment_transition_id.value_as_string,
        )
        runtime_state_table_name = CfnParameter(
            self,
            "RuntimeStateTableName",
            type="String",
            default="",
            min_length=0,
            max_length=255,
            allowed_pattern=r"^$|^[A-Za-z0-9_.-]{3,255}$",
            constraint_description=(
                "must be blank or a valid DynamoDB table name; the recovery "
                "guard enforces AgentCore ownership and restore namespace"
            ),
            description=("Optional restored AgentCore table selected only through the coordinated recovery workflow"),
        )
        primary_state_table_name_parameter = CfnParameter(
            self,
            "PrimaryStateTableName",
            type="String",
            min_length=3,
            max_length=255,
            allowed_pattern=r"^[A-Za-z0-9_.-]{3,255}$",
            constraint_description="must be a valid DynamoDB table name",
            description=("Primary table name read from the verified AgentCore stack outputs by the deployment wrapper"),
        )
        recovery_cutover_mode = CfnParameter(
            self,
            "RecoveryCutoverMode",
            type="String",
            default="normal",
            allowed_values=["normal", "quiesced", "selected"],
            description=(
                "Control-plane recovery phase; table changes are accepted "
                "only while every task and scaling path is stopped"
            ),
        )
        recovery_approval_id = CfnParameter(
            self,
            "RecoveryApprovalId",
            type="String",
            default="",
            max_length=128,
            allowed_pattern=r"^$|^[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}$",
            constraint_description=("must be blank or a 3-128 character change/incident ID"),
            description=("Reviewed change or incident identifier shared with the AgentCore recovery selector"),
        )
        use_recovered_state = CfnCondition(
            self,
            "UseRecoveredState",
            expression=Fn.condition_not(
                Fn.condition_equals(
                    runtime_state_table_name.value_as_string,
                    "",
                )
            ),
        )
        recovery_normal = CfnCondition(
            self,
            "RecoveryNormal",
            expression=Fn.condition_equals(
                recovery_cutover_mode.value_as_string,
                "normal",
            ),
        )
        recovery_quiesced = CfnCondition(
            self,
            "RecoveryQuiesced",
            expression=Fn.condition_equals(
                recovery_cutover_mode.value_as_string,
                "quiesced",
            ),
        )
        recovery_selected = CfnCondition(
            self,
            "RecoverySelected",
            expression=Fn.condition_equals(
                recovery_cutover_mode.value_as_string,
                "selected",
            ),
        )
        recovery_access_blocked = CfnCondition(
            self,
            "RecoveryAccessBlocked",
            expression=Fn.condition_or(
                recovery_quiesced,
                recovery_selected,
            ),
        )

        def imported(stack_name: CfnParameter, output_name: str) -> str:
            return Fn.import_value(
                Fn.join(
                    ":",
                    [stack_name.value_as_string, output_name],
                )
            )

        primary_state_table_name = primary_state_table_name_parameter.value_as_string
        selected_state_table_name = Token.as_string(
            Fn.condition_if(
                use_recovered_state.logical_id,
                runtime_state_table_name.value_as_string,
                primary_state_table_name,
            )
        )
        selected_state_table_arn = self.format_arn(
            service="dynamodb",
            resource="table",
            resource_name=selected_state_table_name,
        )
        if state_mode == "external":
            state_access = external_application_state_access(
                self,
                default_stack_name=state_stack_default,
                primary_state_table_name=primary_state_table_name,
                selected_state_table_name=selected_state_table_name,
                selected_state_table_arn=selected_state_table_arn,
            )
            data_key = state_access.data_key
            routing_config_signing_key = (
                state_access.routing_config_signing_key
            )
            event_outbox_queue = state_access.event_outbox_queue
            security_event_topic = state_access.security_event_topic
            security_event_log_group_arn = (
                state_access.security_event_log_group_arn
            )
            security_event_log_group = (
                state_access.security_event_log_group
            )
        else:
            data_key = kms.Key.from_key_arn(
                self,
                "AgentCoreDataKey",
                imported(agentcore_stack_name, "DataKeyArn"),
            )
            routing_config_signing_key = kms.Key.from_key_arn(
                self,
                "AgentCoreRoutingConfigSigningKey",
                imported(
                    agentcore_stack_name,
                    "RoutingConfigSigningKeyArn",
                ),
            )
            event_outbox_queue = sqs.Queue.from_queue_attributes(
                self,
                "AgentCoreEventOutbox",
                queue_arn=imported(
                    agentcore_stack_name,
                    "SecurityEventOutboxQueueArn",
                ),
                queue_url=imported(
                    agentcore_stack_name,
                    "SecurityEventOutboxQueueUrl",
                ),
                key_arn=data_key.key_arn,
                fifo=True,
            )
            security_event_topic = sns.Topic.from_topic_arn(
                self,
                "AgentCoreSecurityEventTopic",
                imported(
                    agentcore_stack_name,
                    "SecurityEventTopicArn",
                ),
            )
            security_event_log_group_arn = imported(
                agentcore_stack_name,
                "SecurityEventLogGroupArn",
            )
            security_event_log_group = logs.LogGroup.from_log_group_arn(
                self,
                "AgentCoreSecurityEventLogGroup",
                security_event_log_group_arn,
            )
        scim_tenants_secret = (
            secretsmanager.Secret.from_secret_complete_arn(
                self,
                "ScimTenantsSecret",
                scim_tenants_secret_arn,
            )
            if scim_tenants_secret_arn is not None
            else None
        )

        user_pool = cognito.UserPool.from_user_pool_arn(
            self,
            "IdentityUserPool",
            imported(identity_stack_name, "UserPoolArn"),
        )
        alb_client = cognito.UserPoolClient.from_user_pool_client_id(
            self,
            "IdentityAlbClient",
            imported(identity_stack_name, "AlbClientId"),
        )
        hosted_ui_domain_prefix = imported(
            identity_stack_name,
            "HostedUiDomainName",
        )
        oidc_issuer = imported(identity_stack_name, "OidcIssuer")
        tenant_claim = imported(identity_stack_name, "TenantClaimName")
        project_claim = imported(identity_stack_name, "ProjectClaimName")
        control_plane_domain_name = control_plane_domain_input.value_as_string

        image_account_id = Fn.select(
            0,
            Fn.split(".", verified_image_uri.value_as_string),
        )
        image_repository_name = Fn.select(
            0,
            Fn.split(
                "@",
                Fn.select(
                    1,
                    Fn.split(
                        ".amazonaws.com/",
                        verified_image_uri.value_as_string,
                    ),
                ),
            ),
        )
        image_repository_arn = self.format_arn(
            service="ecr",
            region=self.region,
            account=image_account_id,
            resource="repository",
            resource_name=image_repository_name,
        )

        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=2,
            restrict_default_security_group=True,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                    map_public_ip_on_launch=False,
                ),
                ec2.SubnetConfiguration(
                    name="Control",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
        )
        task_security_group = ec2.SecurityGroup(
            self,
            "TaskSecurityGroup",
            vpc=vpc,
            allow_all_outbound=False,
            description="AxonLLM control-plane tasks",
        )
        alb_security_group = ec2.SecurityGroup(
            self,
            "AlbSecurityGroup",
            vpc=vpc,
            allow_all_outbound=False,
            description="AxonLLM control-plane HTTPS ALB",
        )
        endpoint_security_group = ec2.SecurityGroup(
            self,
            "EndpointSecurityGroup",
            vpc=vpc,
            allow_all_outbound=False,
            description="Private AWS endpoints for the AxonLLM control plane",
        )

        for security_group in (task_security_group, alb_security_group):
            security_group.add_egress_rule(
                ec2.Peer.ipv4(vpc.vpc_cidr_block),
                ec2.Port.udp(53),
                "DNS to the VPC resolver",
            )
            security_group.add_egress_rule(
                ec2.Peer.ipv4(vpc.vpc_cidr_block),
                ec2.Port.tcp(53),
                "DNS fallback to the VPC resolver",
            )
        task_security_group.add_ingress_rule(
            alb_security_group,
            ec2.Port.tcp(8000),
            "Application traffic from the control-plane ALB",
        )
        custom_domain_ingress = ec2.CfnSecurityGroupIngress(
            self,
            "CustomDomainIngress",
            group_id=alb_security_group.security_group_id,
            ip_protocol="tcp",
            from_port=443,
            to_port=443,
            source_prefix_list_id=(
                approved_ingress_prefix_list_id.value_as_string
            ),
            description="HTTPS from approved control-plane clients",
        )
        custom_domain_ingress.cfn_options.condition = custom_domain_mode
        cloudfront_origin_ingress = ec2.CfnSecurityGroupIngress(
            self,
            "CloudFrontVpcOriginIngress",
            group_id=alb_security_group.security_group_id,
            ip_protocol="tcp",
            from_port=80,
            to_port=80,
            cidr_ip=vpc.vpc_cidr_block,
            description=(
                "HTTP from CloudFront VPC-origin interfaces in the dedicated VPC"
            ),
        )
        cloudfront_origin_ingress.cfn_options.condition = cloudfront_mode
        alb_security_group.add_egress_rule(
            task_security_group,
            ec2.Port.tcp(8000),
            "Application traffic to control-plane tasks",
        )
        alb_security_group.add_egress_rule(
            ec2.Peer.prefix_list(approved_https_prefix_list_id.value_as_string),
            ec2.Port.tcp(443),
            "HTTPS to Cognito and approved authentication destinations",
        )
        endpoint_security_group.add_ingress_rule(
            task_security_group,
            ec2.Port.tcp(443),
            "HTTPS from control-plane tasks",
        )
        task_security_group.add_egress_rule(
            endpoint_security_group,
            ec2.Port.tcp(443),
            "AWS services through private interface endpoints",
        )
        task_security_group.add_egress_rule(
            ec2.Peer.prefix_list(approved_https_prefix_list_id.value_as_string),
            ec2.Port.tcp(443),
            "HTTPS to approved authentication destinations",
        )

        def managed_prefix_list(
            construct_id: str,
            service_name: str,
        ) -> str:
            lookup_logs = logs.LogGroup(
                self,
                f"{construct_id}Logs",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            )
            lookup = cr.AwsCustomResource(
                self,
                construct_id,
                on_create=cr.AwsSdkCall(
                    service="EC2",
                    action="describeManagedPrefixLists",
                    parameters={
                        "Filters": [
                            {
                                "Name": "prefix-list-name",
                                "Values": [(f"com.amazonaws.{self.region}.{service_name}")],
                            }
                        ]
                    },
                    output_paths=["PrefixLists.0.PrefixListId"],
                    physical_resource_id=(cr.PhysicalResourceId.from_response("PrefixLists.0.PrefixListId")),
                ),
                policy=cr.AwsCustomResourcePolicy.from_statements(
                    [
                        iam.PolicyStatement(
                            actions=["ec2:DescribeManagedPrefixLists"],
                            resources=["*"],
                        )
                    ]
                ),
                install_latest_aws_sdk=False,
                log_group=lookup_logs,
                timeout=Duration.seconds(30),
            )
            return lookup.get_response_field("PrefixLists.0.PrefixListId")

        task_security_group.add_egress_rule(
            ec2.Peer.prefix_list(
                managed_prefix_list(
                    "DynamoDbPrefixList",
                    "dynamodb",
                )
            ),
            ec2.Port.tcp(443),
            "DynamoDB through the VPC gateway endpoint",
        )
        task_security_group.add_egress_rule(
            ec2.Peer.prefix_list(managed_prefix_list("S3PrefixList", "s3")),
            ec2.Port.tcp(443),
            "ECR image layers through the S3 gateway endpoint",
        )

        private_subnets = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)
        dynamodb_endpoint = vpc.add_gateway_endpoint(
            "DynamoDbEndpoint",
            service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
            subnets=[private_subnets],
        )
        s3_endpoint = vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
            subnets=[private_subnets],
        )
        sqs_endpoint = vpc.add_interface_endpoint(
            "SqsEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SQS,
            open=False,
            private_dns_enabled=True,
            security_groups=[endpoint_security_group],
            subnets=private_subnets,
        )
        kms_endpoint = vpc.add_interface_endpoint(
            "KmsEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.KMS,
            open=False,
            private_dns_enabled=True,
            security_groups=[endpoint_security_group],
            subnets=private_subnets,
        )
        sns_endpoint = vpc.add_interface_endpoint(
            "SnsEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SNS,
            open=False,
            private_dns_enabled=True,
            security_groups=[endpoint_security_group],
            subnets=private_subnets,
        )
        logs_endpoint = vpc.add_interface_endpoint(
            "CloudWatchLogsEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
            open=False,
            private_dns_enabled=True,
            security_groups=[endpoint_security_group],
            subnets=private_subnets,
        )
        ecr_api_endpoint = vpc.add_interface_endpoint(
            "EcrApiEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.ECR,
            open=False,
            private_dns_enabled=True,
            security_groups=[endpoint_security_group],
            subnets=private_subnets,
        )
        ecr_docker_endpoint = vpc.add_interface_endpoint(
            "EcrDockerEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
            open=False,
            private_dns_enabled=True,
            security_groups=[endpoint_security_group],
            subnets=private_subnets,
        )
        launch_worker_endpoints: dict[str, ec2.InterfaceVpcEndpoint] = {}
        if deployment_namespace:
            for construct_id, name, endpoint_service in (
                (
                    "LaunchWorkerStepFunctionsEndpoint",
                    "states",
                    ec2.InterfaceVpcEndpointAwsService.STEP_FUNCTIONS,
                ),
                (
                    "LaunchWorkerAgentCoreEndpoint",
                    "agentcore",
                    ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENTCORE,
                ),
                (
                    "LaunchWorkerCloudFormationEndpoint",
                    "cloudformation",
                    ec2.InterfaceVpcEndpointAwsService.CLOUDFORMATION,
                ),
                (
                    "LaunchWorkerEcsEndpoint",
                    "ecs",
                    ec2.InterfaceVpcEndpointAwsService.ECS,
                ),
                (
                    "LaunchWorkerApplicationAutoScalingEndpoint",
                    "application-autoscaling",
                    ec2.InterfaceVpcEndpointAwsService.APPLICATION_AUTOSCALING,
                ),
                (
                    "LaunchWorkerCloudWatchEndpoint",
                    "cloudwatch",
                    ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH,
                ),
            ):
                launch_worker_endpoints[name] = vpc.add_interface_endpoint(
                    construct_id,
                    service=endpoint_service,
                    open=False,
                    private_dns_enabled=True,
                    security_groups=[endpoint_security_group],
                    subnets=private_subnets,
                )
        configured_secrets = [scim_tenants_secret] if scim_tenants_secret is not None else []
        secrets_endpoint = None
        if configured_secrets or deployment_namespace:
            secrets_endpoint = vpc.add_interface_endpoint(
                "SecretsManagerEndpoint",
                service=(ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER),
                open=False,
                private_dns_enabled=True,
                security_groups=[endpoint_security_group],
                subnets=private_subnets,
            )
        launch_worker_execution_role = None
        if deployment_namespace:
            launch_worker_execution_role = iam.Role(
                self,
                "LaunchWorkerExecutionRole",
                role_name=(f"{_LAUNCH_EXECUTION_ROLE_NAME}{physical_suffix}"),
                assumed_by=iam.ServicePrincipal(
                    "ecs-tasks.amazonaws.com",
                    conditions={
                        "ArnLike": {
                            "aws:SourceArn": self.format_arn(
                                service="ecs",
                                resource="*",
                            )
                        },
                        "StringEquals": {"aws:SourceAccount": self.account},
                    },
                ),
                description=("Pulls the exact launch worker image and delivers worker logs"),
                max_session_duration=Duration.hours(1),
            )
            launch_worker_execution_role.add_to_principal_policy(
                iam.PolicyStatement(
                    sid="PullExactLaunchWorkerImage",
                    actions=_ECR_PULL_ACTIONS,
                    resources=[
                        self.format_arn(
                            service="ecr",
                            resource="repository",
                            resource_name="axonllm/fargate",
                        )
                    ],
                )
            )
            launch_worker_execution_role.add_to_principal_policy(
                iam.PolicyStatement(
                    sid="AuthorizeLaunchWorkerImagePull",
                    actions=["ecr:GetAuthorizationToken"],
                    resources=["*"],
                )
            )
            launch_worker_execution_role.add_to_principal_policy(
                iam.PolicyStatement(
                    sid="DeliverLaunchWorkerLogs",
                    actions=[
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                    ],
                    resources=[
                        self.format_arn(
                            service="logs",
                            resource="log-group",
                            resource_name=(f"{name}{physical_suffix}:log-stream:*"),
                            arn_format=(ArnFormat.COLON_RESOURCE_NAME),
                        )
                        for name in (
                            "/aws/ecs/axonllm/launch-workers/action",
                            "/aws/ecs/axonllm/launch-workers/cleanup",
                        )
                    ],
                )
            )
        ecs_trust = iam.ServicePrincipal(
            "ecs-tasks.amazonaws.com",
            conditions={
                "StringEquals": {"aws:SourceAccount": self.account},
                "ArnLike": {"aws:SourceArn": (f"arn:{self.partition}:ecs:{self.region}:{self.account}:*")},
            },
        )
        task_role = iam.Role(
            self,
            "TaskRole",
            assumed_by=ecs_trust,
            description=("Least-privilege AxonLLM control-plane application role"),
        )
        execution_role = iam.Role(
            self,
            "ExecutionRole",
            assumed_by=ecs_trust,
            description=("Pulls the verified control-plane image and writes logs"),
        )
        for secret in configured_secrets:
            secret.grant_read(execution_role)
        if configured_secrets:
            secrets_endpoint.add_to_policy(
                iam.PolicyStatement(
                    principals=[execution_role],
                    actions=[
                        "secretsmanager:DescribeSecret",
                        "secretsmanager:GetSecretValue",
                    ],
                    resources=[secret.secret_arn for secret in configured_secrets],
                )
            )
        task_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=_DYNAMODB_STANDARD_ACTIONS,
                resources=[
                    selected_state_table_arn,
                    f"{selected_state_table_arn}/index/*",
                ],
            )
        )
        task_role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="SignAndVerifyRoutingConfiguration",
                actions=["kms:Sign", "kms:Verify"],
                resources=[routing_config_signing_key.key_arn],
            )
        )
        if rehearsal_control_table_arn is not None:
            task_role.add_to_principal_policy(
                iam.PolicyStatement(
                    sid="UseLaunchRehearsalControlLedger",
                    actions=[
                        "dynamodb:GetItem",
                        "dynamodb:PutItem",
                    ],
                    resources=[rehearsal_control_table_arn.value_as_string],
                )
            )
        transaction_policy = iam.Policy(
            self,
            "TaskDynamoTransactionPolicy",
            statements=[
                iam.PolicyStatement(
                    actions=_DYNAMODB_TRANSACTION_ACTIONS,
                    resources=[
                        selected_state_table_arn,
                        f"{selected_state_table_arn}/index/*",
                    ],
                )
            ],
        )
        task_role.attach_inline_policy(transaction_policy)
        cfn_transaction_policy = transaction_policy.node.default_child
        if not isinstance(cfn_transaction_policy, iam.CfnPolicy):
            raise RuntimeError("DynamoDB transaction policy did not synthesize")
        # cfn-lint 1.52.1 omits this valid DynamoDB IAM action.
        cfn_transaction_policy.add_metadata(
            "cfn-lint",
            {"config": {"ignore_checks": ["W3037"]}},
        )
        recovery_deny_resource = Token.as_string(
            Fn.condition_if(
                recovery_access_blocked.logical_id,
                "*",
                self.format_arn(
                    service="dynamodb",
                    resource="table",
                    resource_name=("__axonllm_control_recovery_access_not_blocked__"),
                ),
            )
        )
        recovery_deny_policy = iam.Policy(
            self,
            "RecoveryStateAccessDeny",
            statements=[
                iam.PolicyStatement(
                    sid="BlockStateAccessDuringRecoveryTransition",
                    effect=iam.Effect.DENY,
                    actions=_DYNAMODB_STANDARD_ACTIONS,
                    resources=[recovery_deny_resource],
                )
            ],
        )
        recovery_deny_policy.attach_to_role(task_role)
        cfn_recovery_deny_policy = recovery_deny_policy.node.default_child
        if not isinstance(cfn_recovery_deny_policy, iam.CfnPolicy):
            raise RuntimeError("control-plane recovery deny policy did not synthesize")
        recovery_transaction_deny_policy = iam.Policy(
            self,
            "RecoveryStateTransactionAccessDeny",
            statements=[
                iam.PolicyStatement(
                    sid="BlockStateTransactionsDuringRecoveryTransition",
                    effect=iam.Effect.DENY,
                    actions=_DYNAMODB_TRANSACTION_ACTIONS,
                    resources=[recovery_deny_resource],
                )
            ],
        )
        recovery_transaction_deny_policy.attach_to_role(task_role)
        cfn_recovery_transaction_deny_policy = recovery_transaction_deny_policy.node.default_child
        if not isinstance(
            cfn_recovery_transaction_deny_policy,
            iam.CfnPolicy,
        ):
            raise RuntimeError("control-plane recovery transaction deny policy did not synthesize")
        # cfn-lint 1.52.1 omits this valid DynamoDB IAM action.
        cfn_recovery_transaction_deny_policy.add_metadata(
            "cfn-lint",
            {"config": {"ignore_checks": ["W3037"]}},
        )
        task_role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="UseSecurityEventOutbox",
                actions=_SQS_ACTIONS,
                resources=[event_outbox_queue.queue_arn],
            )
        )
        task_role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="PublishSecurityEvents",
                actions=["sns:Publish"],
                resources=[security_event_topic.topic_arn],
            )
        )
        task_role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="UseSecurityEventOutboxKey",
                actions=["kms:Decrypt", "kms:GenerateDataKey*"],
                resources=[data_key.key_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                        "kms:ViaService": (f"sqs.{self.region}.{self.url_suffix}"),
                    }
                },
            )
        )
        task_role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="UseSecurityEventTopicKey",
                actions=["kms:Decrypt", "kms:GenerateDataKey*"],
                resources=[data_key.key_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                        "kms:ViaService": (f"sns.{self.region}.{self.url_suffix}"),
                        "kms:EncryptionContext:aws:sns:topicArn": (security_event_topic.topic_arn),
                    }
                },
            )
        )
        security_event_log_group.grant_write(task_role)
        execution_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=_ECR_PULL_ACTIONS,
                resources=[image_repository_arn],
            )
        )
        execution_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )
        application_logs = logs.LogGroup(
            self,
            "ApplicationLogs",
            encryption_key=data_key,
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=removal_policy,
        )
        application_logs.grant_write(execution_role)

        task_definition = ecs.FargateTaskDefinition(
            self,
            "TaskDefinition",
            cpu=1024,
            memory_limit_mib=2048,
            execution_role=execution_role,
            task_role=task_role,
            family=f"axonllm-control-plane{physical_suffix}",
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.X86_64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        task_definition.add_volume(name="tmp")
        linux_parameters = ecs.LinuxParameters(
            self,
            "LinuxParameters",
            init_process_enabled=True,
        )
        linux_parameters.drop_capabilities(ecs.Capability.ALL)
        container_secrets: dict[str, ecs.Secret] = {}
        if scim_tenants_secret is not None:
            container_secrets["AXON_SCIM_TENANTS"] = ecs.Secret.from_secrets_manager(scim_tenants_secret)
        container = task_definition.add_container(
            "Application",
            image=ecs.ContainerImage.from_registry(verified_image_uri.value_as_string),
            logging=ecs.LogDrivers.aws_logs(
                log_group=application_logs,
                stream_prefix="control-plane",
            ),
            environment={
                "AWS_DEFAULT_REGION": self.region,
                "AWS_STS_REGIONAL_ENDPOINTS": "regional",
                "AXON_AWS_ACCOUNT_ID": self.account,
                "LLM_ROUTER_DYNAMODB_ENABLED": "true",
                "AXON_DYNAMODB_TABLE": selected_state_table_name,
                "AXON_ROUTING_CONFIG_SIGNING_MODE": "sign-verify",
                "AXON_ROUTING_CONFIG_SIGNING_KEY_ARN": (
                    routing_config_signing_key.key_arn
                ),
                "AXON_EVENT_OUTBOX_QUEUE_URL": event_outbox_queue.queue_url,
                "AXON_SECURITY_EVENT_SNS_TOPIC_ARN": (security_event_topic.topic_arn),
                "AXON_SECURITY_EVENT_LOG_GROUP_ARN": (security_event_log_group_arn),
                "AXON_AUTH_MODE": "ENFORCE",
                "AXON_DEPLOYMENT_PROFILE": "production",
                "AXON_LOAD_DEMO_DATA": "false",
                "AXON_OIDC_ISSUER": oidc_issuer,
                "AXON_OIDC_TENANT_CLAIM": tenant_claim,
                "AXON_OIDC_PROJECT_CLAIM": project_claim,
                "AXON_CONTROL_PLANE_ENDPOINT_MODE": (
                    endpoint_mode.value_as_string
                ),
                "AXON_REQUIRE_CANONICAL_IDENTITY": "true",
                "AXON_CONTROL_PLANE_ONLY": "true",
                "AXON_SAML_FEDERATION_MODE": "managed-cognito",
                "AXON_SAML_LOGIN_PATH": (saml_login_path.value_as_string),
                "AXON_ENABLED_PROVIDERS": "bedrock",
                "AXON_SERVER_PORT": "8000",
                "HOME": "/tmp",
                **(
                    {
                        "AXON_LAUNCH_REHEARSAL_TABLE": (rehearsal_control_table_arn.value_as_string),
                    }
                    if rehearsal_control_table_arn is not None
                    else {}
                ),
                **query_config.environment(),
            },
            secrets=container_secrets,
            health_check=ecs.HealthCheck(
                command=[
                    "CMD-SHELL",
                    (
                        'python -c "import urllib.request;'
                        "urllib.request.urlopen("
                        "'http://127.0.0.1:8000/ready',timeout=3)\""
                    ),
                ],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.seconds(60),
            ),
            linux_parameters=linux_parameters,
            readonly_root_filesystem=True,
            stop_timeout=Duration.seconds(30),
        )
        container.add_port_mappings(
            ecs.PortMapping(
                container_port=8000,
                protocol=ecs.Protocol.TCP,
            )
        )
        container.add_mount_points(
            ecs.MountPoint(
                container_path="/tmp",
                source_volume="tmp",
                read_only=False,
            )
        )

        cluster = ecs.Cluster(
            self,
            "Cluster",
            vpc=vpc,
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )
        service = ecs.FargateService(
            self,
            "Service",
            cluster=cluster,
            task_definition=task_definition,
            desired_count=2,
            assign_public_ip=False,
            vpc_subnets=private_subnets,
            security_groups=[task_security_group],
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            min_healthy_percent=100,
            max_healthy_percent=200,
            health_check_grace_period=Duration.seconds(90),
            enable_execute_command=False,
        )

        load_balancer = elbv2.ApplicationLoadBalancer(
            self,
            "LoadBalancer",
            vpc=vpc,
            internet_facing=False,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_group=alb_security_group,
            deletion_protection=deletion_protection,
            desync_mitigation_mode=elbv2.DesyncMitigationMode.STRICTEST,
            drop_invalid_header_fields=True,
        )
        cfn_load_balancer = load_balancer.node.default_child
        if not isinstance(cfn_load_balancer, elbv2.CfnLoadBalancer):
            raise RuntimeError("control-plane ALB did not synthesize")
        cfn_load_balancer.add_property_override(
            "Scheme",
            Fn.condition_if(
                custom_domain_mode.logical_id,
                "internet-facing",
                "internal",
            ),
        )
        access_logs_bucket = s3.Bucket(
            self,
            "AccessLogsBucket",
            auto_delete_objects=bool(deployment_namespace),
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            lifecycle_rules=[
                s3.LifecycleRule(
                    enabled=True,
                    expiration=Duration.days(365),
                )
            ],
            removal_policy=removal_policy,
        )
        load_balancer.log_access_logs(
            access_logs_bucket,
            prefix="alb",
        )
        target_group = elbv2.ApplicationTargetGroup(
            self,
            "TargetGroup",
            vpc=vpc,
            port=8000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            deregistration_delay=Duration.seconds(30),
            health_check=elbv2.HealthCheck(
                path="/ready",
                healthy_http_codes="200",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
            ),
        )
        service.attach_to_application_target_group(target_group)
        endpoint_listener = load_balancer.add_listener(
            "HttpsListener",
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            open=False,
            default_action=elbv2.ListenerAction.forward([target_group]),
        )
        cfn_endpoint_listener = endpoint_listener.node.default_child
        if not isinstance(cfn_endpoint_listener, elbv2.CfnListener):
            raise RuntimeError("control-plane endpoint listener did not synthesize")
        forward_action = {
            "Type": "forward",
            "Order": 1,
            "TargetGroupArn": target_group.target_group_arn,
        }
        authenticated_forward_action = {
            "Type": "forward",
            "Order": 2,
            "TargetGroupArn": target_group.target_group_arn,
        }
        authenticate_action = {
            "Type": "authenticate-cognito",
            "Order": 1,
            "AuthenticateCognitoConfig": {
                "UserPoolArn": user_pool.user_pool_arn,
                "UserPoolClientId": alb_client.user_pool_client_id,
                "UserPoolDomain": hosted_ui_domain_prefix,
                "OnUnauthenticatedRequest": "authenticate",
                "Scope": "openid email profile",
                "SessionCookieName": (
                    f"AxonLLMControlPlaneSession{physical_suffix}"
                ),
                "SessionTimeout": "3600",
            },
        }
        cfn_endpoint_listener.add_property_override(
            "Port",
            Fn.condition_if(
                custom_domain_mode.logical_id,
                443,
                80,
            ),
        )
        cfn_endpoint_listener.add_property_override(
            "Protocol",
            Fn.condition_if(
                custom_domain_mode.logical_id,
                "HTTPS",
                "HTTP",
            ),
        )
        cfn_endpoint_listener.add_property_override(
            "Certificates",
            Fn.condition_if(
                custom_domain_mode.logical_id,
                [
                    {
                        "CertificateArn": certificate_arn.value_as_string,
                    }
                ],
                {"Ref": "AWS::NoValue"},
            ),
        )
        cfn_endpoint_listener.add_property_override(
            "SslPolicy",
            Fn.condition_if(
                custom_domain_mode.logical_id,
                "ELBSecurityPolicy-TLS13-1-2-2021-06",
                {"Ref": "AWS::NoValue"},
            ),
        )
        cfn_endpoint_listener.add_property_override(
            "DefaultActions",
            Fn.condition_if(
                custom_domain_mode.logical_id,
                [authenticate_action, authenticated_forward_action],
                [forward_action],
            ),
        )
        self_authenticated_rule = elbv2.CfnListenerRule(
            self,
            "SelfAuthenticatedProtocols",
            listener_arn=endpoint_listener.listener_arn,
            priority=10,
            conditions=[
                elbv2.CfnListenerRule.RuleConditionProperty(
                    field="path-pattern",
                    path_pattern_config=(
                        elbv2.CfnListenerRule.PathPatternConfigProperty(
                            values=["/scim/*"],
                        )
                    ),
                )
            ],
            actions=[
                elbv2.CfnListenerRule.ActionProperty(
                    type="forward",
                    order=1,
                    target_group_arn=target_group.target_group_arn,
                )
            ],
        )
        self_authenticated_rule.cfn_options.condition = custom_domain_mode
        control_plane_alias = route53.CfnRecordSet(
            self,
            "ControlPlaneAlias",
            hosted_zone_id=public_hosted_zone_id.value_as_string,
            name=control_plane_domain_name,
            type="A",
            alias_target=route53.CfnRecordSet.AliasTargetProperty(
                dns_name=Fn.join(
                    "",
                    [
                        "dualstack.",
                        load_balancer.load_balancer_dns_name,
                    ],
                ),
                hosted_zone_id=(load_balancer.load_balancer_canonical_hosted_zone_id),
                evaluate_target_health=True,
            ),
        )
        control_plane_alias.cfn_options.condition = custom_domain_mode

        viewer_ip_set = wafv2.CfnIPSet(
            self,
            "CloudFrontViewerIpSet",
            addresses=allowed_viewer_cidrs.value_as_list,
            ip_address_version="IPV4",
            scope="CLOUDFRONT",
            description=(
                "Reviewed viewer networks for the generated AxonLLM endpoint"
            ),
        )
        viewer_ip_set.cfn_options.condition = cloudfront_mode
        web_acl = wafv2.CfnWebACL(
            self,
            "CloudFrontWebAcl",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(
                block={},
            ),
            scope="CLOUDFRONT",
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=(
                    f"AxonLLMControlPlane{physical_suffix.replace('-', '')}"
                ),
                sampled_requests_enabled=False,
            ),
            rules=[
                wafv2.CfnWebACL.RuleProperty(
                    name="PerViewerRateLimit",
                    priority=0,
                    action=wafv2.CfnWebACL.RuleActionProperty(
                        block={},
                    ),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=(
                            wafv2.CfnWebACL.RateBasedStatementProperty(
                                aggregate_key_type="IP",
                                limit=2_000,
                            )
                        ),
                    ),
                    visibility_config=(
                        wafv2.CfnWebACL.VisibilityConfigProperty(
                            cloud_watch_metrics_enabled=True,
                            metric_name=(
                                "AxonLLMControlPlaneRateLimit"
                                f"{physical_suffix.replace('-', '')}"
                            ),
                            sampled_requests_enabled=False,
                        )
                    ),
                ),
                wafv2.CfnWebACL.RuleProperty(
                    name="ReviewedViewerNetworks",
                    priority=1,
                    action=wafv2.CfnWebACL.RuleActionProperty(
                        allow={},
                    ),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        ip_set_reference_statement=(
                            wafv2.CfnWebACL.IPSetReferenceStatementProperty(
                                arn=viewer_ip_set.attr_arn,
                            )
                        ),
                    ),
                    visibility_config=(
                        wafv2.CfnWebACL.VisibilityConfigProperty(
                            cloud_watch_metrics_enabled=True,
                            metric_name=(
                                "AxonLLMControlPlaneAllowedViewers"
                                f"{physical_suffix.replace('-', '')}"
                            ),
                            sampled_requests_enabled=False,
                        )
                    ),
                ),
            ],
        )
        web_acl.cfn_options.condition = cloudfront_mode
        strip_untrusted_identity = cloudfront.Function(
            self,
            "StripUntrustedIdentityHeaders",
            code=cloudfront.FunctionCode.from_inline(
                """function handler(event) {
    var request = event.request;
    delete request.headers['x-amzn-oidc-data'];
    delete request.headers['x-amzn-oidc-identity'];
    delete request.headers['x-amzn-oidc-accesstoken'];
    return request;
}
"""
            ),
            runtime=cloudfront.FunctionRuntime.JS_2_0,
            comment=(
                "Remove viewer-supplied ALB identity headers before the "
                "private origin"
            ),
        )
        cfn_strip_untrusted_identity = (
            strip_untrusted_identity.node.default_child
        )
        if not isinstance(
            cfn_strip_untrusted_identity,
            cloudfront.CfnFunction,
        ):
            raise RuntimeError("CloudFront request function did not synthesize")
        cfn_strip_untrusted_identity.cfn_options.condition = cloudfront_mode
        cloudfront_origin = origins.VpcOrigin.with_application_load_balancer(
            load_balancer,
            http_port=80,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
            read_timeout=Duration.seconds(60),
            connection_timeout=Duration.seconds(10),
        )
        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=cloudfront_origin,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cached_methods=(
                    cloudfront.CachedMethods.CACHE_GET_HEAD_OPTIONS
                ),
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=(
                    cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER
                ),
                viewer_protocol_policy=(
                    cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS
                ),
                function_associations=[
                    cloudfront.FunctionAssociation(
                        event_type=(
                            cloudfront.FunctionEventType.VIEWER_REQUEST
                        ),
                        function=strip_untrusted_identity,
                    )
                ],
            ),
            comment=(
                "Generated AxonLLM control-plane endpoint"
                f"{physical_suffix}"
            ),
            enable_ipv6=False,
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            web_acl_id=web_acl.attr_arn,
        )
        cfn_distribution = distribution.node.default_child
        if not isinstance(cfn_distribution, cloudfront.CfnDistribution):
            raise RuntimeError("CloudFront distribution did not synthesize")
        cfn_distribution.cfn_options.condition = cloudfront_mode
        edge_static_oac = None
        if edge_cutover_enabled:
            if not all(
                value is not None
                for value in (
                    edge_backend_mode,
                    edge_migration_id,
                    serverless_api_domain_name,
                    serverless_api_origin_path,
                    serverless_origin_credential_secret_arn,
                    serverless_static_bucket_domain_name,
                    serverless_source_revision,
                    serverless_control_api_sha256,
                    serverless_static_assets_sha256,
                )
            ):
                raise RuntimeError(
                    "edge cutover parameters did not initialize"
                )
            Tags.of(cfn_distribution).add(
                "AxonLLMEdgeMigrationId",
                edge_migration_id.value_as_string,
            )
            Tags.of(cfn_strip_untrusted_identity).add(
                "AxonLLMEdgeMigrationId",
                edge_migration_id.value_as_string,
            )
            edge_static_oac = cloudfront.CfnOriginAccessControl(
                self,
                "ServerlessStaticOriginAccessControl",
                origin_access_control_config=(
                    cloudfront.CfnOriginAccessControl.OriginAccessControlConfigProperty(
                        name=(
                            "axonllm-edge-static"
                            f"{physical_suffix}"
                        )[:64],
                        description=(
                            "Existing AxonLLM edge access to the qualified "
                            "private static-site bucket"
                        ),
                        origin_access_control_origin_type="s3",
                        signing_behavior="always",
                        signing_protocol="sigv4",
                    )
                ),
            )
            origin_credential = Token.as_string(
                Fn.join(
                    "",
                    [
                        "{{resolve:secretsmanager:",
                        serverless_origin_credential_secret_arn.value_as_string,
                        ":SecretString}}",
                    ],
                )
            )
            cfn_distribution.add_property_override(
                "DistributionConfig.Origins.1",
                {
                    "Id": "AxonLLMServerlessApiOrigin",
                    "DomainName": (
                        serverless_api_domain_name.value_as_string
                    ),
                    "OriginPath": (
                        serverless_api_origin_path.value_as_string
                    ),
                    "OriginCustomHeaders": [
                        {
                            "HeaderName": "x-api-key",
                            "HeaderValue": origin_credential,
                        }
                    ],
                    "CustomOriginConfig": {
                        "HTTPPort": 80,
                        "HTTPSPort": 443,
                        "OriginKeepaliveTimeout": 5,
                        "OriginProtocolPolicy": "https-only",
                        "OriginReadTimeout": 30,
                        "OriginSSLProtocols": ["TLSv1.2"],
                    },
                },
            )
            cfn_distribution.add_property_override(
                "DistributionConfig.Origins.2",
                {
                    "Id": "AxonLLMServerlessStaticOrigin",
                    "DomainName": (
                        serverless_static_bucket_domain_name.value_as_string
                    ),
                    "OriginAccessControlId": edge_static_oac.attr_id,
                    "S3OriginConfig": {
                        "OriginAccessIdentity": "",
                    },
                },
            )
            cfn_strip_untrusted_identity.function_code = Token.as_string(
                Fn.join(
                    "",
                    [
                        """import cf from 'cloudfront';
function handler(event) {
    var request = event.request;
    delete request.headers['x-amzn-oidc-data'];
    delete request.headers['x-amzn-oidc-identity'];
    delete request.headers['x-amzn-oidc-accesstoken'];
    delete request.headers['x-axon-public-host'];
    var backend = '""",
                        edge_backend_mode.value_as_string,
                        """';
    if (backend !== 'serverless') {
        return request;
    }
    var uri = request.uri || '/';
    var isStatic = (
        uri === '/' ||
        uri === '/index.html' ||
        uri === '/admin/dashboard' ||
        uri.indexOf('/admin/static/') === 0
    );
    if (isStatic) {
        if (uri === '/' || uri === '/admin/dashboard') {
            request.uri = '/index.html';
        }
        cf.selectRequestOriginById('AxonLLMServerlessStaticOrigin');
        return request;
    }
    var isApi = (
        uri === '/health' ||
        uri === '/ready' ||
        uri.indexOf('/admin/') === 0 ||
        uri.indexOf('/auth/') === 0 ||
        uri.indexOf('/saml/') === 0 ||
        uri.indexOf('/scim/') === 0
    );
    if (!isApi) {
        cf.selectRequestOriginById('AxonLLMServerlessStaticOrigin');
        return request;
    }
    if (request.headers.host) {
        request.headers['x-axon-public-host'] = {
            value: request.headers.host.value
        };
    }
    cf.selectRequestOriginById('AxonLLMServerlessApiOrigin');
    return request;
}
""",
                    ],
                )
            )
        cfn_vpc_origins = [
            construct
            for construct in self.node.find_all()
            if isinstance(construct, cloudfront.CfnVpcOrigin)
        ]
        if len(cfn_vpc_origins) != 1:
            raise RuntimeError("CloudFront VPC origin did not synthesize once")
        cfn_vpc_origin = cfn_vpc_origins[0]
        cfn_vpc_origin.cfn_options.condition = cloudfront_mode
        cfn_vpc_origin.add_dependency(cfn_endpoint_listener)

        control_plane_url = Fn.join(
            "",
            [
                "https://",
                Token.as_string(
                    Fn.condition_if(
                        cloudfront_mode.logical_id,
                        distribution.distribution_domain_name,
                        control_plane_domain_name,
                    )
                ),
            ],
        )
        browser_callback_url = Fn.join(
            "",
            [
                "https://",
                distribution.distribution_domain_name,
                "/auth/callback",
            ],
        )
        browser_signed_out_url = Fn.join(
            "",
            [
                "https://",
                distribution.distribution_domain_name,
                "/auth/signed-out",
            ],
        )
        browser_client = user_pool.add_client(
            "CloudFrontBrowserClient",
            user_pool_client_name=(
                f"axonllm-control-plane-cloudfront{physical_suffix}"
            ),
            generate_secret=False,
            prevent_user_existence_errors=True,
            enable_token_revocation=True,
            auth_flows=cognito.AuthFlow(),
            access_token_validity=Duration.minutes(15),
            id_token_validity=Duration.minutes(15),
            refresh_token_validity=Duration.hours(8),
            refresh_token_rotation_grace_period=Duration.seconds(0),
            read_attributes=(
                cognito.ClientAttributes()
                .with_standard_attributes(
                    email=True,
                    email_verified=True,
                )
                .with_custom_attributes("tenant_id", "project_id")
            ),
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO
            ],
            o_auth=cognito.OAuthSettings(
                callback_urls=[browser_callback_url],
                logout_urls=[browser_signed_out_url],
                flows=cognito.OAuthFlows(
                    authorization_code_grant=True,
                    implicit_code_grant=False,
                    client_credentials=False,
                ),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
            ),
        )
        browser_client.apply_removal_policy(removal_policy)
        cfn_browser_client = browser_client.node.default_child
        if not isinstance(cfn_browser_client, cognito.CfnUserPoolClient):
            raise RuntimeError("CloudFront Cognito client did not synthesize")
        cfn_browser_client.cfn_options.condition = cloudfront_mode

        def endpoint_value(
            cloudfront_value: str,
            custom_domain_value: str = "",
        ) -> str:
            return Token.as_string(
                Fn.condition_if(
                    cloudfront_mode.logical_id,
                    cloudfront_value,
                    custom_domain_value,
                )
            )

        hosted_ui_base = Fn.join(
            "",
            [
                "https://",
                hosted_ui_domain_prefix,
                ".auth.",
                self.region,
                ".amazoncognito.com",
            ],
        )
        container.add_environment(
            "AXON_OIDC_AUDIENCE",
            endpoint_value(
                browser_client.user_pool_client_id,
                alb_client.user_pool_client_id,
            ),
        )
        container.add_environment(
            "AXON_ALB_CLIENT_ID",
            endpoint_value("", alb_client.user_pool_client_id),
        )
        container.add_environment(
            "AXON_ALB_ISSUER",
            endpoint_value(
                "",
                (
                    "https://public-keys.auth.elb."
                    f"{self.region}.amazonaws.com"
                ),
            ),
        )
        container.add_environment(
            "AXON_ALB_SIGNER_ARN",
            endpoint_value("", load_balancer.load_balancer_arn),
        )
        container.add_environment(
            "AXON_BROWSER_AUTH_MODE",
            endpoint_value("oidc-session"),
        )
        container.add_environment(
            "AXON_BROWSER_AUTH_CLIENT_ID",
            endpoint_value(browser_client.user_pool_client_id),
        )
        container.add_environment(
            "AXON_BROWSER_AUTH_AUTHORIZATION_ENDPOINT",
            endpoint_value(
                Fn.join("", [hosted_ui_base, "/oauth2/authorize"])
            ),
        )
        container.add_environment(
            "AXON_BROWSER_AUTH_OAUTH_EXCHANGE_URL",
            endpoint_value(
                Fn.join("", [hosted_ui_base, "/oauth2/token"])
            ),
        )
        container.add_environment(
            "AXON_BROWSER_AUTH_LOGOUT_ENDPOINT",
            endpoint_value(Fn.join("", [hosted_ui_base, "/logout"])),
        )
        container.add_environment(
            "AXON_BROWSER_AUTH_REDIRECT_URI",
            endpoint_value(browser_callback_url),
        )
        container.add_environment(
            "AXON_BROWSER_AUTH_SIGNED_OUT_URI",
            endpoint_value(browser_signed_out_url),
        )
        container.add_environment(
            "AXON_BROWSER_AUTH_SESSION_TTL_SECONDS",
            endpoint_value("28800", "900"),
        )
        container.add_environment(
            "AXON_CONTROL_PLANE_URL",
            control_plane_url,
        )

        scaling = service.auto_scale_task_count(
            min_capacity=2,
            max_capacity=6,
        )
        scaling.scale_on_cpu_utilization(
            "CpuScaling",
            target_utilization_percent=60,
            scale_in_cooldown=Duration.minutes(5),
            scale_out_cooldown=Duration.minutes(1),
        )
        scaling.scale_on_memory_utilization(
            "MemoryScaling",
            target_utilization_percent=70,
            scale_in_cooldown=Duration.minutes(5),
            scale_out_cooldown=Duration.minutes(1),
        )

        recovery_guard_handler_logs = logs.LogGroup(
            self,
            "RecoveryGuardHandlerLogs",
            encryption_key=data_key,
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=removal_policy,
        )
        recovery_guard_handler = lambda_.Function(
            self,
            "RecoveryGuardHandler",
            description=("Blocks unsafe control-plane DynamoDB recovery transitions"),
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(_CONTROL_PLANE_RECOVERY_GUARD),
            timeout=Duration.seconds(60),
            log_group=recovery_guard_handler_logs,
        )
        recovery_guard_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudformation:DescribeStacks"],
                resources=[
                    self.format_arn(
                        service="cloudformation",
                        resource="stack",
                        resource_name=Fn.join(
                            "",
                            [
                                agentcore_stack_name.value_as_string,
                                "/*",
                            ],
                        ),
                    ),
                    self.format_arn(
                        service="cloudformation",
                        resource="stack",
                        resource_name=f"{self.stack_name}/*",
                    ),
                ],
            )
        )
        recovery_guard_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:DescribeServices"],
                resources=[
                    self.format_arn(
                        service="ecs",
                        resource="service",
                        resource_name="*/*",
                    )
                ],
            )
        )
        recovery_guard_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=["application-autoscaling:DescribeScalableTargets"],
                resources=["*"],
            )
        )
        recovery_guard_provider_logs = logs.LogGroup(
            self,
            "RecoveryGuardProviderLogs",
            encryption_key=data_key,
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=removal_policy,
        )
        recovery_guard_provider = cr.Provider(
            self,
            "RecoveryGuardProvider",
            on_event_handler=recovery_guard_handler,
            log_group=recovery_guard_provider_logs,
        )
        recovery_guard = CustomResource(
            self,
            "RecoveryGuard",
            service_token=recovery_guard_provider.service_token,
            properties={
                "AgentCoreStackName": (agentcore_stack_name.value_as_string),
                "ApprovalId": recovery_approval_id.value_as_string,
                "ControlPlaneStackName": self.stack_name,
                "Mode": recovery_cutover_mode.value_as_string,
                "PrimaryTable": primary_state_table_name,
                "SelectedTable": selected_state_table_name,
            },
        )
        recovery_guard_resource = recovery_guard.node.default_child
        if not isinstance(recovery_guard_resource, CfnResource):
            raise RuntimeError("control-plane recovery guard did not synthesize")

        cfn_service = service.node.default_child
        if not isinstance(cfn_service, ecs.CfnService):
            raise RuntimeError("control-plane service did not synthesize")
        cfn_service.add_override(
            "Properties.DesiredCount",
            Fn.condition_if(
                recovery_normal.logical_id,
                2,
                0,
            ),
        )
        cfn_service.add_dependency(recovery_guard_resource)

        scaling_targets = [
            child
            for child in scaling.node.find_all()
            if isinstance(child, CfnResource)
            and child.cfn_resource_type == "AWS::ApplicationAutoScaling::ScalableTarget"
        ]
        if len(scaling_targets) != 1:
            raise RuntimeError("control-plane scalable target did not synthesize")
        cfn_scaling_target = scaling_targets[0]
        cfn_scaling_target.add_override(
            "Properties.MinCapacity",
            Fn.condition_if(
                recovery_normal.logical_id,
                2,
                0,
            ),
        )
        cfn_scaling_target.add_override(
            "Properties.SuspendedState",
            Fn.condition_if(
                recovery_normal.logical_id,
                {
                    key: False
                    for key in (
                        "DynamicScalingInSuspended",
                        "DynamicScalingOutSuspended",
                        "ScheduledScalingSuspended",
                    )
                },
                {
                    key: True
                    for key in (
                        "DynamicScalingInSuspended",
                        "DynamicScalingOutSuspended",
                        "ScheduledScalingSuspended",
                    )
                },
            ),
        )
        cfn_scaling_target.add_dependency(recovery_guard_resource)
        cfn_recovery_deny_policy.add_dependency(recovery_guard_resource)
        cfn_recovery_transaction_deny_policy.add_dependency(recovery_guard_resource)

        launch_task_role_arns: list[str] = []
        launch_task_principals: list[iam.IPrincipal] = []
        launch_execution_principal: iam.IPrincipal | None = None
        if deployment_namespace:

            def role_arn(role_name: str) -> str:
                return self.format_arn(
                    service="iam",
                    region="",
                    resource="role",
                    resource_name=role_name,
                )

            launch_task_role_arns = [
                role_arn(_LAUNCH_ACTION_ROLE_NAME),
                role_arn(_LAUNCH_CLEANUP_ROLE_NAME),
            ]
            launch_task_principals = [
                iam.ArnPrincipal(arn) for arn in launch_task_role_arns
            ]
            launch_execution_principal = launch_worker_execution_role

        dynamodb_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=_DYNAMODB_ACTIONS,
                resources=[
                    selected_state_table_arn,
                    f"{selected_state_table_arn}/index/*",
                ],
                conditions={
                    "ArnEquals": {
                        "aws:PrincipalArn": task_role.role_arn,
                    }
                },
            )
        )
        kms_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[task_role],
                actions=["kms:Sign", "kms:Verify"],
                resources=[routing_config_signing_key.key_arn],
            )
        )
        if rehearsal_control_table_arn is not None:
            dynamodb_endpoint.add_to_policy(
                iam.PolicyStatement(
                    principals=[iam.AnyPrincipal()],
                    actions=[
                        "dynamodb:GetItem",
                        "dynamodb:PutItem",
                    ],
                    resources=[rehearsal_control_table_arn.value_as_string],
                    conditions={
                        "ArnEquals": {
                            "aws:PrincipalArn": task_role.role_arn,
                        }
                    },
                )
            )
        s3_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=["s3:GetObject"],
                resources=[
                    (
                        f"arn:{self.partition}:s3:::"
                        f"prod-{self.region}-starport-layer-bucket/*"
                    )
                ],
            )
        )
        sqs_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[task_role],
                actions=_SQS_ACTIONS,
                resources=[event_outbox_queue.queue_arn],
            )
        )
        sns_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[task_role],
                actions=["sns:Publish"],
                resources=[security_event_topic.topic_arn],
            )
        )
        logs_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[task_role, execution_role],
                actions=[
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=[
                    application_logs.log_group_arn,
                    f"{application_logs.log_group_arn}:*",
                    security_event_log_group_arn,
                    f"{security_event_log_group_arn}:*",
                ],
            )
        )
        for endpoint in (ecr_api_endpoint, ecr_docker_endpoint):
            endpoint.add_to_policy(
                iam.PolicyStatement(
                    principals=[execution_role],
                    actions=_ECR_PULL_ACTIONS,
                    resources=[image_repository_arn],
                )
            )
        ecr_api_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[execution_role],
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )
        if launch_task_principals:
            launch_state_table_arn = self.format_arn(
                service="dynamodb",
                resource="table",
                resource_name=(f"axonllm-agentcore-state{physical_suffix}"),
            )
            launch_restored_table_arn = f"{launch_state_table_arn}-restore-validation-*"
            launch_lease_table_arn = self.format_arn(
                service="dynamodb",
                resource="table",
                resource_name="axonllm-launch-rehearsal-leases",
            )
            launch_runtime_arn = self.format_arn(
                service="bedrock-agentcore",
                resource="runtime",
                resource_name=(f"axonllm_{deployment_namespace.replace('-', '_')}-*"),
            )
            launch_stack_arns = [
                self.format_arn(
                    service="cloudformation",
                    resource="stack",
                    resource_name=f"{name}{physical_suffix}/*",
                )
                for name in (
                    "AxonLLMAgentCoreStack",
                    "AxonLLMControlPlaneStack",
                )
            ]
            launch_queue_arns = [
                self.format_arn(
                    service="sqs",
                    resource=(f"AxonLLMAgentCoreStack{physical_suffix}-{queue_name}*"),
                    arn_format=ArnFormat.NO_RESOURCE_NAME,
                )
                for queue_name in (
                    "SecurityEventOutboxQueue",
                    "SecurityEventDeadLetterQueue",
                )
            ]
            launch_security_log_group_arn = self.format_arn(
                service="logs",
                resource="log-group",
                resource_name=(f"AxonLLMAgentCoreStack{physical_suffix}-SecurityEventLogGroup*"),
                arn_format=ArnFormat.COLON_RESOURCE_NAME,
            )
            launch_control_service_arn = self.format_arn(
                service="ecs",
                resource="service",
                resource_name=(f"*/AxonLLMControlPlaneStack{physical_suffix}-Service*"),
            )
            launch_secret_arn = self.format_arn(
                service="secretsmanager",
                resource="secret",
                resource_name=("axonllm/launch/runtime-identity-*"),
                arn_format=ArnFormat.COLON_RESOURCE_NAME,
            )
            launch_activity_arns = [
                self.format_arn(
                    service="states",
                    resource="activity",
                    resource_name=activity_name,
                    arn_format=ArnFormat.COLON_RESOURCE_NAME,
                )
                for activity_name in (
                    "axonllm-agentcore-launch-actions",
                    "axonllm-agentcore-launch-cleanup",
                )
            ]

            dynamodb_endpoint.add_to_policy(
                iam.PolicyStatement(
                    principals=[iam.AnyPrincipal()],
                    actions=_LAUNCH_WORKER_DYNAMODB_ACTIONS,
                    resources=[
                        launch_state_table_arn,
                        f"{launch_state_table_arn}/index/*",
                        launch_restored_table_arn,
                        f"{launch_restored_table_arn}/index/*",
                        launch_lease_table_arn,
                        rehearsal_control_table_arn.value_as_string,
                    ],
                    conditions={
                        "ArnEquals": {
                            "aws:PrincipalArn": launch_task_role_arns,
                        }
                    },
                )
            )
            sqs_endpoint.add_to_policy(
                iam.PolicyStatement(
                    principals=launch_task_principals,
                    actions=_LAUNCH_WORKER_SQS_ACTIONS,
                    resources=launch_queue_arns,
                )
            )
            logs_endpoint.add_to_policy(
                iam.PolicyStatement(
                    principals=launch_task_principals,
                    actions=[
                        "logs:CreateLogStream",
                        "logs:DeleteLogStream",
                        "logs:FilterLogEvents",
                    ],
                    resources=[
                        launch_security_log_group_arn,
                        (f"{launch_security_log_group_arn}:log-stream:axonllm-launch-*"),
                    ],
                )
            )
            if secrets_endpoint is None:
                raise RuntimeError("launch worker Secrets Manager endpoint did not synthesize")
            secrets_endpoint.add_to_policy(
                iam.PolicyStatement(
                    principals=launch_task_principals,
                    actions=[
                        "secretsmanager:DescribeSecret",
                        "secretsmanager:GetSecretValue",
                    ],
                    resources=[launch_secret_arn],
                )
            )
            launch_worker_endpoints["states"].add_to_policy(
                iam.PolicyStatement(
                    principals=launch_task_principals,
                    actions=["states:GetActivityTask"],
                    resources=launch_activity_arns,
                )
            )
            launch_worker_endpoints["states"].add_to_policy(
                iam.PolicyStatement(
                    principals=launch_task_principals,
                    actions=[
                        "states:SendTaskFailure",
                        "states:SendTaskHeartbeat",
                        "states:SendTaskSuccess",
                    ],
                    resources=["*"],
                    conditions={"StringEquals": {"aws:RequestedRegion": self.region}},
                )
            )
            launch_worker_endpoints["agentcore"].add_to_policy(
                iam.PolicyStatement(
                    principals=launch_task_principals,
                    actions=[
                        "bedrock-agentcore:GetAgentRuntime",
                        "bedrock-agentcore:GetAgentRuntimeEndpoint",
                        "bedrock-agentcore:InvokeAgentRuntime",
                    ],
                    resources=[
                        launch_runtime_arn,
                        f"{launch_runtime_arn}/runtime-endpoint/*",
                    ],
                )
            )
            launch_worker_endpoints["cloudformation"].add_to_policy(
                iam.PolicyStatement(
                    principals=launch_task_principals,
                    actions=[
                        "cloudformation:DescribeStacks",
                        "cloudformation:UpdateStack",
                    ],
                    resources=launch_stack_arns,
                )
            )
            launch_worker_endpoints["ecs"].add_to_policy(
                iam.PolicyStatement(
                    principals=launch_task_principals,
                    actions=[
                        "ecs:DescribeServices",
                        "ecs:UpdateService",
                    ],
                    resources=[launch_control_service_arn],
                )
            )
            launch_worker_endpoints["application-autoscaling"].add_to_policy(
                iam.PolicyStatement(
                    principals=launch_task_principals,
                    actions=[
                        ("application-autoscaling:DescribeScalableTargets"),
                        ("application-autoscaling:RegisterScalableTarget"),
                    ],
                    resources=["*"],
                    conditions={"StringEquals": {"aws:RequestedRegion": self.region}},
                )
            )
            launch_worker_endpoints["cloudwatch"].add_to_policy(
                iam.PolicyStatement(
                    principals=launch_task_principals,
                    actions=[
                        "cloudwatch:DescribeAlarms",
                        "cloudwatch:PutMetricData",
                    ],
                    resources=["*"],
                    conditions={"StringEquals": {"aws:RequestedRegion": self.region}},
                )
            )

            if launch_execution_principal is None:
                raise RuntimeError("launch worker execution principal did not synthesize")
            launch_worker_log_group_arns = [
                self.format_arn(
                    service="logs",
                    resource="log-group",
                    resource_name=f"{name}{physical_suffix}",
                    arn_format=ArnFormat.COLON_RESOURCE_NAME,
                )
                for name in (
                    "/aws/ecs/axonllm/launch-workers/action",
                    "/aws/ecs/axonllm/launch-workers/cleanup",
                )
            ]
            logs_endpoint.add_to_policy(
                iam.PolicyStatement(
                    principals=[launch_execution_principal],
                    actions=[
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                    ],
                    resources=[f"{log_group_arn}:log-stream:*" for log_group_arn in launch_worker_log_group_arns],
                )
            )
            launch_worker_repository_arn = self.format_arn(
                service="ecr",
                resource="repository",
                resource_name="axonllm/fargate",
            )
            for endpoint in (
                ecr_api_endpoint,
                ecr_docker_endpoint,
            ):
                endpoint.add_to_policy(
                    iam.PolicyStatement(
                        principals=[launch_execution_principal],
                        actions=_ECR_PULL_ACTIONS,
                        resources=[launch_worker_repository_arn],
                    )
                )
            ecr_api_endpoint.add_to_policy(
                iam.PolicyStatement(
                    principals=[launch_execution_principal],
                    actions=["ecr:GetAuthorizationToken"],
                    resources=["*"],
                )
            )
        endpoint_mode_output = CfnOutput(
            self,
            "EndpointModeOutput",
            value=endpoint_mode.value_as_string,
            description="Selected control-plane endpoint architecture",
        )
        endpoint_mode_output.override_logical_id("EndpointMode")
        CfnOutput(
            self,
            "ControlPlaneUrl",
            value=control_plane_url,
            description="Canonical HTTPS URL for the web control plane",
        )
        CfnOutput(
            self,
            "ControlPlaneDomainName",
            value=Token.as_string(
                Fn.condition_if(
                    cloudfront_mode.logical_id,
                    distribution.distribution_domain_name,
                    control_plane_domain_name,
                )
            ),
            description="Canonical control-plane hostname",
        )
        CfnOutput(
            self,
            "ControlPlaneAuthMode",
            value=Token.as_string(
                Fn.condition_if(
                    cloudfront_mode.logical_id,
                    "application-oidc",
                    "alb-cognito",
                )
            ),
            description="Browser authentication boundary",
        )
        browser_client_output = CfnOutput(
            self,
            "BrowserClientId",
            value=browser_client.user_pool_client_id,
            description="CloudFront-mode Cognito PKCE client",
        )
        browser_client_output.condition = cloudfront_mode
        distribution_id_output = CfnOutput(
            self,
            "DistributionId",
            value=distribution.distribution_id,
            description="Generated-endpoint CloudFront distribution",
        )
        distribution_id_output.condition = cloudfront_mode
        distribution_domain_output = CfnOutput(
            self,
            "DistributionDomainName",
            value=distribution.distribution_domain_name,
            description="AWS-generated CloudFront hostname",
        )
        distribution_domain_output.condition = cloudfront_mode
        web_acl_output = CfnOutput(
            self,
            "WebAclArn",
            value=web_acl.attr_arn,
            description="CloudFront viewer allowlist and rate-limit WebACL",
        )
        web_acl_output.condition = cloudfront_mode
        vpc_origin_output = CfnOutput(
            self,
            "VpcOriginId",
            value=cfn_vpc_origin.attr_id,
            description="Private CloudFront origin attachment",
        )
        vpc_origin_output.condition = cloudfront_mode
        if edge_cutover_enabled:
            if edge_static_oac is None:
                raise RuntimeError(
                    "edge static origin access control did not initialize"
                )
            def edge_output(
                name: str,
                value: str,
                description: str = "",
            ) -> None:
                output = CfnOutput(
                    self,
                    f"{name}Output",
                    value=value,
                    **(
                        {"description": description}
                        if description
                        else {}
                    ),
                )
                output.override_logical_id(name)
                output.condition = cloudfront_mode

            edge_output(
                "EdgeBackendMode",
                edge_backend_mode.value_as_string,
                (
                    "Current reviewed backend selected by the existing "
                    "CloudFront distribution"
                ),
            )
            edge_output(
                "EdgeMigrationId",
                edge_migration_id.value_as_string,
                "Content-addressed qualification and rollback plan",
            )
            edge_output(
                "ProductionDistributionArn",
                self.format_arn(
                    service="cloudfront",
                    region="",
                    account=self.account,
                    resource="distribution",
                    resource_name=distribution.distribution_id,
                    arn_format=ArnFormat.SLASH_RESOURCE_NAME,
                ),
                (
                    "Existing customer-facing CloudFront distribution ARN"
                ),
            )
            edge_output(
                "ServerlessControlApiDomainName",
                serverless_api_domain_name.value_as_string,
            )
            edge_output(
                "ServerlessControlApiOriginPath",
                serverless_api_origin_path.value_as_string,
            )
            edge_output(
                "ServerlessOriginCredentialSecretArn",
                serverless_origin_credential_secret_arn.value_as_string,
            )
            edge_output(
                "ServerlessStaticBucketRegionalDomainName",
                serverless_static_bucket_domain_name.value_as_string,
            )
            edge_output(
                "ServerlessStaticOriginAccessControlId",
                edge_static_oac.attr_id,
            )
            edge_output(
                "ServerlessSourceRevision",
                serverless_source_revision.value_as_string,
            )
            edge_output(
                "ServerlessControlApiSha256",
                serverless_control_api_sha256.value_as_string,
            )
            edge_output(
                "ServerlessStaticAssetsSha256",
                serverless_static_assets_sha256.value_as_string,
            )
        CfnOutput(
            self,
            "LoadBalancerScheme",
            value=Token.as_string(
                Fn.condition_if(
                    custom_domain_mode.logical_id,
                    "internet-facing",
                    "internal",
                )
            ),
        )
        CfnOutput(
            self,
            "AgentCoreStackNameOutput",
            value=agentcore_stack_name.value_as_string,
        ).override_logical_id("AgentCoreStackName")
        if state_mode == "external":
            application_state_stack_output = CfnOutput(
                self,
                "ApplicationStateStackNameOutput",
                value=state_access.stack_name,
            )
            application_state_stack_output.override_logical_id(
                "ApplicationStateStackName"
            )
        CfnOutput(
            self,
            "PrimaryStateTableNameOutput",
            value=primary_state_table_name,
        ).override_logical_id("PrimaryStateTableName")
        CfnOutput(
            self,
            "SelectedRuntimeStateTableName",
            value=selected_state_table_name,
        )
        CfnOutput(
            self,
            "RecoveryCutoverModeOutput",
            value=recovery_cutover_mode.value_as_string,
        ).override_logical_id("RecoveryCutoverMode")
        CfnOutput(
            self,
            "RecoveryApprovalIdOutput",
            value=recovery_approval_id.value_as_string,
        ).override_logical_id("RecoveryApprovalId")
        CfnOutput(
            self,
            "LoadBalancerDnsName",
            value=load_balancer.load_balancer_dns_name,
        )
        CfnOutput(
            self,
            "TargetGroupArn",
            value=target_group.target_group_arn,
        )
        CfnOutput(
            self,
            "ClusterName",
            value=cluster.cluster_name,
        )
        CfnOutput(
            self,
            "ClusterArn",
            value=cluster.cluster_arn,
        )
        CfnOutput(
            self,
            "SubnetIds",
            value=Fn.join(
                ",",
                vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnet_ids,
            ),
        )
        CfnOutput(
            self,
            "TaskSecurityGroupId",
            value=task_security_group.security_group_id,
        )
        CfnOutput(
            self,
            "ServiceName",
            value=service.service_name,
        )
        CfnOutput(
            self,
            "ControlPlaneImageUri",
            value=verified_image_uri.value_as_string,
        )
        CfnOutput(
            self,
            "DeploymentTransitionIdOutput",
            value=deployment_transition_id.value_as_string,
        ).override_logical_id("DeploymentTransitionId")
        CfnOutput(
            self,
            "TaskDefinitionArn",
            value=task_definition.task_definition_arn,
        )
        CfnOutput(
            self,
            "QueryPlaneEnabled",
            value="true" if query_config.enabled else "false",
        )
        if scim_tenants_secret is not None:
            CfnOutput(
                self,
                "ScimTenantsSecretArn",
                value=scim_tenants_secret.secret_arn,
            )
