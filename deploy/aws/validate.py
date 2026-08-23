#!/usr/bin/env python3
"""Synthesize and validate every supported AWS deployment profile."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ACCOUNT = "123456789012"
REGION = "us-east-1"
DIGESTS = {
    "control_plane": "a" * 64,
    "gateway": "b" * 64,
    "frontend": "c" * 64,
    "agentcore": "d" * 64,
}


def _production(agentcore: bool) -> dict:
    images = {
        key: (
            f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
            f"ostiari/{key.replace('_', '-')}@sha256:{digest}"
        )
        for key, digest in DIGESTS.items()
        if key != "agentcore" or agentcore
    }
    secrets = {
        key: (f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:ostiari-{key}-AbCd12")
        for key in (
            "jwt",
            "admin_password",
            "encryption_key",
            "config_admin_key",
            "gateway_agent_token",
            "workload_client_secret",
        )
    }
    auth = {
        "workload_issuer": "https://workload.example.com",
        "workload_audience": "ostiari-control-plane",
        "workload_token_url": "https://workload.example.com/oauth2/token",
        "gateway_client_id": "gateway-production",
        "agent_issuer": "https://agents.example.com",
        "agent_audience": "ostiari-gateway",
    }
    if agentcore:
        secrets["agentcore_client_secret"] = (
            f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:ostiari-agentcore-AbCd12"
        )
        auth.update(
            {
                "agentcore_token_url": "https://agents.example.com/oauth2/token",
                "agentcore_client_id": "agentcore-production",
            }
        )
    return {
        "production": True,
        "org_id": "production",
        "desired_count": 2,
        "images": images,
        "secrets": secrets,
        "auth": auth,
        "domains": {
            "dashboard": "ostiari.example.com",
            "gateway": "gateway.ostiari.example.com",
            "certificate_arn": (
                f"arn:aws:acm:{REGION}:{ACCOUNT}:certificate/00000000-0000-0000-0000-000000000000"
            ),
        },
    }


PROFILES = (
    ("aws-demo", True, False, False),
    ("aws-empty", False, False, False),
    ("aws-agentcore-demo", True, True, False),
    ("aws-agentcore-empty", False, True, False),
    ("production", False, False, True),
    ("production-agentcore", False, True, True),
)


def _types(template: dict) -> list[str]:
    return [resource["Type"] for resource in template["Resources"].values()]


def main() -> int:
    cfn_lint = shutil.which("cfn-lint")
    if not cfn_lint:
        raise RuntimeError("cfn-lint is required")
    with tempfile.TemporaryDirectory(prefix="ostiari-cdk-") as temporary:
        root = Path(temporary)
        cache = root / "jsii-cache"
        for index, (profile, demo, agentcore, production) in enumerate(PROFILES):
            name = f"validate-{index}"
            config = {
                "name": name,
                "profile": profile,
                "account": ACCOUNT,
                "region": REGION,
                "allowed_cidr": "203.0.113.10/32",
                "demo": demo,
                "agentcore": agentcore,
                "production": production,
            }
            if agentcore or production:
                config.update(
                    {
                        "availability_zone_ids": ["use1-az1", "use1-az2"],
                        "availability_zones": ["us-east-1c", "us-east-1d"],
                    }
                )
            if production:
                config.update(_production(agentcore))
            config_path = root / f"{profile}.json"
            output = root / profile
            config_path.write_text(json.dumps(config))
            env = os.environ.copy()
            env.update(
                {
                    "OSTIARI_DEPLOY_CONFIG": str(config_path),
                    "CDK_OUTDIR": str(output),
                    "JSII_RUNTIME_PACKAGE_CACHE": str(cache),
                }
            )
            subprocess.run([sys.executable, str(HERE / "app.py")], env=env, check=True)
            template_path = output / f"Ostiari-{name}.template.json"
            template = json.loads(template_path.read_text())
            assets = json.loads(
                (output / f"Ostiari-{name}.assets.json").read_text()
            )
            resource_types = _types(template)
            expected_services = 7 if demo else 3
            assert resource_types.count("AWS::ECS::Service") == expected_services
            assert resource_types.count("AWS::CloudFront::Distribution") == 1
            assert resource_types.count("AWS::WAFv2::WebACL") == 1
            assert resource_types.count("AWS::WAFv2::IPSet") >= 1
            demo_resources = [
                logical_id
                for logical_id in template["Resources"]
                if logical_id.startswith("DemoTools")
            ]
            assert bool(demo_resources) is demo
            control_task = next(
                resource
                for logical_id, resource in template["Resources"].items()
                if logical_id.startswith("ControlPlaneTask")
                and resource["Type"] == "AWS::ECS::TaskDefinition"
            )
            control_secret_names = {
                secret["Name"]
                for container in control_task["Properties"]["ContainerDefinitions"]
                for secret in container.get("Secrets", [])
            }
            assert ("OSTIARI_ADMIN_PASSWORD" in control_secret_names) is (not demo)
            outputs = template.get("Outputs", {})
            assert ("DemoLoginEnabled" in outputs) is demo
            assert ("AdminSecretArn" in outputs) is (not demo)
            if not production:
                frontend_asset = next(
                    asset["source"]
                    for asset in assets["dockerImages"].values()
                    if asset["source"].get("dockerFile")
                    == "deploy/docker/Dockerfile.frontend"
                )
                assert frontend_asset["dockerBuildArgs"]["VITE_DEMO_LOGIN"] == (
                    "true" if demo else "false"
                )
            assert ("AWS::BedrockAgentCore::Runtime" in resource_types) is agentcore
            assert "CloudFrontDistributionId" in outputs
            assert "CloudFrontDomainName" in outputs
            prefix_list_rules = [
                resource
                for resource in template["Resources"].values()
                if resource["Type"] == "AWS::EC2::SecurityGroupIngress"
                and "SourcePrefixListId" in resource["Properties"]
            ]
            assert len(prefix_list_rules) == 1
            if demo:
                demo_gateway_ids = {
                    environment["Value"]
                    for resource in template["Resources"].values()
                    if resource["Type"] == "AWS::ECS::TaskDefinition"
                    for container in resource["Properties"]["ContainerDefinitions"]
                    for environment in container.get("Environment", [])
                    if environment["Name"] == "OSTIARI_GATEWAY_ID"
                }
                assert {
                    "crm-agent",
                    "ops-agent",
                    "devops-agent",
                    "analytics-agent",
                } <= demo_gateway_ids
            if config.get("availability_zones"):
                subnet_zones = {
                    resource["Properties"]["AvailabilityZone"]
                    for resource in template["Resources"].values()
                    if resource["Type"] == "AWS::EC2::Subnet"
                }
                assert subnet_zones == set(config["availability_zones"])
            if agentcore:
                execution_role_id, execution_role = next(
                    (logical_id, resource)
                    for logical_id, resource in template["Resources"].items()
                    if resource["Type"] == "AWS::IAM::Role"
                    and resource["Properties"]["AssumeRolePolicyDocument"]["Statement"][0][
                        "Principal"
                    ].get("Service")
                    == "bedrock-agentcore.amazonaws.com"
                )
                trust = execution_role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
                assert {"StringEquals", "ArnLike"} <= set(trust["Condition"])
                execution_policies = [
                    resource
                    for resource in template["Resources"].values()
                    if resource["Type"] == "AWS::IAM::Policy"
                    and {"Ref": execution_role_id} in resource["Properties"]["Roles"]
                ]
                actions = {
                    action
                    for policy in execution_policies
                    for statement in policy["Properties"]["PolicyDocument"]["Statement"]
                    for action in (
                        statement["Action"]
                        if isinstance(statement["Action"], list)
                        else [statement["Action"]]
                    )
                }
                assert {
                    "logs:PutResourcePolicy",
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "xray:PutTraceSegments",
                    "cloudwatch:PutMetricData",
                } <= actions
            if production:
                assert template["Resources"][
                    next(
                        key
                        for key, value in template["Resources"].items()
                        if value["Type"] == "AWS::ElasticLoadBalancingV2::LoadBalancer"
                    )
                ]["Properties"]["LoadBalancerAttributes"]
                required_ecr_actions = {
                    "ecr:GetAuthorizationToken",
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                }
                for task_definition in (
                    resource
                    for resource in template["Resources"].values()
                    if resource["Type"] == "AWS::ECS::TaskDefinition"
                ):
                    execution_role_id = task_definition["Properties"]["ExecutionRoleArn"][
                        "Fn::GetAtt"
                    ][0]
                    execution_policies = [
                        resource
                        for resource in template["Resources"].values()
                        if resource["Type"] == "AWS::IAM::Policy"
                        and {"Ref": execution_role_id} in resource["Properties"]["Roles"]
                    ]
                    actions = {
                        action
                        for policy in execution_policies
                        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
                        for action in (
                            statement["Action"]
                            if isinstance(statement["Action"], list)
                            else [statement["Action"]]
                        )
                    }
                    assert required_ecr_actions <= actions
            subprocess.run(
                [
                    cfn_lint,
                    "-i",
                    "W3005",
                    "W3010",
                    "-t",
                    str(template_path),
                ],
                check=True,
            )
            print(f"validated {profile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
