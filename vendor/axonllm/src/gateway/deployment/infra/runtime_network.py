"""AgentCore runtime networking without implicit AWS lookups."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_network
import re
from typing import Any, Literal

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import custom_resources as cr
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct


RuntimeNetworkMode = Literal["legacy", "existing", "managed", "public"]
RuntimeEgressMode = Literal[
    "endpoints-only",
    "existing-egress",
    "managed-nat",
]

_NETWORK_MODES = frozenset({"legacy", "existing", "managed", "public"})
_EGRESS_MODES = frozenset({"endpoints-only", "existing-egress", "managed-nat"})
_VPC_ID_PATTERN = re.compile(r"^vpc-[0-9a-f]{8,17}$")
_SUBNET_ID_PATTERN = re.compile(r"^subnet-[0-9a-f]{8,17}$")
_SECURITY_GROUP_ID_PATTERN = re.compile(r"^sg-[0-9a-f]{8,17}$")


def runtime_network_mode(scope: Construct) -> RuntimeNetworkMode:
    """Return the explicitly selected runtime network mode."""

    raw = scope.node.try_get_context("runtime_network_mode")
    if raw is None:
        return "legacy"
    if not isinstance(raw, str) or raw not in _NETWORK_MODES:
        raise ValueError("runtime_network_mode must be 'legacy', 'existing', 'managed', or 'public'")
    return raw  # type: ignore[return-value]


def runtime_network_requires_prefix_list(scope: Construct) -> bool:
    """Return whether this stack owns an HTTPS prefix-list egress rule."""

    mode = runtime_network_mode(scope)
    if mode == "legacy":
        return True
    if mode not in {"existing", "managed"}:
        return False
    security_group_ids = _context_string_list(
        scope,
        "runtime_network_security_group_ids",
        minimum=0,
    )
    return not security_group_ids and _runtime_egress_mode(scope) == "existing-egress"


@dataclass(frozen=True)
class RuntimeNetworkResources:
    """Network resources and bindings consumed by the runtime stack."""

    mode: RuntimeNetworkMode
    configuration: agentcore.RuntimeNetworkConfiguration
    vpc: ec2.IVpc | None = None
    subnets: tuple[ec2.ISubnet, ...] = ()
    runtime_security_groups: tuple[ec2.ISecurityGroup, ...] = ()
    endpoint_security_group: ec2.ISecurityGroup | None = None
    dynamodb_endpoint: ec2.GatewayVpcEndpoint | None = None
    bedrock_endpoint: ec2.InterfaceVpcEndpoint | None = None
    kms_endpoint: ec2.InterfaceVpcEndpoint | None = None
    athena_endpoint: ec2.InterfaceVpcEndpoint | None = None
    sts_endpoint: ec2.InterfaceVpcEndpoint | None = None
    secrets_endpoint: ec2.InterfaceVpcEndpoint | None = None
    sqs_endpoint: ec2.InterfaceVpcEndpoint | None = None
    sns_endpoint: ec2.InterfaceVpcEndpoint | None = None
    logs_endpoint: ec2.InterfaceVpcEndpoint | None = None
    dynamodb_prefix_list_id: str | None = None

    @property
    def owns_service_endpoints(self) -> bool:
        """Return whether this stack owns endpoint policy resources."""

        return self.mode == "legacy"

    def add_endpoint_policy(
        self,
        endpoint_name: str,
        statement: iam.PolicyStatement,
    ) -> None:
        """Add a policy only to an endpoint owned by this stack."""

        endpoint = getattr(self, endpoint_name)
        if endpoint is not None:
            endpoint.add_to_policy(statement)

    def routing_seeder_lambda_options(
        self,
        scope: Construct,
    ) -> dict[str, Any]:
        """Return network options for the one-shot routing seeder."""

        if self.vpc is None:
            return {}
        if self.mode != "legacy":
            return {
                "vpc": self.vpc,
                "vpc_subnets": ec2.SubnetSelection(subnets=list(self.subnets)),
                "security_groups": list(self.runtime_security_groups),
            }
        if self.endpoint_security_group is None or self.dynamodb_prefix_list_id is None:
            raise RuntimeError("legacy routing seeder network is incomplete")

        seeder_security_group = ec2.SecurityGroup(
            scope,
            "RoutingConfigSeederSecurityGroup",
            vpc=self.vpc,
            allow_all_outbound=False,
            description=("One-shot signed routing configuration bootstrap egress"),
        )
        seeder_security_group.add_egress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.udp(53),
            "DNS to the VPC resolver",
        )
        seeder_security_group.add_egress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.tcp(53),
            "DNS fallback to the VPC resolver",
        )
        seeder_security_group.add_egress_rule(
            ec2.Peer.prefix_list(self.dynamodb_prefix_list_id),
            ec2.Port.tcp(443),
            "DynamoDB through the VPC gateway endpoint",
        )
        self.endpoint_security_group.add_ingress_rule(
            seeder_security_group,
            ec2.Port.tcp(443),
            "HTTPS from the routing configuration seeder",
        )
        seeder_security_group.add_egress_rule(
            self.endpoint_security_group,
            ec2.Port.tcp(443),
            "KMS through the private interface endpoint",
        )
        return {
            "vpc": self.vpc,
            "vpc_subnets": ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            "security_groups": [seeder_security_group],
        }

    def add_routing_seeder_dependencies(
        self,
        resource: Any,
    ) -> None:
        """Order the seeder after stack-owned DynamoDB and KMS endpoints."""

        for endpoint in (self.dynamodb_endpoint, self.kms_endpoint):
            if endpoint is None:
                continue
            endpoint_resource = endpoint.node.default_child
            if not isinstance(endpoint_resource, ec2.CfnVPCEndpoint):
                raise TypeError("routing seeder endpoint is malformed")
            resource.add_dependency(endpoint_resource)


def build_runtime_network(
    scope: Construct,
    *,
    approved_https_prefix_list_id: str | None,
    query_enabled: bool,
) -> RuntimeNetworkResources:
    """Build or bind the selected AgentCore runtime network."""

    mode = runtime_network_mode(scope)
    if mode == "legacy":
        if approved_https_prefix_list_id is None:
            raise ValueError("legacy runtime networking requires an approved HTTPS prefix list")
        return _build_legacy_network(
            scope,
            approved_https_prefix_list_id=(approved_https_prefix_list_id),
            query_enabled=query_enabled,
        )
    if mode == "public":
        _validate_public_mode(scope)
        return RuntimeNetworkResources(
            mode=mode,
            configuration=(agentcore.RuntimeNetworkConfiguration.using_public_network()),
        )
    return _bind_external_vpc(
        scope,
        mode=mode,
        approved_https_prefix_list_id=approved_https_prefix_list_id,
    )


def _build_legacy_network(
    scope: Construct,
    *,
    approved_https_prefix_list_id: str,
    query_enabled: bool,
) -> RuntimeNetworkResources:
    vpc = ec2.Vpc(
        scope,
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
                name="Runtime",
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                cidr_mask=24,
            ),
        ],
    )
    runtime_security_group = ec2.SecurityGroup(
        scope,
        "RuntimeSecurityGroup",
        vpc=vpc,
        allow_all_outbound=False,
        description="AxonLLM AgentCore explicitly approved egress",
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
    runtime_security_group.add_egress_rule(
        ec2.Peer.prefix_list(approved_https_prefix_list_id),
        ec2.Port.tcp(443),
        "HTTPS to explicitly approved external destinations",
    )

    subnet_selection = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)
    dynamodb_endpoint = vpc.add_gateway_endpoint(
        "DynamoDbEndpoint",
        service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
        subnets=[subnet_selection],
    )
    prefix_lookup_logs = logs.LogGroup(
        scope,
        "PrefixLookupLogs",
        retention=logs.RetentionDays.ONE_WEEK,
        removal_policy=RemovalPolicy.DESTROY,
    )
    dynamodb_prefix_list = cr.AwsCustomResource(
        scope,
        "DynamoDbPrefixList",
        on_create=cr.AwsSdkCall(
            service="EC2",
            action="describeManagedPrefixLists",
            parameters={
                "Filters": [
                    {
                        "Name": "prefix-list-name",
                        "Values": [f"com.amazonaws.{scope.node.try_get_context('region') or 'us-east-1'}.dynamodb"],
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
        log_group=prefix_lookup_logs,
        timeout=Duration.seconds(30),
    )
    dynamodb_prefix_list_id = dynamodb_prefix_list.get_response_field("PrefixLists.0.PrefixListId")
    runtime_security_group.add_egress_rule(
        ec2.Peer.prefix_list(dynamodb_prefix_list_id),
        ec2.Port.tcp(443),
        "DynamoDB through the VPC gateway endpoint",
    )

    endpoint_security_group = ec2.SecurityGroup(
        scope,
        "EndpointSecurityGroup",
        vpc=vpc,
        allow_all_outbound=False,
        description="Private AWS service endpoints for AgentCore",
    )
    endpoint_security_group.add_ingress_rule(
        runtime_security_group,
        ec2.Port.tcp(443),
        "HTTPS from the AgentCore runtime",
    )
    runtime_security_group.add_egress_rule(
        endpoint_security_group,
        ec2.Port.tcp(443),
        "AWS services through private interface endpoints",
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
            subnets=subnet_selection,
        )

    bedrock_endpoint = interface_endpoint(
        "BedrockRuntimeEndpoint",
        ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
    )
    kms_endpoint = interface_endpoint(
        "KmsEndpoint",
        ec2.InterfaceVpcEndpointAwsService.KMS,
    )
    athena_endpoint = (
        interface_endpoint(
            "AthenaEndpoint",
            ec2.InterfaceVpcEndpointAwsService.ATHENA,
        )
        if query_enabled
        else None
    )
    sts_endpoint = (
        interface_endpoint(
            "StsEndpoint",
            ec2.InterfaceVpcEndpointAwsService.STS,
        )
        if query_enabled
        else None
    )
    secrets_endpoint = interface_endpoint(
        "SecretsManagerEndpoint",
        ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
    )
    sqs_endpoint = interface_endpoint(
        "SqsEndpoint",
        ec2.InterfaceVpcEndpointAwsService.SQS,
    )
    sns_endpoint = interface_endpoint(
        "SnsEndpoint",
        ec2.InterfaceVpcEndpointAwsService.SNS,
    )
    logs_endpoint = interface_endpoint(
        "CloudWatchLogsEndpoint",
        ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
    )
    return RuntimeNetworkResources(
        mode="legacy",
        configuration=agentcore.RuntimeNetworkConfiguration.using_vpc(
            scope,
            vpc=vpc,
            security_groups=[runtime_security_group],
            vpc_subnets=subnet_selection,
        ),
        vpc=vpc,
        subnets=tuple(vpc.private_subnets),
        runtime_security_groups=(runtime_security_group,),
        endpoint_security_group=endpoint_security_group,
        dynamodb_endpoint=dynamodb_endpoint,
        bedrock_endpoint=bedrock_endpoint,
        kms_endpoint=kms_endpoint,
        athena_endpoint=athena_endpoint,
        sts_endpoint=sts_endpoint,
        secrets_endpoint=secrets_endpoint,
        sqs_endpoint=sqs_endpoint,
        sns_endpoint=sns_endpoint,
        logs_endpoint=logs_endpoint,
        dynamodb_prefix_list_id=dynamodb_prefix_list_id,
    )


def _bind_external_vpc(
    scope: Construct,
    *,
    mode: Literal["existing", "managed"],
    approved_https_prefix_list_id: str | None,
) -> RuntimeNetworkResources:
    vpc_id = _context_string(scope, "runtime_network_vpc_id")
    vpc_cidr = _context_string(scope, "runtime_network_vpc_cidr")
    subnet_ids = _context_string_list(
        scope,
        "runtime_network_private_subnet_ids",
        minimum=2,
    )
    availability_zones = _context_string_list(
        scope,
        "runtime_network_availability_zones",
        minimum=2,
    )
    if len(subnet_ids) != len(availability_zones):
        raise ValueError(
            "runtime_network_private_subnet_ids and runtime_network_availability_zones must have equal lengths"
        )
    security_group_ids = _context_string_list(
        scope,
        "runtime_network_security_group_ids",
        minimum=0,
    )
    egress_mode = _runtime_egress_mode(scope)
    if _VPC_ID_PATTERN.fullmatch(vpc_id) is None:
        raise ValueError("runtime_network_vpc_id is not a valid VPC ID")
    try:
        parsed_vpc_cidr = ip_network(vpc_cidr, strict=True)
    except ValueError as exc:
        raise ValueError("runtime_network_vpc_cidr must be a canonical IPv4 CIDR") from exc
    if parsed_vpc_cidr.version != 4:
        raise ValueError("runtime_network_vpc_cidr must be a canonical IPv4 CIDR")
    if any(_SUBNET_ID_PATTERN.fullmatch(subnet_id) is None for subnet_id in subnet_ids):
        raise ValueError("runtime_network_private_subnet_ids contains an invalid subnet ID")
    if any(_SECURITY_GROUP_ID_PATTERN.fullmatch(security_group_id) is None for security_group_id in security_group_ids):
        raise ValueError("runtime_network_security_group_ids contains an invalid security group ID")
    region = scope.node.try_get_context("region") or "us-east-1"
    if any(re.fullmatch(rf"{re.escape(region)}[a-z]", zone) is None for zone in availability_zones):
        raise ValueError(f"runtime_network_availability_zones must contain standard {region} Availability Zone names")
    if mode == "existing" and egress_mode == "managed-nat":
        raise ValueError("existing runtime networking cannot use managed-nat egress")
    if mode == "managed" and egress_mode == "existing-egress":
        raise ValueError("managed runtime networking cannot use existing-egress")
    if mode == "managed" and not security_group_ids:
        raise ValueError("managed runtime networking requires security group IDs from AxonLLMManagedNetworkStack")

    vpc = ec2.Vpc.from_vpc_attributes(
        scope,
        "ExistingVpc",
        vpc_id=vpc_id,
        vpc_cidr_block=vpc_cidr,
        availability_zones=list(availability_zones),
        private_subnet_ids=list(subnet_ids),
        region=region,
    )
    subnets = tuple(
        ec2.Subnet.from_subnet_attributes(
            scope,
            f"ExistingPrivateSubnet{index}",
            subnet_id=subnet_id,
            availability_zone=availability_zone,
        )
        for index, (subnet_id, availability_zone) in enumerate(
            zip(subnet_ids, availability_zones, strict=True),
            start=1,
        )
    )
    security_groups = tuple(
        ec2.SecurityGroup.from_security_group_id(
            scope,
            f"ExistingRuntimeSecurityGroup{index}",
            security_group_id,
            allow_all_outbound=False,
            mutable=False,
        )
        for index, security_group_id in enumerate(
            security_group_ids,
            start=1,
        )
    )
    if not security_groups:
        runtime_security_group = ec2.SecurityGroup(
            scope,
            "RuntimeSecurityGroup",
            vpc=vpc,
            allow_all_outbound=False,
            description="AxonLLM AgentCore explicitly approved egress",
        )
        runtime_security_group.add_egress_rule(
            ec2.Peer.ipv4(vpc_cidr),
            ec2.Port.udp(53),
            "DNS to the VPC resolver",
        )
        runtime_security_group.add_egress_rule(
            ec2.Peer.ipv4(vpc_cidr),
            ec2.Port.tcp(53),
            "DNS fallback to the VPC resolver",
        )
        runtime_security_group.add_egress_rule(
            ec2.Peer.ipv4(vpc_cidr),
            ec2.Port.tcp(443),
            "HTTPS to customer-owned private endpoints and proxies",
        )
        if egress_mode == "existing-egress":
            if approved_https_prefix_list_id is None:
                raise ValueError(
                    "existing-egress with an AxonLLM security group requires an approved HTTPS prefix list"
                )
            runtime_security_group.add_egress_rule(
                ec2.Peer.prefix_list(approved_https_prefix_list_id),
                ec2.Port.tcp(443),
                "HTTPS through customer-owned egress",
            )
        security_groups = (runtime_security_group,)

    selection = ec2.SubnetSelection(subnets=list(subnets))
    return RuntimeNetworkResources(
        mode=mode,
        configuration=agentcore.RuntimeNetworkConfiguration.using_vpc(
            scope,
            vpc=vpc,
            security_groups=list(security_groups),
            vpc_subnets=selection,
        ),
        vpc=vpc,
        subnets=subnets,
        runtime_security_groups=security_groups,
    )


def _runtime_egress_mode(scope: Construct) -> RuntimeEgressMode:
    raw = scope.node.try_get_context("runtime_network_egress_mode")
    if not isinstance(raw, str) or raw not in _EGRESS_MODES:
        raise ValueError("runtime_network_egress_mode must be 'endpoints-only', 'existing-egress', or 'managed-nat'")
    return raw  # type: ignore[return-value]


def _validate_public_mode(scope: Construct) -> None:
    profile = scope.node.try_get_context("deployment_profile")
    namespace = scope.node.try_get_context("deployment_namespace")
    if profile != "development" or not isinstance(namespace, str) or not namespace:
        raise ValueError(
            "public runtime networking requires deployment_profile=development and a non-empty deployment_namespace"
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


__all__ = [
    "RuntimeNetworkResources",
    "build_runtime_network",
    "runtime_network_mode",
    "runtime_network_requires_prefix_list",
]
