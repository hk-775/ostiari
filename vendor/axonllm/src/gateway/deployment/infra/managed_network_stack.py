"""Disposable, explicitly selected networking for AgentCore Runtime."""

from __future__ import annotations

from ipaddress import ip_network
import json
from pathlib import Path
import re

from aws_cdk import (
    CfnOutput,
    CfnParameter,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    Token,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_logs as logs,
    custom_resources as cr,
)
from constructs import Construct

if __package__:
    from .agentcore_stack import (
        ATHENA_ASSUME_ROLE_ACTIONS,
        ATHENA_QUERY_ACTIONS,
        load_athena_infrastructure_config,
    )
else:
    from agentcore_stack import (
        ATHENA_ASSUME_ROLE_ACTIONS,
        ATHENA_QUERY_ACTIONS,
        load_athena_infrastructure_config,
    )


_SUPPORTED_AZS_FILE = "agentcore-supported-availability-zones-v1.json"
_NETWORK_EGRESS_MODES = frozenset({"endpoints-only", "managed-nat"})


class AxonLLMManagedNetworkStack(Stack):
    """Optional VPC and egress owned by AxonLLM, separate from runtime."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        deployment_namespace: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        profile = _context_string(self, "deployment_profile")
        if profile not in {"development", "production"}:
            raise ValueError("deployment_profile must be 'development' or 'production'")
        query_config = load_athena_infrastructure_config(self)
        egress_mode = _context_string(
            self,
            "managed_network_egress_mode",
        )
        if egress_mode not in _NETWORK_EGRESS_MODES:
            raise ValueError("managed_network_egress_mode must be 'endpoints-only' or 'managed-nat'")
        vpc_cidr = _canonical_ipv4_cidr(_context_string(self, "managed_network_vpc_cidr"))
        availability_zones = _context_string_list(
            self,
            "managed_network_availability_zones",
            minimum=2,
        )
        availability_zone_ids = _context_string_list(
            self,
            "managed_network_availability_zone_ids",
            minimum=2,
        )
        _validate_availability_zones(
            region=self.region,
            names=availability_zones,
            zone_ids=availability_zone_ids,
        )

        nat_gateway_count = 0
        approved_https_prefix_list_id = None
        if egress_mode == "managed-nat":
            acknowledgement = self.node.try_get_context("managed_network_cost_acknowledgement")
            if acknowledgement is not True:
                raise ValueError("managed-nat requires managed_network_cost_acknowledgement=true")
            nat_gateway_count = _context_integer(
                self,
                "managed_network_nat_gateway_count",
                allowed={1, 2},
            )
            if nat_gateway_count > len(availability_zones):
                raise ValueError(
                    "managed_network_nat_gateway_count cannot exceed the number of selected Availability Zones"
                )
            approved_https_prefix_list_id = CfnParameter(
                self,
                "ApprovedHttpsPrefixListId",
                type="String",
                allowed_pattern=r"^pl-[0-9a-fA-F]+$",
                constraint_description=("must be an EC2 managed prefix list ID"),
                description=("Approved external HTTPS destinations reachable through the managed NAT gateways"),
            )

        selected_state_table_name = CfnParameter(
            self,
            "SelectedStateTableName",
            type="String",
            min_length=3,
            max_length=255,
            allowed_pattern=r"^[A-Za-z0-9_.-]{3,255}$",
            description=("Canonical DynamoDB table from the application-state descriptor"),
        )
        data_key_arn = _arn_parameter(
            self,
            "ApplicationStateDataKeyArn",
            service="kms",
            resource=r"key/[0-9a-fA-F-]{36}",
        )
        routing_key_arn = _arn_parameter(
            self,
            "ApplicationStateRoutingConfigSigningKeyArn",
            service="kms",
            resource=r"key/[0-9a-fA-F-]{36}",
        )
        provider_secret_arn = _arn_parameter(
            self,
            "ApplicationStateProviderSecretArn",
            service="secretsmanager",
            resource=r"secret:[A-Za-z0-9/_+=.@-]+",
        )
        outbox_queue_arn = _arn_parameter(
            self,
            "ApplicationStateSecurityEventOutboxQueueArn",
            service="sqs",
            resource=r"[A-Za-z0-9_-]{1,80}\.fifo",
        )
        security_event_topic_arn = _arn_parameter(
            self,
            "ApplicationStateSecurityEventTopicArn",
            service="sns",
            resource=r"[A-Za-z0-9_-]{1,256}\.fifo",
        )
        security_event_log_group_arn = _arn_parameter(
            self,
            "ApplicationStateSecurityEventLogGroupArn",
            service="logs",
            resource=r"log-group:[A-Za-z0-9_./#-]+",
        )
        bedrock_invoke_resource_arns = CfnParameter(
            self,
            "BedrockInvokeResourceArns",
            type="CommaDelimitedList",
            allowed_pattern=(
                r"^arn:[a-z0-9-]+:bedrock:[a-z0-9-]+:"
                r"(?:[0-9]{12})?:(?:foundation-model|inference-profile|"
                r"application-inference-profile|custom-model|"
                r"provisioned-model|imported-model)/"
                r"[A-Za-z0-9][A-Za-z0-9._:/+-]*$"
            ),
            description=("Exact Bedrock model and inference-profile resources reachable through the private endpoint"),
        )
        verified_image_uri = CfnParameter(
            self,
            "VerifiedImageUri",
            type="String",
            allowed_pattern=(
                rf"^[0-9]{{12}}\.dkr\.ecr\.{self.region}"
                r"\.amazonaws\.com/[a-z0-9]+(?:[._/-][a-z0-9]+)*@"
                r"sha256:[0-9a-f]{64}$"
            ),
            description=("Immutable private ECR image verified for AgentCore"),
        )
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
        state_table_arn = self.format_arn(
            service="dynamodb",
            resource="table",
            resource_name=selected_state_table_name.value_as_string,
        )
        rehearsal_control_table_arn = (
            _arn_parameter(
                self,
                "RehearsalControlTableArn",
                service="dynamodb",
                resource=r"table/axonllm-rehearsal-control-ledger",
            )
            if deployment_namespace
            else None
        )

        runtime_subnet_type = (
            ec2.SubnetType.PRIVATE_ISOLATED if egress_mode == "endpoints-only" else ec2.SubnetType.PRIVATE_WITH_EGRESS
        )
        subnet_configuration = [
            ec2.SubnetConfiguration(
                name="Runtime",
                subnet_type=runtime_subnet_type,
                cidr_mask=24,
            )
        ]
        if egress_mode == "managed-nat":
            subnet_configuration.insert(
                0,
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                    map_public_ip_on_launch=False,
                ),
            )
        vpc = ec2.Vpc(
            self,
            "Vpc",
            ip_addresses=ec2.IpAddresses.cidr(vpc_cidr),
            availability_zones=list(availability_zones),
            nat_gateways=nat_gateway_count,
            restrict_default_security_group=True,
            subnet_configuration=subnet_configuration,
        )
        runtime_subnets = ec2.SubnetSelection(subnet_type=runtime_subnet_type)
        runtime_security_group = ec2.SecurityGroup(
            self,
            "RuntimeSecurityGroup",
            vpc=vpc,
            allow_all_outbound=False,
            description=("AxonLLM AgentCore managed-network approved egress"),
        )
        runtime_security_group.add_egress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.udp(53),
            "DNS to the VPC resolver",
        )
        runtime_security_group.add_egress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(53),
            "DNS fallback to the VPC resolver",
        )
        if approved_https_prefix_list_id is not None:
            runtime_security_group.add_egress_rule(
                ec2.Peer.prefix_list(approved_https_prefix_list_id.value_as_string),
                ec2.Port.tcp(443),
                "HTTPS to explicitly approved external destinations",
            )

        endpoint_security_group = ec2.SecurityGroup(
            self,
            "EndpointSecurityGroup",
            vpc=vpc,
            allow_all_outbound=False,
            description=("Private AWS service endpoints for AxonLLM AgentCore"),
        )
        endpoint_security_group.add_ingress_rule(
            runtime_security_group,
            ec2.Port.tcp(443),
            "HTTPS from the AgentCore runtime and routing seeder",
        )
        runtime_security_group.add_egress_rule(
            endpoint_security_group,
            ec2.Port.tcp(443),
            "AWS services through private interface endpoints",
        )

        dynamodb_endpoint = vpc.add_gateway_endpoint(
            "DynamoDbEndpoint",
            service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
            subnets=[runtime_subnets],
        )
        state_table_resources = [
            state_table_arn,
            f"{state_table_arn}/index/*",
        ]
        if rehearsal_control_table_arn is not None:
            state_table_resources.append(rehearsal_control_table_arn.value_as_string)
        dynamodb_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=[
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
                ],
                resources=state_table_resources,
            )
        )
        s3_endpoint = vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
            subnets=[runtime_subnets],
        )
        s3_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=["s3:GetObject"],
                resources=[(f"arn:{self.partition}:s3:::prod-{self.region}-starport-layer-bucket/*")],
            )
        )
        for service_name in ("dynamodb", "s3"):
            prefix_list_id = _managed_prefix_list_id(
                self,
                service_name=service_name,
            )
            runtime_security_group.add_egress_rule(
                ec2.Peer.prefix_list(prefix_list_id),
                ec2.Port.tcp(443),
                (f"{service_name.upper()} through the VPC gateway endpoint"),
            )

        def interface_endpoint(
            construct_id: str,
            service: ec2.InterfaceVpcEndpointService,
        ) -> ec2.InterfaceVpcEndpoint:
            return vpc.add_interface_endpoint(
                construct_id,
                service=service,
                open=False,
                private_dns_enabled=True,
                security_groups=[endpoint_security_group],
                subnets=runtime_subnets,
            )

        ecr_api_endpoint = interface_endpoint(
            "EcrApiEndpoint",
            ec2.InterfaceVpcEndpointAwsService.ECR,
        )
        ecr_docker_endpoint = interface_endpoint(
            "EcrDockerEndpoint",
            ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
        )
        ecr_repository_statement = iam.PolicyStatement(
            principals=[iam.AnyPrincipal()],
            actions=[
                "ecr:BatchCheckLayerAvailability",
                "ecr:BatchGetImage",
                "ecr:GetDownloadUrlForLayer",
            ],
            resources=[image_repository_arn],
        )
        for endpoint in (ecr_api_endpoint, ecr_docker_endpoint):
            endpoint.add_to_policy(ecr_repository_statement)
            endpoint.add_to_policy(
                iam.PolicyStatement(
                    principals=[iam.AnyPrincipal()],
                    actions=["ecr:GetAuthorizationToken"],
                    resources=["*"],
                )
            )

        bedrock_endpoint = interface_endpoint(
            "BedrockRuntimeEndpoint",
            ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
        )
        bedrock_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=bedrock_invoke_resource_arns.value_as_list,
            )
        )
        kms_endpoint = interface_endpoint(
            "KmsEndpoint",
            ec2.InterfaceVpcEndpointAwsService.KMS,
        )
        kms_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=["kms:Sign", "kms:Verify"],
                resources=[routing_key_arn.value_as_string],
            )
        )
        kms_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=[
                    "kms:Decrypt",
                    "kms:GenerateDataKey",
                    "kms:GenerateDataKeyWithoutPlaintext",
                ],
                resources=[data_key_arn.value_as_string],
            )
        )
        secrets_endpoint = interface_endpoint(
            "SecretsManagerEndpoint",
            ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
        )
        secrets_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=[
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:GetSecretValue",
                ],
                resources=[provider_secret_arn.value_as_string],
            )
        )
        sqs_endpoint = interface_endpoint(
            "SqsEndpoint",
            ec2.InterfaceVpcEndpointAwsService.SQS,
        )
        sqs_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=[
                    "sqs:ChangeMessageVisibility",
                    "sqs:DeleteMessage",
                    "sqs:GetQueueAttributes",
                    "sqs:ReceiveMessage",
                    "sqs:SendMessage",
                ],
                resources=[outbox_queue_arn.value_as_string],
            )
        )
        sns_endpoint = interface_endpoint(
            "SnsEndpoint",
            ec2.InterfaceVpcEndpointAwsService.SNS,
        )
        sns_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=["sns:Publish"],
                resources=[security_event_topic_arn.value_as_string],
            )
        )
        logs_endpoint = interface_endpoint(
            "CloudWatchLogsEndpoint",
            ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
        )
        logs_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:DescribeLogStreams",
                    "logs:PutLogEvents",
                ],
                resources=[
                    security_event_log_group_arn.value_as_string,
                    f"{security_event_log_group_arn.value_as_string}:*",
                    self.format_arn(
                        service="logs",
                        resource="log-group",
                        resource_name="/aws/bedrock-agentcore/*",
                    ),
                ],
            )
        )
        interface_endpoint(
            "CognitoIdpEndpoint",
            ec2.InterfaceVpcEndpointAwsService.COGNITO_IDP,
        )
        if query_config.enabled:
            sts_endpoint = interface_endpoint(
                "StsEndpoint",
                ec2.InterfaceVpcEndpointAwsService.STS,
            )
            sts_endpoint.add_to_policy(
                iam.PolicyStatement(
                    principals=[iam.AnyPrincipal()],
                    actions=ATHENA_ASSUME_ROLE_ACTIONS,
                    resources=list(query_config.role_arns),
                )
            )
            athena_endpoint = interface_endpoint(
                "AthenaEndpoint",
                ec2.InterfaceVpcEndpointAwsService.ATHENA,
            )
            athena_endpoint.add_to_policy(
                iam.PolicyStatement(
                    principals=[iam.ArnPrincipal(role_arn) for role_arn in query_config.role_arns],
                    actions=ATHENA_QUERY_ACTIONS,
                    resources=["*"],
                )
            )

        CfnOutput(
            self,
            "ManagedNetworkStackName",
            value=self.stack_name,
        )
        CfnOutput(
            self,
            "VpcId",
            value=vpc.vpc_id,
        )
        CfnOutput(
            self,
            "VpcCidr",
            value=vpc.vpc_cidr_block,
        )
        CfnOutput(
            self,
            "PrivateSubnetIds",
            value=Fn.join(
                ",",
                vpc.select_subnets(subnet_type=runtime_subnet_type).subnet_ids,
            ),
        )
        CfnOutput(
            self,
            "AvailabilityZones",
            value=Fn.join(",", list(availability_zones)),
        )
        CfnOutput(
            self,
            "AvailabilityZoneIds",
            value=Fn.join(",", list(availability_zone_ids)),
        )
        CfnOutput(
            self,
            "RuntimeSecurityGroupIds",
            value=runtime_security_group.security_group_id,
        )
        CfnOutput(
            self,
            "EgressMode",
            value=egress_mode,
        )
        CfnOutput(
            self,
            "NatGatewayCount",
            value=str(nat_gateway_count),
        )
        CfnOutput(
            self,
            "DeploymentNamespace",
            value=deployment_namespace or "production",
        )


def _managed_prefix_list_id(
    scope: Construct,
    *,
    service_name: str,
) -> str:
    lookup_logs = logs.LogGroup(
        scope,
        f"{service_name.title()}PrefixLookupLogs",
        retention=logs.RetentionDays.ONE_WEEK,
        removal_policy=RemovalPolicy.DESTROY,
    )
    lookup = cr.AwsCustomResource(
        scope,
        f"{service_name.title()}PrefixList",
        on_create=cr.AwsSdkCall(
            service="EC2",
            action="describeManagedPrefixLists",
            parameters={
                "Filters": [
                    {
                        "Name": "prefix-list-name",
                        "Values": [
                            (f"com.amazonaws.{scope.node.try_get_context('region') or 'us-east-1'}.{service_name}")
                        ],
                    }
                ]
            },
            output_paths=["PrefixLists.0.PrefixListId"],
            physical_resource_id=cr.PhysicalResourceId.from_response("PrefixLists.0.PrefixListId"),
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


def _arn_parameter(
    scope: Construct,
    construct_id: str,
    *,
    service: str,
    resource: str,
) -> CfnParameter:
    stack = Stack.of(scope)
    account = r"[0-9]{12}" if Token.is_unresolved(stack.account) else re.escape(stack.account)
    return CfnParameter(
        scope,
        construct_id,
        type="String",
        allowed_pattern=(
            rf"^arn:{re.escape(stack.partition)}:{re.escape(service)}:"
            rf"{re.escape(stack.region)}:{account}:{resource}$"
        ),
        constraint_description=(f"must be an exact {service} ARN in this account and region"),
    )


def _canonical_ipv4_cidr(value: str) -> str:
    try:
        network = ip_network(value, strict=True)
    except ValueError as exc:
        raise ValueError("managed_network_vpc_cidr must be a canonical IPv4 CIDR") from exc
    if network.version != 4:
        raise ValueError("managed_network_vpc_cidr must be a canonical IPv4 CIDR")
    return str(network)


def _validate_availability_zones(
    *,
    region: str,
    names: tuple[str, ...],
    zone_ids: tuple[str, ...],
) -> None:
    if len(names) != len(zone_ids):
        raise ValueError("managed network Availability Zone names and IDs must have equal lengths")
    if any(re.fullmatch(rf"{re.escape(region)}[a-z]", name) is None for name in names):
        raise ValueError(f"managed_network_availability_zones contains an invalid {region} Availability Zone name")
    document = json.loads(Path(__file__).with_name(_SUPPORTED_AZS_FILE).read_text(encoding="utf-8"))
    supported = document["regions"].get(region)
    if not isinstance(supported, list):
        raise ValueError(f"AgentCore VPC networking is not supported in {region}")
    unsupported = sorted(set(zone_ids).difference(supported))
    if unsupported:
        raise ValueError(
            f"managed network contains unsupported AgentCore Availability Zone IDs: {', '.join(unsupported)}"
        )


def _context_string(scope: Construct, name: str) -> str:
    value = scope.node.try_get_context(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _context_string_list(
    scope: Construct,
    name: str,
    *,
    minimum: int,
) -> tuple[str, ...]:
    value = scope.node.try_get_context(name)
    if (
        not isinstance(value, list)
        or len(value) < minimum
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"{name} must be a unique string list with at least {minimum} items")
    return tuple(value)


def _context_integer(
    scope: Construct,
    name: str,
    *,
    allowed: set[int],
) -> int:
    value = scope.node.try_get_context(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value not in allowed:
        allowed_values = ", ".join(str(item) for item in sorted(allowed))
        raise ValueError(f"{name} must be one of: {allowed_values}")
    return value


__all__ = ["AxonLLMManagedNetworkStack"]
