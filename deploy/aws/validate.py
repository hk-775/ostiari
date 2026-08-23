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
            resource_types = _types(template)
            expected_services = 4 if demo else 3
            assert resource_types.count("AWS::ECS::Service") == expected_services
            demo_resources = [
                logical_id
                for logical_id in template["Resources"]
                if logical_id.startswith("DemoTools")
            ]
            assert bool(demo_resources) is demo
            assert ("AWS::BedrockAgentCore::Runtime" in resource_types) is agentcore
            assert ("AWS::WAFv2::WebACL" in resource_types) is production
            if production:
                assert template["Resources"][
                    next(
                        key
                        for key, value in template["Resources"].items()
                        if value["Type"] == "AWS::ElasticLoadBalancingV2::LoadBalancer"
                    )
                ]["Properties"]["LoadBalancerAttributes"]
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
