"""Read-only AWS preflight for AgentCore deployment networking."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from ipaddress import ip_network
import json
import re
from typing import Any

from src.gateway.deployment.config_contract import (
    required_endpoint_services,
    validate_deployment_config,
)


_SUPPORTED_AZS_PACKAGE = "src.gateway.deployment.infra"
_SUPPORTED_AZS_NAME = "agentcore-supported-availability-zones-v1.json"
_GATEWAY_ENDPOINT_SERVICES = frozenset({"dynamodb", "s3"})


class NetworkPreflightError(ValueError):
    """Raised when the selected network cannot safely host AxonLLM."""


@dataclass(frozen=True)
class NetworkPreflightResult:
    """Hashable, non-secret context produced by read-only inspection."""

    mode: str
    egress_mode: str | None
    runtime_context: dict[str, Any] | None
    managed_stack_context: dict[str, Any] | None
    required_services: tuple[str, ...]
    approved_https_prefix_list_id: str | None


def preflight_deployment_network(
    boto3_session: Any,
    config: dict[str, Any],
    *,
    account_id: str,
    deployment_namespace: str = "",
) -> NetworkPreflightResult:
    """Validate network inputs and return exact CDK context values."""

    validated = validate_deployment_config(config)
    network = validated["network"]
    mode = network["mode"]
    if mode == "public":
        if not deployment_namespace:
            raise NetworkPreflightError("public networking requires a non-empty deployment namespace")
        return NetworkPreflightResult(
            mode=mode,
            egress_mode=None,
            runtime_context={
                "deployment_namespace": deployment_namespace,
                "deployment_profile": validated["deployment_profile"],
                "runtime_network_mode": "public",
            },
            managed_stack_context=None,
            required_services=(),
            approved_https_prefix_list_id=None,
        )

    ec2_client = boto3_session.client(
        "ec2",
        region_name=validated["region"],
    )
    if mode == "managed":
        availability_zones = _resolve_availability_zone_names(
            ec2_client,
            region=validated["region"],
            zone_ids=tuple(network["availability_zone_ids"]),
        )
        egress = network["egress"]
        managed_context: dict[str, Any] = {
            "deployment_profile": validated["deployment_profile"],
            "managed_network_egress_mode": egress["mode"],
            "managed_network_vpc_cidr": network["vpc_cidr"],
            "managed_network_availability_zones": list(availability_zones),
            "managed_network_availability_zone_ids": list(network["availability_zone_ids"]),
        }
        if egress["mode"] == "managed-nat":
            managed_context.update(
                {
                    "managed_network_nat_gateway_count": egress["nat_gateway_count"],
                    "managed_network_cost_acknowledgement": True,
                }
            )
        if deployment_namespace:
            managed_context["deployment_namespace"] = deployment_namespace
        return NetworkPreflightResult(
            mode=mode,
            egress_mode=egress["mode"],
            runtime_context=None,
            managed_stack_context=managed_context,
            required_services=(required_endpoint_services(validated) if egress["mode"] == "endpoints-only" else ()),
            approved_https_prefix_list_id=egress.get("approved_https_prefix_list_id"),
        )

    return _preflight_existing_network(
        ec2_client,
        validated,
        account_id=account_id,
        deployment_namespace=deployment_namespace,
    )


def _preflight_existing_network(
    ec2_client: Any,
    config: dict[str, Any],
    *,
    account_id: str,
    deployment_namespace: str,
) -> NetworkPreflightResult:
    network = config["network"]
    region = config["region"]
    vpc_id = network["vpc_id"]
    vpcs = ec2_client.describe_vpcs(VpcIds=[vpc_id]).get("Vpcs")
    if not isinstance(vpcs, list) or len(vpcs) != 1:
        raise NetworkPreflightError("network.vpc_id did not resolve to exactly one VPC")
    vpc = vpcs[0]
    if (
        not isinstance(vpc, dict)
        or vpc.get("VpcId") != vpc_id
        or vpc.get("State") != "available"
        or vpc.get("OwnerId") != account_id
        or not isinstance(vpc.get("CidrBlock"), str)
    ):
        raise NetworkPreflightError("network.vpc_id must be an available VPC owned by the deployment account")
    _require_vpc_dns(ec2_client, vpc_id)

    subnet_ids = tuple(network["private_subnet_ids"])
    subnet_response = ec2_client.describe_subnets(SubnetIds=list(subnet_ids))
    subnets = subnet_response.get("Subnets")
    if not isinstance(subnets, list) or len(subnets) != len(subnet_ids):
        raise NetworkPreflightError("network.private_subnet_ids did not resolve exactly")
    by_id = {subnet.get("SubnetId"): subnet for subnet in subnets if isinstance(subnet, dict)}
    if set(by_id) != set(subnet_ids):
        raise NetworkPreflightError("network.private_subnet_ids did not resolve exactly")
    availability_zones: list[str] = []
    availability_zone_ids: list[str] = []
    for subnet_id in subnet_ids:
        subnet = by_id[subnet_id]
        zone_name = subnet.get("AvailabilityZone")
        zone_id = subnet.get("AvailabilityZoneId")
        if (
            subnet.get("VpcId") != vpc_id
            or subnet.get("OwnerId") != account_id
            or subnet.get("State") != "available"
            or subnet.get("MapPublicIpOnLaunch") is not False
            or not isinstance(zone_name, str)
            or not isinstance(zone_id, str)
        ):
            raise NetworkPreflightError(
                f"subnet {subnet_id} must be an available private subnet "
                "owned by the deployment account in the selected VPC"
            )
        availability_zones.append(zone_name)
        availability_zone_ids.append(zone_id)
    if len(set(availability_zone_ids)) != len(availability_zone_ids):
        raise NetworkPreflightError("network.private_subnet_ids must span distinct Availability Zone IDs")
    _require_supported_zone_ids(region, tuple(availability_zone_ids))

    security_group_ids = tuple(network["security_group_ids"])
    if security_group_ids:
        response = ec2_client.describe_security_groups(GroupIds=list(security_group_ids))
        groups = response.get("SecurityGroups")
        if not isinstance(groups, list) or len(groups) != len(security_group_ids):
            raise NetworkPreflightError("network.security_group_ids did not resolve exactly")
        resolved_ids = {
            group.get("GroupId")
            for group in groups
            if isinstance(group, dict) and group.get("VpcId") == vpc_id and group.get("OwnerId") == account_id
        }
        if resolved_ids != set(security_group_ids):
            raise NetworkPreflightError(
                "network.security_group_ids must belong to the selected VPC and deployment account"
            )

    route_table_ids = _effective_route_table_ids(
        ec2_client,
        vpc_id=vpc_id,
        subnet_ids=subnet_ids,
    )
    egress = network["egress"]
    if egress["mode"] == "endpoints-only":
        _require_no_default_routes(
            ec2_client,
            route_table_ids=route_table_ids,
        )
        services = required_endpoint_services(config)
        _require_vpc_endpoints(
            ec2_client,
            region=region,
            vpc_id=vpc_id,
            route_table_ids=route_table_ids,
            services=services,
        )
    else:
        _require_existing_egress(
            ec2_client,
            route_table_ids=route_table_ids,
        )
        prefix_list_id = egress.get("approved_https_prefix_list_id")
        if prefix_list_id is not None:
            _require_prefix_list(
                ec2_client,
                prefix_list_id=prefix_list_id,
                account_id=account_id,
            )

    runtime_context: dict[str, Any] = {
        "deployment_profile": config["deployment_profile"],
        "runtime_network_mode": "existing",
        "runtime_network_egress_mode": egress["mode"],
        "runtime_network_vpc_id": vpc_id,
        "runtime_network_vpc_cidr": vpc["CidrBlock"],
        "runtime_network_private_subnet_ids": list(subnet_ids),
        "runtime_network_availability_zones": availability_zones,
        "runtime_network_security_group_ids": list(security_group_ids),
    }
    if deployment_namespace:
        runtime_context["deployment_namespace"] = deployment_namespace
    return NetworkPreflightResult(
        mode="existing",
        egress_mode=egress["mode"],
        runtime_context=runtime_context,
        managed_stack_context=None,
        required_services=(required_endpoint_services(config) if egress["mode"] == "endpoints-only" else ()),
        approved_https_prefix_list_id=egress.get("approved_https_prefix_list_id"),
    )


def runtime_network_context(
    preflight: NetworkPreflightResult,
    *,
    managed_outputs: dict[str, str] | None = None,
    expected_managed_stack_name: str | None = None,
) -> dict[str, Any]:
    """Return the exact runtime CDK context bound by preflight evidence."""

    if preflight.runtime_context is not None:
        if managed_outputs is not None:
            raise NetworkPreflightError("managed network outputs are forbidden for this mode")
        return json.loads(json.dumps(preflight.runtime_context))
    if (
        preflight.mode != "managed"
        or preflight.managed_stack_context is None
        or managed_outputs is None
        or expected_managed_stack_name is None
    ):
        raise NetworkPreflightError("managed runtime networking requires verified stack outputs")
    required = {
        "AvailabilityZoneIds",
        "AvailabilityZones",
        "DeploymentNamespace",
        "EgressMode",
        "ManagedNetworkStackName",
        "NatGatewayCount",
        "PrivateSubnetIds",
        "RuntimeSecurityGroupIds",
        "VpcCidr",
        "VpcId",
    }
    missing = sorted(required.difference(managed_outputs))
    if missing:
        raise NetworkPreflightError(f"managed network outputs are incomplete: {', '.join(missing)}")
    if managed_outputs["ManagedNetworkStackName"] != expected_managed_stack_name:
        raise NetworkPreflightError("managed network output stack name does not match deployment")
    expected_namespace = preflight.managed_stack_context.get(
        "deployment_namespace",
        "production",
    )
    if managed_outputs["DeploymentNamespace"] != expected_namespace:
        raise NetworkPreflightError("managed network output namespace does not match deployment")
    if managed_outputs["EgressMode"] != preflight.egress_mode:
        raise NetworkPreflightError("managed network output egress mode does not match preflight")
    expected_nat_count = preflight.managed_stack_context.get(
        "managed_network_nat_gateway_count",
        0,
    )
    if managed_outputs["NatGatewayCount"] != str(expected_nat_count):
        raise NetworkPreflightError("managed network output NAT count does not match preflight")

    vpc_id = managed_outputs["VpcId"]
    if (
        re.fullmatch(
            r"vpc-(?:[0-9a-f]{8}|[0-9a-f]{17})",
            vpc_id,
        )
        is None
    ):
        raise NetworkPreflightError("managed network output contains an invalid VPC ID")
    try:
        parsed_vpc_cidr = ip_network(
            managed_outputs["VpcCidr"],
            strict=True,
        )
    except ValueError as exc:
        raise NetworkPreflightError("managed network output contains an invalid VPC CIDR") from exc
    if parsed_vpc_cidr.version != 4:
        raise NetworkPreflightError("managed network output contains an invalid VPC CIDR")
    expected_vpc_cidr = preflight.managed_stack_context["managed_network_vpc_cidr"]
    if str(parsed_vpc_cidr) != expected_vpc_cidr:
        raise NetworkPreflightError("managed network output VPC CIDR does not match preflight")
    subnet_ids = _csv_output(
        managed_outputs["PrivateSubnetIds"],
        field="PrivateSubnetIds",
        pattern=r"subnet-(?:[0-9a-f]{8}|[0-9a-f]{17})",
    )
    security_group_ids = _csv_output(
        managed_outputs["RuntimeSecurityGroupIds"],
        field="RuntimeSecurityGroupIds",
        pattern=r"sg-(?:[0-9a-f]{8}|[0-9a-f]{17})",
    )
    availability_zones = _csv_output(
        managed_outputs["AvailabilityZones"],
        field="AvailabilityZones",
        pattern=r"[a-z]{2,8}(?:-[a-z0-9]+)+-[0-9]+[a-z]",
    )
    availability_zone_ids = _csv_output(
        managed_outputs["AvailabilityZoneIds"],
        field="AvailabilityZoneIds",
        pattern=r"[a-z0-9]+-az[0-9]+",
    )
    expected_zone_ids = tuple(preflight.managed_stack_context["managed_network_availability_zone_ids"])
    if availability_zone_ids != expected_zone_ids:
        raise NetworkPreflightError("managed network output Availability Zone IDs do not match preflight")
    expected_zones = tuple(preflight.managed_stack_context["managed_network_availability_zones"])
    if availability_zones != expected_zones:
        raise NetworkPreflightError("managed network output Availability Zones do not match preflight")
    if not (len(subnet_ids) == len(availability_zones) == len(availability_zone_ids)):
        raise NetworkPreflightError("managed network output subnet and Availability Zone counts do not match")
    return {
        "deployment_profile": preflight.managed_stack_context["deployment_profile"],
        **({"deployment_namespace": expected_namespace} if expected_namespace != "production" else {}),
        "runtime_network_mode": "managed",
        "runtime_network_egress_mode": preflight.egress_mode,
        "runtime_network_vpc_id": vpc_id,
        "runtime_network_vpc_cidr": str(parsed_vpc_cidr),
        "runtime_network_private_subnet_ids": list(subnet_ids),
        "runtime_network_availability_zones": list(availability_zones),
        "runtime_network_security_group_ids": list(security_group_ids),
    }


def _resolve_availability_zone_names(
    ec2_client: Any,
    *,
    region: str,
    zone_ids: tuple[str, ...],
) -> tuple[str, ...]:
    _require_supported_zone_ids(region, zone_ids)
    response = ec2_client.describe_availability_zones(
        Filters=[
            {
                "Name": "zone-id",
                "Values": list(zone_ids),
            },
            {
                "Name": "state",
                "Values": ["available"],
            },
        ],
        AllAvailabilityZones=False,
    )
    zones = response.get("AvailabilityZones")
    if not isinstance(zones, list):
        raise NetworkPreflightError("AWS returned malformed Availability Zone metadata")
    names_by_id = {
        zone.get("ZoneId"): zone.get("ZoneName")
        for zone in zones
        if isinstance(zone, dict)
        and zone.get("State") == "available"
        and zone.get("ZoneType") == "availability-zone"
        and zone.get("OptInStatus") in {"opt-in-not-required", "opted-in"}
        and isinstance(zone.get("ZoneId"), str)
        and isinstance(zone.get("ZoneName"), str)
    }
    if set(names_by_id) != set(zone_ids):
        missing = sorted(set(zone_ids).difference(names_by_id))
        raise NetworkPreflightError(
            f"managed network Availability Zone IDs are not available in this account: {', '.join(missing)}"
        )
    return tuple(names_by_id[zone_id] for zone_id in zone_ids)


def _require_vpc_dns(ec2_client: Any, vpc_id: str) -> None:
    for attribute in ("enableDnsSupport", "enableDnsHostnames"):
        response = ec2_client.describe_vpc_attribute(
            VpcId=vpc_id,
            Attribute=attribute,
        )
        value = response.get(attribute[0].upper() + attribute[1:])
        if not isinstance(value, dict) or value.get("Value") is not True:
            raise NetworkPreflightError(f"VPC {vpc_id} must have {attribute} enabled")


def _effective_route_table_ids(
    ec2_client: Any,
    *,
    vpc_id: str,
    subnet_ids: tuple[str, ...],
) -> tuple[str, ...]:
    route_tables = _paginated_items(
        ec2_client,
        "describe_route_tables",
        "RouteTables",
        Filters=[
            {
                "Name": "vpc-id",
                "Values": [vpc_id],
            }
        ],
    )
    explicit: dict[str, str] = {}
    main_route_table_id = None
    for route_table in route_tables:
        if not isinstance(route_table, dict):
            continue
        route_table_id = route_table.get("RouteTableId")
        if not isinstance(route_table_id, str):
            continue
        for association in route_table.get("Associations", []):
            if not isinstance(association, dict):
                continue
            subnet_id = association.get("SubnetId")
            if isinstance(subnet_id, str):
                explicit[subnet_id] = route_table_id
            if association.get("Main") is True:
                main_route_table_id = route_table_id
    resolved = []
    for subnet_id in subnet_ids:
        route_table_id = explicit.get(
            subnet_id,
            main_route_table_id,
        )
        if route_table_id is None:
            raise NetworkPreflightError(f"could not resolve the effective route table for {subnet_id}")
        resolved.append(route_table_id)
    return tuple(resolved)


def _route_tables_by_id(
    ec2_client: Any,
    route_table_ids: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    response = ec2_client.describe_route_tables(RouteTableIds=sorted(set(route_table_ids)))
    route_tables = response.get("RouteTables")
    if not isinstance(route_tables, list):
        raise NetworkPreflightError("AWS returned malformed route-table metadata")
    by_id = {
        route_table.get("RouteTableId"): route_table
        for route_table in route_tables
        if isinstance(route_table, dict) and isinstance(route_table.get("RouteTableId"), str)
    }
    if set(by_id) != set(route_table_ids):
        raise NetworkPreflightError("selected subnet route tables did not resolve exactly")
    return by_id


def _active_default_routes(route_table: dict[str, Any]) -> list[dict]:
    return [
        route
        for route in route_table.get("Routes", [])
        if isinstance(route, dict)
        and route.get("State", "active") == "active"
        and (route.get("DestinationCidrBlock") == "0.0.0.0/0" or route.get("DestinationIpv6CidrBlock") == "::/0")
    ]


def _require_no_default_routes(
    ec2_client: Any,
    *,
    route_table_ids: tuple[str, ...],
) -> None:
    for route_table_id, route_table in _route_tables_by_id(
        ec2_client,
        route_table_ids,
    ).items():
        if _active_default_routes(route_table):
            raise NetworkPreflightError(
                f"endpoints-only subnet route table {route_table_id} contains an active default route"
            )


def _require_existing_egress(
    ec2_client: Any,
    *,
    route_table_ids: tuple[str, ...],
) -> None:
    for route_table_id, route_table in _route_tables_by_id(
        ec2_client,
        route_table_ids,
    ).items():
        defaults = _active_default_routes(route_table)
        if not defaults:
            raise NetworkPreflightError(
                f"existing-egress subnet route table {route_table_id} has no active default route"
            )
        if any(str(route.get("GatewayId", "")).startswith("igw-") for route in defaults):
            raise NetworkPreflightError(
                f"existing-egress subnet route table {route_table_id} must not route directly to an internet gateway"
            )


def _require_vpc_endpoints(
    ec2_client: Any,
    *,
    region: str,
    vpc_id: str,
    route_table_ids: tuple[str, ...],
    services: tuple[str, ...],
) -> None:
    endpoints = _paginated_items(
        ec2_client,
        "describe_vpc_endpoints",
        "VpcEndpoints",
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "vpc-endpoint-state", "Values": ["available"]},
        ],
    )
    by_service = {
        endpoint.get("ServiceName"): endpoint
        for endpoint in endpoints
        if isinstance(endpoint, dict)
        and endpoint.get("VpcId") == vpc_id
        and endpoint.get("State") == "available"
        and isinstance(endpoint.get("ServiceName"), str)
    }
    for service in services:
        service_name = f"com.amazonaws.{region}.{service}"
        endpoint = by_service.get(service_name)
        if endpoint is None:
            raise NetworkPreflightError(f"endpoints-only networking is missing available endpoint {service_name}")
        if service in _GATEWAY_ENDPOINT_SERVICES:
            endpoint_route_tables = endpoint.get("RouteTableIds")
            if not isinstance(endpoint_route_tables, list) or not set(route_table_ids).issubset(endpoint_route_tables):
                raise NetworkPreflightError(
                    f"gateway endpoint {service_name} is not associated with every selected subnet route table"
                )
        elif (
            endpoint.get("VpcEndpointType") != "Interface"
            or endpoint.get("PrivateDnsEnabled") is not True
            or not endpoint.get("Groups")
        ):
            raise NetworkPreflightError(
                f"interface endpoint {service_name} must have private DNS and at least one security group"
            )


def _require_prefix_list(
    ec2_client: Any,
    *,
    prefix_list_id: str,
    account_id: str,
) -> None:
    response = ec2_client.describe_managed_prefix_lists(PrefixListIds=[prefix_list_id])
    prefix_lists = response.get("PrefixLists")
    if not isinstance(prefix_lists, list) or len(prefix_lists) != 1:
        raise NetworkPreflightError("approved HTTPS prefix list did not resolve exactly")
    prefix_list = prefix_lists[0]
    if (
        not isinstance(prefix_list, dict)
        or prefix_list.get("PrefixListId") != prefix_list_id
        or prefix_list.get("OwnerId") != account_id
        or prefix_list.get("AddressFamily") != "IPv4"
        or prefix_list.get("State")
        not in {
            "create-complete",
            "modify-complete",
            "restore-complete",
        }
    ):
        raise NetworkPreflightError("approved HTTPS prefix list must be a stable customer-owned IPv4 prefix list")


def _require_supported_zone_ids(
    region: str,
    zone_ids: tuple[str, ...],
) -> None:
    document = json.loads(files(_SUPPORTED_AZS_PACKAGE).joinpath(_SUPPORTED_AZS_NAME).read_text(encoding="utf-8"))
    supported = document["regions"].get(region)
    if not isinstance(supported, list):
        raise NetworkPreflightError(f"AgentCore VPC networking is not supported in {region}")
    unsupported = sorted(set(zone_ids).difference(supported))
    if unsupported:
        raise NetworkPreflightError(
            f"network uses unsupported AgentCore Availability Zone IDs: {', '.join(unsupported)}"
        )


def _paginated_items(
    client: Any,
    operation_name: str,
    result_key: str,
    **kwargs: Any,
) -> list[Any]:
    paginator = client.get_paginator(operation_name)
    items: list[Any] = []
    for page in paginator.paginate(**kwargs):
        values = page.get(result_key)
        if not isinstance(values, list):
            raise NetworkPreflightError(f"AWS returned malformed {result_key} metadata")
        items.extend(values)
    return items


def _csv_output(
    value: str,
    *,
    field: str,
    pattern: str,
) -> tuple[str, ...]:
    items = tuple(value.split(","))
    if (
        not items
        or any(not item or re.fullmatch(pattern, item) is None for item in items)
        or len(set(items)) != len(items)
    ):
        raise NetworkPreflightError(f"managed network output {field} is malformed")
    return items


__all__ = [
    "NetworkPreflightError",
    "NetworkPreflightResult",
    "preflight_deployment_network",
    "runtime_network_context",
]
