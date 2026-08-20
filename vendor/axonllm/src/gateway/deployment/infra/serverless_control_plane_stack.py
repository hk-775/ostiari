"""Static CloudFront UI and request-driven Lambda control API."""

from __future__ import annotations

import re
from pathlib import Path

from aws_cdk import (
    CfnCondition,
    CfnOutput,
    CfnParameter,
    CustomResource,
    Duration,
    Fn,
    RemovalPolicy,
    Size,
    Stack,
    Tags,
    Token,
    aws_apigateway as apigateway,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
    aws_wafv2 as wafv2,
)
from constructs import Construct

if __package__:
    from .application_state import external_application_state_access
else:
    from application_state import external_application_state_access


_SECRET_ARN = re.compile(
    r"^arn:(?:aws|aws-us-gov|aws-cn):secretsmanager:"
    r"[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]{1,512}$"
)
_DYNAMODB_ACTIONS = [
    "dynamodb:BatchGetItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:ConditionCheckItem",
    "dynamodb:DeleteItem",
    "dynamodb:DescribeTable",
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:TransactWriteItems",
    "dynamodb:UpdateItem",
]


def _parameter(
    stack: Stack,
    logical_id: str,
    *,
    description: str,
    allowed_pattern: str,
    default: str | None = None,
) -> CfnParameter:
    options: dict[str, object] = {
        "type": "String",
        "allowed_pattern": allowed_pattern,
        "constraint_description": description,
        "description": description,
    }
    if default is not None:
        options["default"] = default
    return CfnParameter(stack, logical_id, **options)


class AxonLLMServerlessControlPlaneStack(Stack):
    """Deploy the AgentCore web control plane without always-on compute."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        deployment_namespace: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if not Token.is_unresolved(self.region) and self.region != "us-east-1":
            raise ValueError("CloudFront WAF control-plane stacks must deploy in us-east-1")

        physical_suffix = f"-{deployment_namespace}" if deployment_namespace else ""
        removal_policy = RemovalPolicy.DESTROY if deployment_namespace else RemovalPolicy.RETAIN
        edge_attachment_enabled = self.node.try_get_context(
            "edge_attachment_enabled"
        )
        if edge_attachment_enabled is None:
            edge_attachment_enabled = False
        if not isinstance(edge_attachment_enabled, bool):
            raise ValueError("edge_attachment_enabled must be a boolean")
        state_stack_default = "AxonLLMApplicationStateStack" + (
            f"-{deployment_namespace}" if deployment_namespace else ""
        )

        primary_state_table = _parameter(
            self,
            "PrimaryStateTableName",
            description="must be the canonical AxonLLM DynamoDB table name",
            allowed_pattern=r"^[A-Za-z0-9_.-]{3,255}$",
        )
        runtime_state_table = _parameter(
            self,
            "RuntimeStateTableName",
            description=("must be blank or an approved restore-validation table"),
            allowed_pattern=r"^$|^[A-Za-z0-9_.-]{3,255}$",
            default="",
        )
        use_recovered_state = CfnCondition(
            self,
            "UseRecoveredState",
            expression=Fn.condition_not(
                Fn.condition_equals(
                    runtime_state_table.value_as_string,
                    "",
                )
            ),
        )
        selected_state_table_name = Token.as_string(
            Fn.condition_if(
                use_recovered_state.logical_id,
                runtime_state_table.value_as_string,
                primary_state_table.value_as_string,
            )
        )
        selected_state_table_arn = self.format_arn(
            service="dynamodb",
            resource="table",
            resource_name=selected_state_table_name,
        )
        state = external_application_state_access(
            self,
            default_stack_name=state_stack_default,
            primary_state_table_name=primary_state_table.value_as_string,
            selected_state_table_name=selected_state_table_name,
            selected_state_table_arn=selected_state_table_arn,
        )

        identity_user_pool_id = _parameter(
            self,
            "IdentityUserPoolId",
            description="must be the retained AxonLLM Cognito user-pool ID",
            allowed_pattern=r"^[a-z0-9-]+_[A-Za-z0-9]+$",
        )
        identity_oidc_issuer = _parameter(
            self,
            "IdentityOidcIssuer",
            description="must be the exact HTTPS Cognito OIDC issuer",
            allowed_pattern=(
                r"^https://cognito-idp\.[a-z0-9-]+\."
                r"(?:amazonaws\.com|amazonaws\.com\.cn)/"
                r"[a-z0-9-]+_[A-Za-z0-9]+$"
            ),
        )
        hosted_ui_domain_name = _parameter(
            self,
            "IdentityHostedUiDomainName",
            description="must be the Cognito managed-login hostname",
            allowed_pattern=(
                r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?"
                r"\.auth\.[a-z0-9-]+\.amazoncognito\.com(?:\.cn)?$"
            ),
        )
        tenant_claim = _parameter(
            self,
            "OidcTenantClaim",
            description="must be the configured tenant claim",
            allowed_pattern=r"^[^\s]{1,256}$",
            default="custom:tenant_id",
        )
        project_claim = _parameter(
            self,
            "OidcProjectClaim",
            description="must be the configured project claim",
            allowed_pattern=r"^[^\s]{1,256}$",
            default="custom:project_id",
        )
        allowed_viewer_cidrs = CfnParameter(
            self,
            "AllowedViewerCidrs",
            type="CommaDelimitedList",
            default="192.0.2.0/32",
            description=("Reviewed IPv4 viewer CIDRs allowed by the CloudFront WAF"),
        )
        source_revision = _parameter(
            self,
            "SourceRevision",
            description="must be the full reviewed source commit SHA",
            allowed_pattern=r"^[0-9a-f]{40}$",
        )
        production_distribution_arn = (
            _parameter(
                self,
                "ProductionDistributionArn",
                description=(
                    "must be the existing production CloudFront distribution "
                    "ARN retained as the edge owner"
                ),
                allowed_pattern=(
                    r"^arn:(?:aws|aws-us-gov|aws-cn):cloudfront::"
                    r"[0-9]{12}:distribution/[A-Z0-9]{13,32}$"
                ),
            )
            if edge_attachment_enabled
            else None
        )
        production_distribution_id = (
            _parameter(
                self,
                "ProductionDistributionId",
                description=(
                    "must be the existing production CloudFront distribution "
                    "ID retained as the edge owner"
                ),
                allowed_pattern=r"^[A-Z0-9]{13,32}$",
            )
            if edge_attachment_enabled
            else None
        )
        production_control_plane_hostname = (
            _parameter(
                self,
                "ProductionControlPlaneHostname",
                description=(
                    "must be the existing production control-plane hostname "
                    "preserved through cutover"
                ),
                allowed_pattern=(
                    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}"
                    r"[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}"
                    r"[a-z0-9])$"
                ),
            )
            if edge_attachment_enabled
            else None
        )

        artifact_bucket_name = _parameter(
            self,
            "ArtifactBucketName",
            description="must be the private release artifact bucket",
            allowed_pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
        )
        artifact_bucket_key_arn = _parameter(
            self,
            "ArtifactBucketKeyArn",
            description=("must be the complete KMS key ARN encrypting release artifacts"),
            allowed_pattern=(
                r"^arn:(?:aws|aws-us-gov|aws-cn):kms:us-east-1:"
                r"[0-9]{12}:key/[0-9a-fA-F-]{36}$"
            ),
        )
        control_code_key = _parameter(
            self,
            "ControlApiCodeObjectKey",
            description="must be the content-addressed Lambda ZIP key",
            allowed_pattern=r"^[A-Za-z0-9!_.*'()/-]{1,1024}\.zip$",
        )
        control_code_version = _parameter(
            self,
            "ControlApiCodeObjectVersion",
            description="must be the immutable S3 object version",
            allowed_pattern=r"^[A-Za-z0-9._~-]{1,1024}$",
        )
        control_code_sha256 = _parameter(
            self,
            "ControlApiCodeSha256",
            description="must be the verified Lambda ZIP SHA-256",
            allowed_pattern=r"^[0-9a-f]{64}$",
        )
        static_assets_key = _parameter(
            self,
            "StaticAssetsObjectKey",
            description="must be the content-addressed static-site ZIP key",
            allowed_pattern=r"^[A-Za-z0-9!_.*'()/-]{1,1024}\.zip$",
        )
        static_assets_version = _parameter(
            self,
            "StaticAssetsObjectVersion",
            description="must be the immutable static-site S3 object version",
            allowed_pattern=r"^[A-Za-z0-9._~-]{1,1024}$",
        )
        static_assets_sha256 = _parameter(
            self,
            "StaticAssetsSha256",
            description="must be the verified static-site ZIP SHA-256",
            allowed_pattern=r"^[0-9a-f]{64}$",
        )
        export_queue_url = _parameter(
            self,
            "ExportQueueUrl",
            description=("must be the serverless-workers FIFO export queue URL"),
            allowed_pattern=(
                r"^https://sqs\.[a-z0-9-]+\."
                r"(?:amazonaws\.com|amazonaws\.com\.cn)/"
                r"[0-9]{12}/[A-Za-z0-9_-]{1,75}\.fifo$"
            ),
        )
        export_queue_arn = _parameter(
            self,
            "ExportQueueArn",
            description=("must be the serverless-workers FIFO export queue ARN"),
            allowed_pattern=(
                r"^arn:(?:aws|aws-us-gov|aws-cn):sqs:"
                r"[a-z0-9-]+:[0-9]{12}:"
                r"[A-Za-z0-9_-]{1,75}\.fifo$"
            ),
        )
        export_bucket_name = _parameter(
            self,
            "ExportBucketName",
            description=("must be the private serverless-workers export bucket"),
            allowed_pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
        )

        artifact_key = kms.Key.from_key_arn(
            self,
            "ArtifactBucketKey",
            artifact_bucket_key_arn.value_as_string,
        )
        artifact_bucket = s3.Bucket.from_bucket_attributes(
            self,
            "ArtifactBucket",
            bucket_name=artifact_bucket_name.value_as_string,
            bucket_arn=Fn.join(
                "",
                ["arn:", self.partition, ":s3:::", artifact_bucket_name.value_as_string],
            ),
            encryption_key=artifact_key,
        )

        site_key = kms.Key(
            self,
            "StaticSiteKey",
            alias=(
                f"alias/axonllm/serverless-static-site{physical_suffix}"
            ),
            description=(
                "Encrypts AxonLLM serverless static-site objects"
            ),
            enable_key_rotation=True,
            removal_policy=removal_policy,
            pending_window=Duration.days(30),
        )
        site_bucket = s3.Bucket(
            self,
            "StaticSiteBucket",
            auto_delete_objects=bool(deployment_namespace),
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            bucket_key_enabled=True,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=site_key,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=removal_policy,
            versioned=True,
        )
        if production_distribution_arn is not None:
            site_bucket.add_to_resource_policy(
                iam.PolicyStatement(
                    sid="AllowExactProductionCloudFrontDistribution",
                    actions=["s3:GetObject"],
                    principals=[
                        iam.ServicePrincipal("cloudfront.amazonaws.com")
                    ],
                    resources=[site_bucket.arn_for_objects("*")],
                    conditions={
                        "StringEquals": {
                            "AWS:SourceArn": (
                                production_distribution_arn.value_as_string
                            )
                        }
                    },
                )
            )
        access_logs_bucket = s3.Bucket(
            self,
            "AccessLogsBucket",
            access_control=s3.BucketAccessControl.LOG_DELIVERY_WRITE,
            auto_delete_objects=bool(deployment_namespace),
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    enabled=True,
                    expiration=Duration.days(365),
                )
            ],
            object_ownership=s3.ObjectOwnership.OBJECT_WRITER,
            removal_policy=removal_policy,
        )

        function_name = f"axonllm-control-api{physical_suffix}"
        application_logs = logs.LogGroup(
            self,
            "ControlApiLogs",
            encryption_key=state.data_key,
            log_group_name=f"/aws/lambda/{function_name}",
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=removal_policy,
        )
        browser_client_name = f"axonllm-serverless-control{physical_suffix}"
        control_function = lambda_.Function(
            self,
            "ControlApi",
            architecture=lambda_.Architecture.ARM_64,
            code=lambda_.Code.from_bucket(
                artifact_bucket,
                control_code_key.value_as_string,
                object_version=control_code_version.value_as_string,
            ),
            description=Fn.join(
                "",
                [
                    "AxonLLM serverless control API artifact ",
                    control_code_sha256.value_as_string,
                ],
            ),
            environment={
                "AWS_STS_REGIONAL_ENDPOINTS": "regional",
                "AXON_AWS_ACCOUNT_ID": self.account,
                "AXON_AUTH_MODE": "ENFORCE",
                "AXON_BROWSER_AUTH_MODE": "oidc-session",
                "AXON_COGNITO_BROWSER_CLIENT_NAME": browser_client_name,
                "AXON_COGNITO_HOSTED_UI_URL": Fn.join(
                    "",
                    ["https://", hosted_ui_domain_name.value_as_string],
                ),
                "AXON_COGNITO_USER_POOL_ID": (identity_user_pool_id.value_as_string),
                "AXON_CONTROL_PLANE_ENDPOINT_MODE": "cloudfront",
                "AXON_CONTROL_PLANE_ONLY": "true",
                "AXON_DEPLOYMENT_PROFILE": "production",
                "AXON_DYNAMODB_TABLE": selected_state_table_name,
                "AXON_ENABLED_PROVIDERS": "bedrock",
                "AXON_EVENT_OUTBOX_QUEUE_URL": (state.event_outbox_queue.queue_url),
                "AXON_EXPORT_BUCKET_NAME": (export_bucket_name.value_as_string),
                "AXON_EXPORT_QUEUE_URL": (export_queue_url.value_as_string),
                "AXON_LOAD_DEMO_DATA": "false",
                "AXON_OIDC_ISSUER": identity_oidc_issuer.value_as_string,
                "AXON_OIDC_PROJECT_CLAIM": (project_claim.value_as_string),
                "AXON_OIDC_TENANT_CLAIM": tenant_claim.value_as_string,
                "AXON_REQUIRE_CANONICAL_IDENTITY": "true",
                "AXON_ROUTING_CONFIG_SIGNING_KEY_ARN": (state.routing_config_signing_key.key_arn),
                "AXON_ROUTING_CONFIG_SIGNING_MODE": "sign-verify",
                "AXON_SAML_FEDERATION_MODE": "managed-cognito",
                "AXON_SAML_LOGIN_PATH": "/admin/dashboard",
                "AXON_SECURITY_EVENT_LOG_GROUP_ARN": (state.security_event_log_group_arn),
                "AXON_SECURITY_EVENT_SNS_TOPIC_ARN": (state.security_event_topic.topic_arn),
                "HOME": "/tmp",
                "LLM_ROUTER_DYNAMODB_ENABLED": "true",
                "AXON_SOURCE_REVISION": (source_revision.value_as_string),
            },
            function_name=function_name,
            handler="src.gateway.serverless_control.lambda_handler",
            log_group=application_logs,
            memory_size=1024,
            reserved_concurrent_executions=20,
            runtime=lambda_.Runtime.PYTHON_3_12,
            timeout=Duration.seconds(30),
            tracing=lambda_.Tracing.ACTIVE,
        )
        Tags.of(control_function).add(
            "AxonLLMArtifactSha256",
            control_code_sha256.value_as_string,
        )
        Tags.of(control_function).add(
            "AxonLLMSourceRevision",
            source_revision.value_as_string,
        )
        control_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadWriteCanonicalState",
                actions=_DYNAMODB_ACTIONS,
                resources=[
                    selected_state_table_arn,
                    f"{selected_state_table_arn}/index/*",
                ],
            )
        )
        state.data_key.grant_encrypt_decrypt(control_function)
        state.routing_config_signing_key.grant(
            control_function,
            "kms:GetPublicKey",
            "kms:Sign",
            "kms:Verify",
        )
        state.event_outbox_queue.grant_send_messages(control_function)
        control_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="CheckSecurityEventOutbox",
                actions=["sqs:GetQueueAttributes"],
                resources=[state.event_outbox_queue.queue_arn],
            )
        )
        control_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="QueueAndInspectExports",
                actions=[
                    "sqs:GetQueueAttributes",
                    "sqs:SendMessage",
                ],
                resources=[export_queue_arn.value_as_string],
            )
        )
        control_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="DownloadCompletedExports",
                actions=["s3:GetObject"],
                resources=[
                    Fn.join(
                        "",
                        [
                            "arn:",
                            self.partition,
                            ":s3:::",
                            export_bucket_name.value_as_string,
                            "/exports/*",
                        ],
                    )
                ],
            )
        )
        control_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="DiscoverDedicatedBrowserClient",
                actions=["cognito-idp:ListUserPoolClients"],
                resources=[
                    self.format_arn(
                        service="cognito-idp",
                        resource="userpool",
                        resource_name=identity_user_pool_id.value_as_string,
                    )
                ],
            )
        )

        scim_secret_arn = self.node.try_get_context("scim_tenants_secret_arn")
        if scim_secret_arn not in (None, ""):
            if not isinstance(scim_secret_arn, str) or _SECRET_ARN.fullmatch(scim_secret_arn) is None:
                raise ValueError("scim_tenants_secret_arn must be a complete Secrets Manager ARN")
            scim_secret = secretsmanager.Secret.from_secret_complete_arn(
                self,
                "ScimTenantsSecret",
                scim_secret_arn,
            )
            scim_secret.grant_read(control_function)
            control_function.add_environment(
                "AXON_SCIM_TENANTS_SECRET_ARN",
                scim_secret.secret_arn,
            )

        origin_credential = secretsmanager.Secret(
            self,
            "OriginCredential",
            description=("CloudFront-to-API-Gateway origin credential; not a user authentication token"),
            generate_secret_string=(
                secretsmanager.SecretStringGenerator(
                    exclude_punctuation=True,
                    password_length=48,
                )
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        control_api = apigateway.RestApi(
            self,
            "ControlApiGateway",
            cloud_watch_role=False,
            deploy_options=apigateway.StageOptions(
                metrics_enabled=True,
                throttling_burst_limit=100,
                throttling_rate_limit=50,
                tracing_enabled=True,
            ),
            description="Private-origin AxonLLM serverless control API",
            endpoint_types=[apigateway.EndpointType.REGIONAL],
            fail_on_warnings=True,
            rest_api_name=f"axonllm-control-api{physical_suffix}",
        )
        integration = apigateway.LambdaIntegration(
            control_function,
            allow_test_invoke=False,
            proxy=True,
        )
        method_options = apigateway.MethodOptions(
            api_key_required=True,
            authorization_type=apigateway.AuthorizationType.NONE,
        )
        control_api.root.add_method(
            "ANY",
            integration,
            api_key_required=True,
            authorization_type=apigateway.AuthorizationType.NONE,
        )
        control_api.root.add_proxy(
            any_method=True,
            default_integration=integration,
            default_method_options=method_options,
        )
        origin_api_key = control_api.add_api_key(
            "OriginApiKey",
            api_key_name=f"axonllm-cloudfront-origin{physical_suffix}",
            description=("Origin credential used only by the AxonLLM CloudFront distribution"),
            value=origin_credential.secret_value.to_string(),
        )
        usage_plan = control_api.add_usage_plan(
            "OriginUsagePlan",
            name=f"axonllm-cloudfront-origin{physical_suffix}",
            throttle=apigateway.ThrottleSettings(
                burst_limit=100,
                rate_limit=50,
            ),
        )
        usage_plan.add_api_key(origin_api_key)
        usage_plan.add_api_stage(stage=control_api.deployment_stage)
        api_origin = origins.RestApiOrigin(
            control_api,
            custom_headers={
                "x-api-key": origin_credential.secret_value.to_string(),
            },
        )
        static_origin = origins.S3BucketOrigin.with_origin_access_control(site_bucket)

        viewer_ip_set = wafv2.CfnIPSet(
            self,
            "ViewerIpSet",
            addresses=allowed_viewer_cidrs.value_as_list,
            description="Reviewed AxonLLM control-plane viewer networks",
            ip_address_version="IPV4",
            scope="CLOUDFRONT",
        )
        metric_suffix = physical_suffix.replace("-", "")
        web_acl = wafv2.CfnWebACL(
            self,
            "WebAcl",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(block={}),
            scope="CLOUDFRONT",
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=f"AxonLLMServerlessControl{metric_suffix}",
                sampled_requests_enabled=False,
            ),
            rules=[
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedCommonProtections",
                    priority=0,
                    override_action=(wafv2.CfnWebACL.OverrideActionProperty(none={})),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=(
                            wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                                name="AWSManagedRulesCommonRuleSet",
                                vendor_name="AWS",
                            )
                        )
                    ),
                    visibility_config=(
                        wafv2.CfnWebACL.VisibilityConfigProperty(
                            cloud_watch_metrics_enabled=True,
                            metric_name=(f"AxonLLMCommonProtections{metric_suffix}"),
                            sampled_requests_enabled=False,
                        )
                    ),
                ),
                wafv2.CfnWebACL.RuleProperty(
                    name="PerViewerRateLimit",
                    priority=1,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=(
                            wafv2.CfnWebACL.RateBasedStatementProperty(
                                aggregate_key_type="IP",
                                limit=2_000,
                            )
                        )
                    ),
                    visibility_config=(
                        wafv2.CfnWebACL.VisibilityConfigProperty(
                            cloud_watch_metrics_enabled=True,
                            metric_name=(f"AxonLLMViewerRate{metric_suffix}"),
                            sampled_requests_enabled=False,
                        )
                    ),
                ),
                wafv2.CfnWebACL.RuleProperty(
                    name="ReviewedViewerNetworks",
                    priority=2,
                    action=wafv2.CfnWebACL.RuleActionProperty(allow={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        ip_set_reference_statement=(
                            wafv2.CfnWebACL.IPSetReferenceStatementProperty(
                                arn=viewer_ip_set.attr_arn,
                            )
                        )
                    ),
                    visibility_config=(
                        wafv2.CfnWebACL.VisibilityConfigProperty(
                            cloud_watch_metrics_enabled=True,
                            metric_name=(f"AxonLLMAllowedViewers{metric_suffix}"),
                            sampled_requests_enabled=False,
                        )
                    ),
                ),
            ],
        )

        trusted_host = cloudfront.Function(
            self,
            "TrustedPublicHost",
            code=cloudfront.FunctionCode.from_inline(
                """function handler(event) {
    var request = event.request;
    var publicHost = request.headers.host.value;
    delete request.headers['x-api-key'];
    delete request.headers['x-axon-public-host'];
    request.headers['x-axon-public-host'] = { value: publicHost };
    return request;
}
"""
            ),
            comment=("Replace any viewer-supplied public-host header with the CloudFront viewer host"),
            runtime=cloudfront.FunctionRuntime.JS_2_0,
        )
        dashboard_rewrite = cloudfront.Function(
            self,
            "DashboardRewrite",
            code=cloudfront.FunctionCode.from_inline(
                """function handler(event) {
    var request = event.request;
    request.uri = '/index.html';
    return request;
}
"""
            ),
            comment="Serve the static dashboard shell at /admin/dashboard",
            runtime=cloudfront.FunctionRuntime.JS_2_0,
        )
        security_headers = cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeaders",
            security_headers_behavior=(
                cloudfront.ResponseSecurityHeadersBehavior(
                    content_security_policy=(
                        cloudfront.ResponseHeadersContentSecurityPolicy(
                            content_security_policy=(
                                "default-src 'self'; "
                                "base-uri 'none'; "
                                "connect-src 'self'; "
                                "font-src 'self'; "
                                "frame-ancestors 'self'; "
                                "frame-src 'self'; "
                                "img-src 'self' data:; "
                                "media-src 'self'; "
                                "object-src 'none'; "
                                "script-src 'self' 'unsafe-inline'; "
                                "style-src 'self' 'unsafe-inline'"
                            ),
                            override=True,
                        )
                    ),
                    content_type_options=(cloudfront.ResponseHeadersContentTypeOptions(override=True)),
                    frame_options=cloudfront.ResponseHeadersFrameOptions(
                        frame_option=cloudfront.HeadersFrameOption.SAMEORIGIN,
                        override=True,
                    ),
                    referrer_policy=(
                        cloudfront.ResponseHeadersReferrerPolicy(
                            referrer_policy=(cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN),
                            override=True,
                        )
                    ),
                    strict_transport_security=(
                        cloudfront.ResponseHeadersStrictTransportSecurity(
                            access_control_max_age=Duration.days(365),
                            include_subdomains=True,
                            override=True,
                            preload=True,
                        )
                    ),
                    xss_protection=(
                        cloudfront.ResponseHeadersXSSProtection(
                            mode_block=True,
                            override=True,
                            protection=True,
                        )
                    ),
                )
            ),
        )

        static_behavior = cloudfront.BehaviorOptions(
            origin=static_origin,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
            cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD_OPTIONS,
            cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            compress=True,
            response_headers_policy=security_headers,
            viewer_protocol_policy=(cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS),
        )
        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_behavior=static_behavior,
            default_root_object="index.html",
            enable_ipv6=False,
            enable_logging=True,
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            log_bucket=access_logs_bucket,
            log_file_prefix="cloudfront/",
            minimum_protocol_version=(cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021),
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            web_acl_id=web_acl.attr_arn,
        )
        for path_pattern in (
            "/admin/*",
            "/auth/*",
            "/health",
            "/ready",
            "/saml/*",
            "/scim/*",
        ):
            distribution.add_behavior(
                path_pattern,
                api_origin,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cached_methods=(cloudfront.CachedMethods.CACHE_GET_HEAD_OPTIONS),
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                compress=True,
                function_associations=[
                    cloudfront.FunctionAssociation(
                        event_type=(cloudfront.FunctionEventType.VIEWER_REQUEST),
                        function=trusted_host,
                    )
                ],
                origin_request_policy=(cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER),
                response_headers_policy=security_headers,
                viewer_protocol_policy=(cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS),
            )
        distribution.add_behavior(
            "/admin/static/*",
            static_origin,
            allowed_methods=(cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS),
            cached_methods=(cloudfront.CachedMethods.CACHE_GET_HEAD_OPTIONS),
            cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            compress=True,
            response_headers_policy=security_headers,
            viewer_protocol_policy=(cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS),
        )
        distribution.add_behavior(
            "/admin/dashboard",
            static_origin,
            allowed_methods=(cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS),
            cached_methods=(cloudfront.CachedMethods.CACHE_GET_HEAD_OPTIONS),
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            compress=True,
            function_associations=[
                cloudfront.FunctionAssociation(
                    event_type=(cloudfront.FunctionEventType.VIEWER_REQUEST),
                    function=dashboard_rewrite,
                )
            ],
            response_headers_policy=security_headers,
            viewer_protocol_policy=(cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS),
        )

        static_deployer_name = f"axonllm-static-assets{physical_suffix}"
        static_deployer_logs = logs.LogGroup(
            self,
            "StaticAssetDeployerLogs",
            encryption_key=state.data_key,
            log_group_name=f"/aws/lambda/{static_deployer_name}",
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=removal_policy,
        )
        static_deployer = lambda_.Function(
            self,
            "StaticAssetDeployer",
            architecture=lambda_.Architecture.ARM_64,
            code=lambda_.Code.from_inline(
                Path(__file__).with_name("static_asset_deployer.py").read_text(encoding="utf-8")
            ),
            ephemeral_storage_size=Size.mebibytes(1024),
            function_name=static_deployer_name,
            handler="index.handler",
            log_group=static_deployer_logs,
            memory_size=1024,
            runtime=lambda_.Runtime.PYTHON_3_12,
            timeout=Duration.minutes(15),
        )
        static_deployer.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadExactStaticArtifactVersion",
                actions=["s3:GetObjectVersion"],
                resources=[artifact_bucket.arn_for_objects(static_assets_key.value_as_string)],
            )
        )
        artifact_key.grant_decrypt(static_deployer)
        site_key.grant_encrypt_decrypt(static_deployer)
        static_deployer.add_to_role_policy(
            iam.PolicyStatement(
                sid="ListPrivateStaticSite",
                actions=["s3:ListBucket"],
                resources=[site_bucket.bucket_arn],
            )
        )
        static_deployer.add_to_role_policy(
            iam.PolicyStatement(
                sid="ManagePrivateStaticObjects",
                actions=["s3:DeleteObject", "s3:PutObject"],
                resources=[site_bucket.arn_for_objects("*")],
            )
        )
        static_deployer.add_to_role_policy(
            iam.PolicyStatement(
                sid="InvalidateExactDistribution",
                actions=["cloudfront:CreateInvalidation"],
                resources=[
                    distribution.distribution_arn,
                    *(
                        [production_distribution_arn.value_as_string]
                        if production_distribution_arn is not None
                        else []
                    ),
                ],
            )
        )
        static_deployment = CustomResource(
            self,
            "DeployStaticAssets",
            resource_type="Custom::AxonLLMStaticAssets",
            service_token=static_deployer.function_arn,
            properties={
                "DestinationBucket": site_bucket.bucket_name,
                "DistributionId": distribution.distribution_id,
                **(
                    {
                        "AdditionalDistributionId": (
                            production_distribution_id.value_as_string
                        )
                    }
                    if production_distribution_id is not None
                    else {}
                ),
                "RetainOnDelete": ("false" if deployment_namespace else "true"),
                "SourceRevision": source_revision.value_as_string,
                "SourceBucket": artifact_bucket.bucket_name,
                "SourceKey": static_assets_key.value_as_string,
                "SourceSha256": static_assets_sha256.value_as_string,
                "SourceVersion": static_assets_version.value_as_string,
            },
        )
        static_deployment.node.add_dependency(distribution)

        user_pool = cognito.UserPool.from_user_pool_id(
            self,
            "IdentityUserPool",
            identity_user_pool_id.value_as_string,
        )
        public_origin = Fn.join(
            "",
            ["https://", distribution.distribution_domain_name],
        )
        callback_urls = [
            Fn.join("", [public_origin, "/auth/callback"])
        ]
        logout_urls = [
            Fn.join("", [public_origin, "/auth/signed-out"])
        ]
        if production_control_plane_hostname is not None:
            callback_urls.append(
                Fn.join(
                    "",
                    [
                        "https://",
                        production_control_plane_hostname.value_as_string,
                        "/auth/callback",
                    ],
                )
            )
            logout_urls.append(
                Fn.join(
                    "",
                    [
                        "https://",
                        production_control_plane_hostname.value_as_string,
                        "/auth/signed-out",
                    ],
                )
            )
        browser_client = user_pool.add_client(
            "BrowserClient",
            access_token_validity=Duration.minutes(15),
            auth_flows=cognito.AuthFlow(),
            enable_token_revocation=True,
            generate_secret=False,
            id_token_validity=Duration.minutes(15),
            o_auth=cognito.OAuthSettings(
                callback_urls=callback_urls,
                flows=cognito.OAuthFlows(
                    authorization_code_grant=True,
                    client_credentials=False,
                    implicit_code_grant=False,
                ),
                logout_urls=logout_urls,
                scopes=[
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.PROFILE,
                ],
            ),
            prevent_user_existence_errors=True,
            read_attributes=(
                cognito.ClientAttributes()
                .with_standard_attributes(
                    email=True,
                    email_verified=True,
                )
                .with_custom_attributes("tenant_id", "project_id")
            ),
            refresh_token_rotation_grace_period=Duration.seconds(0),
            refresh_token_validity=Duration.hours(8),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
            user_pool_client_name=browser_client_name,
        )
        browser_client.apply_removal_policy(removal_policy)

        outputs = {
            "ApplicationStateStackName": state.stack_name,
            "BrowserClientId": browser_client.user_pool_client_id,
            "ControlApiArtifactSha256": (control_code_sha256.value_as_string),
            "ControlApiGatewayId": control_api.rest_api_id,
            "ControlApiFunctionArn": control_function.function_arn,
            "ControlPlaneAuthMode": "application-oidc",
            "ControlPlaneDomainName": (distribution.distribution_domain_name),
            "ControlPlaneUrl": public_origin,
            "DistributionId": distribution.distribution_id,
            "EndpointMode": "cloudfront",
            "PrimaryStateTableName": (primary_state_table.value_as_string),
            "SelectedRuntimeStateTableName": (selected_state_table_name),
            "StaticAssetsSha256": static_assets_sha256.value_as_string,
            "StaticAssetsObjectVersion": (static_assets_version.value_as_string),
            "SourceRevision": source_revision.value_as_string,
            "StaticSiteBucketName": site_bucket.bucket_name,
            "WebAclArn": web_acl.attr_arn,
        }
        if production_distribution_arn is not None:
            outputs.update(
                {
                    "ControlApiOriginDomainName": Fn.join(
                        "",
                        [
                            control_api.rest_api_id,
                            ".execute-api.",
                            self.region,
                            ".",
                            self.url_suffix,
                        ],
                    ),
                    "ControlApiOriginPath": Fn.join(
                        "",
                        [
                            "/",
                            control_api.deployment_stage.stage_name,
                        ],
                    ),
                    "OriginCredentialSecretArn": (
                        origin_credential.secret_arn
                    ),
                    "ProductionControlPlaneHostname": (
                        production_control_plane_hostname.value_as_string
                    ),
                    "ProductionDistributionArn": (
                        production_distribution_arn.value_as_string
                    ),
                    "ProductionDistributionId": (
                        production_distribution_id.value_as_string
                    ),
                    "StaticSiteBucketRegionalDomainName": (
                        site_bucket.bucket_regional_domain_name
                    ),
                }
            )
        for name, value in outputs.items():
            output = CfnOutput(
                self,
                f"{name}Output",
                value=value,
            )
            output.override_logical_id(name)


__all__ = ["AxonLLMServerlessControlPlaneStack"]
