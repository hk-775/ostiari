"""Adopter-facing deployment CLI.

The CLI intentionally uses only the Python standard library. Local deployment
requires Docker Compose. AWS deployment bootstraps an isolated CDK toolchain
under ``deploy/aws`` and never installs packages into the user's Python.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
AWS_DIR = DEPLOY / "aws"
STATE_DIR = ROOT / ".ostiari" / "deployments"
AGENTCORE_AZS = (
    ROOT
    / "vendor"
    / "axonllm"
    / "src"
    / "gateway"
    / "deployment"
    / "infra"
    / "agentcore-supported-availability-zones-v1.json"
)


@dataclass(frozen=True)
class Profile:
    name: str
    target: str
    demo: bool
    agentcore: bool = False
    production: bool = False
    description: str = ""


PROFILES = {
    profile.name: profile
    for profile in (
        Profile("local-demo", "local", True, description="Local stack with realistic demo data"),
        Profile("local-empty", "local", False, description="Local stack with no seeded data"),
        Profile(
            "aws-demo", "aws", True, description="Cost-aware AWS adoption stack with demo data"
        ),
        Profile(
            "aws-empty", "aws", False, description="Cost-aware AWS adoption stack with no demo data"
        ),
        Profile(
            "aws-agentcore-demo",
            "aws",
            True,
            agentcore=True,
            description="AWS adoption stack plus an AgentCore governance bridge",
        ),
        Profile(
            "aws-agentcore-empty",
            "aws",
            False,
            agentcore=True,
            description="Empty AWS stack plus an AgentCore governance bridge",
        ),
        Profile(
            "production",
            "aws",
            False,
            production=True,
            description="Hardened production stack; creates a reviewed change set",
        ),
        Profile(
            "production-agentcore",
            "aws",
            False,
            agentcore=True,
            production=True,
            description="Hardened production stack with AgentCore bridge",
        ),
    )
}


class DeployError(RuntimeError):
    pass


def _run(
    command: Sequence[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    display = " ".join(command)
    print(f"+ {display}")
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        check=check,
        text=True,
        capture_output=capture_output,
    )


def _require(command: str, hint: str) -> None:
    if shutil.which(command) is None:
        raise DeployError(f"{command!r} is required. {hint}")


def _http_ready(url: str, *, timeout: int = 180) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=4) as response:
                if 200 <= response.status < 300:
                    return
                last_error = f"HTTP {response.status}"
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(2)
    raise DeployError(f"Timed out waiting for {url}: {last_error}")


def _profile(name: str, target: str | None = None) -> Profile:
    profile = PROFILES.get(name)
    if profile is None:
        raise DeployError(f"Unknown profile {name!r}. Run 'deploy/ostiari profiles'.")
    if target and profile.target != target:
        raise DeployError(f"Profile {name!r} is for {profile.target}, not {target}.")
    return profile


def _local_env(profile: Profile, args: argparse.Namespace) -> dict[str, str]:
    env = os.environ.copy()
    gateway_port = str(args.gateway_port)
    control_port = str(args.control_plane_port)
    frontend_port = str(args.frontend_port)
    demo_port = str(args.demo_tools_port)
    env.update(
        {
            "OSTIARI_GATEWAY_PORT": gateway_port,
            "OSTIARI_CONTROL_PLANE_PORT": control_port,
            "OSTIARI_FRONTEND_PORT": frontend_port,
            "OSTIARI_DEMO_TOOLS_PORT": demo_port,
            "OSTIARI_REDIS_PORT": str(args.redis_port),
            "OSTIARI_BROWSER_API_URL": f"http://localhost:{control_port}",
            "OSTIARI_NO_DEMO": "0" if profile.demo else "1",
            "OSTIARI_GATEWAY_ID": "crm-agent" if profile.demo else "my-gateway",
        }
    )
    return env


def _compose(profile: Profile, args: argparse.Namespace) -> tuple[list[str], dict[str, str]]:
    _require("docker", "Install Docker Desktop or Docker Engine with Compose v2.")
    result = subprocess.run(
        ["docker", "info"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode:
        raise DeployError("Docker is installed but the Docker daemon is not running.")
    command = [
        "docker",
        "compose",
        "--project-name",
        f"ostiari-{profile.name}",
        "--file",
        str(DEPLOY / "docker" / "docker-compose.yml"),
    ]
    if profile.demo:
        command.extend(["--file", str(DEPLOY / "docker" / "docker-compose.demo.yml")])
    return command, _local_env(profile, args)


def _wait_demo_seed(command: Sequence[str], env: dict[str, str], *, timeout: int = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        container = subprocess.run(
            [*command, "ps", "--all", "--quiet", "demo-seed"],
            env=env,
            check=False,
            text=True,
            capture_output=True,
        ).stdout.strip()
        if container:
            state = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Status}} {{.State.ExitCode}}",
                    container,
                ],
                check=False,
                text=True,
                capture_output=True,
            )
            if state.returncode == 0:
                status, _, exit_code = state.stdout.strip().partition(" ")
                if status == "exited":
                    if exit_code == "0":
                        return
                    raise DeployError(
                        "The demo seed job failed. Run "
                        "'./deploy/ostiari local logs --profile local-demo'."
                    )
        time.sleep(2)
    raise DeployError("Timed out waiting for the demo seed job.")


def local_up(args: argparse.Namespace) -> None:
    profile = _profile(args.profile, "local")
    command, env = _compose(profile, args)
    _run([*command, "up", "--detach", "--build", "--remove-orphans"], env=env)

    control = f"http://localhost:{args.control_plane_port}/api/ready"
    gateway = f"http://localhost:{args.gateway_port}/ready"
    frontend = f"http://localhost:{args.frontend_port}/"
    print("Waiting for Ostiari readiness...")
    try:
        _http_ready(control)
        _http_ready(gateway)
        _http_ready(frontend)

        if profile.demo:
            _http_ready(f"http://localhost:{args.demo_tools_port}/health")
            _wait_demo_seed(command, env)
    except DeployError:
        print("\nLocal deployment did not become ready. Current state:")
        _run([*command, "ps", "--all"], env=env, check=False)
        print("\nRecent service logs:")
        _run([*command, "logs", "--tail", "120"], env=env, check=False)
        raise

    print()
    print(f"Ostiari is ready ({profile.name})")
    print(f"  Dashboard: http://localhost:{args.frontend_port}")
    print(f"  API:       http://localhost:{args.control_plane_port}/docs")
    print(f"  Gateway:   http://localhost:{args.gateway_port}/ready")
    if profile.demo:
        print(f"  Demo tools: http://localhost:{args.demo_tools_port}/health")
        print("  Login:     admin@ostiari.ai / admin")


def local_down(args: argparse.Namespace) -> None:
    profile = _profile(args.profile, "local")
    command, env = _compose(profile, args)
    suffix = ["down", "--remove-orphans"]
    if args.purge:
        suffix.extend(["--volumes", "--rmi", "local"])
    _run([*command, *suffix], env=env)


def local_status(args: argparse.Namespace) -> None:
    profile = _profile(args.profile, "local")
    command, env = _compose(profile, args)
    _run([*command, "ps"], env=env)


def local_logs(args: argparse.Namespace) -> None:
    profile = _profile(args.profile, "local")
    command, env = _compose(profile, args)
    suffix = ["logs", "--tail", str(args.tail)]
    if args.follow:
        suffix.append("--follow")
    _run([*command, *suffix], env=env)


def _aws_identity(region: str) -> dict[str, Any]:
    _require("aws", "Install AWS CLI v2 and authenticate with 'aws login'.")
    result = subprocess.run(
        [
            "aws",
            "sts",
            "get-caller-identity",
            "--region",
            region,
            "--output",
            "json",
            "--no-cli-pager",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def _aws_capture(arguments: Sequence[str], *, check: bool = True) -> str:
    result = subprocess.run(
        ["aws", *arguments, "--no-cli-pager"],
        check=check,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _agentcore_availability_zone_config(region: str) -> dict[str, list[str]]:
    try:
        registry = json.loads(AGENTCORE_AZS.read_text())
        supported = registry["regions"][region]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise DeployError(
            f"AgentCore VPC availability-zone metadata is unavailable for {region}."
        ) from exc
    if (
        not isinstance(supported, list)
        or len(supported) < 2
        or not all(isinstance(zone_id, str) and zone_id for zone_id in supported)
    ):
        raise DeployError(f"AgentCore VPC availability-zone metadata is invalid for {region}.")

    try:
        response = json.loads(
            _aws_capture(
                [
                    "ec2",
                    "describe-availability-zones",
                    "--region",
                    region,
                    "--output",
                    "json",
                ]
            )
        )
        zones = response["AvailabilityZones"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise DeployError("Could not parse AWS availability-zone metadata.") from exc
    if not isinstance(zones, list):
        raise DeployError("AWS returned malformed availability-zone metadata.")

    names_by_id = {
        zone["ZoneId"]: zone["ZoneName"]
        for zone in zones
        if isinstance(zone, dict)
        and zone.get("State") == "available"
        and zone.get("ZoneType") == "availability-zone"
        and zone.get("OptInStatus") in {"opt-in-not-required", "opted-in"}
        and isinstance(zone.get("ZoneId"), str)
        and isinstance(zone.get("ZoneName"), str)
    }
    selected_ids = [zone_id for zone_id in supported if zone_id in names_by_id][:2]
    if len(selected_ids) < 2:
        missing = ", ".join(supported)
        raise DeployError(
            f"AgentCore requires two supported availability zones in {region}; "
            f"this account does not expose two of: {missing}."
        )
    selected_names = [names_by_id[zone_id] for zone_id in selected_ids]
    print(
        "  AgentCore availability zones: "
        + ", ".join(
            f"{zone_name} ({zone_id})"
            for zone_name, zone_id in zip(selected_names, selected_ids, strict=True)
        )
    )
    return {
        "availability_zone_ids": selected_ids,
        "availability_zones": selected_names,
    }


def _determine_cidr(value: str) -> str:
    if value != "auto":
        return value
    try:
        with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=5) as response:
            ip = response.read().decode().strip()
    except (OSError, urllib.error.URLError) as exc:
        raise DeployError(
            "Could not determine your public IP. Pass --allowed-cidr A.B.C.D/32."
        ) from exc
    return f"{ip}/32"


def _load_json(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    source = Path(path).expanduser().resolve()
    try:
        value = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DeployError(f"Cannot read deployment config {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise DeployError("Deployment config must be a JSON object.")
    return value


def _validate_aws_config(config: dict[str, Any]) -> None:
    module_name = "_ostiari_aws_deployment_config"
    module = sys.modules.get(module_name)
    if module is None:
        spec = importlib.util.spec_from_file_location(module_name, AWS_DIR / "config.py")
        if spec is None or spec.loader is None:
            raise DeployError("Could not load the AWS deployment configuration validator.")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    try:
        module.DeploymentConfig.from_dict(config)
    except ValueError as exc:
        raise DeployError(str(exc)) from exc


def _aws_config(args: argparse.Namespace) -> tuple[Profile, Path, dict[str, Any]]:
    profile = _profile(args.profile, "aws")
    if profile.production and not args.config:
        raise DeployError(
            "Production requires --config deploy/aws/examples/production.json "
            "(copy it and replace every REPLACE_* value)."
        )
    saved_config = STATE_DIR / args.name / "config.json"
    config_source = args.config
    if not config_source and saved_config.exists():
        config_source = str(saved_config)
    config = _load_json(config_source)
    identity = _aws_identity(args.region)
    config.update(
        {
            "name": args.name,
            "profile": profile.name,
            "account": identity["Account"],
            "region": args.region,
            "demo": profile.demo,
            "agentcore": profile.agentcore,
            "production": profile.production,
        }
    )
    if profile.agentcore:
        config.update(_agentcore_availability_zone_config(args.region))
    if args.allowed_cidr != "auto":
        config.pop("allowed_cidrs", None)
        config["allowed_cidr"] = args.allowed_cidr
    elif not config.get("allowed_cidrs") and not config.get("allowed_cidr"):
        config["allowed_cidr"] = _determine_cidr(args.allowed_cidr)
    _validate_aws_config(config)
    directory = STATE_DIR / args.name
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "config.json"
    target.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    return profile, target, config


def _ensure_aws_tools() -> tuple[Path, Path]:
    _require("node", "Install Node.js 22 or newer.")
    _require("npm", "Install npm.")
    version = subprocess.run(
        ["node", "--version"], check=True, text=True, capture_output=True
    ).stdout.strip()
    try:
        major = int(version.removeprefix("v").split(".", 1)[0])
    except ValueError as exc:
        raise DeployError(f"Cannot parse Node.js version {version!r}.") from exc
    if major < 22:
        raise DeployError("Node.js 22 or newer is required for the CDK toolchain.")
    python = AWS_DIR / ".venv" / "bin" / "python"
    cdk = AWS_DIR / "node_modules" / ".bin" / "cdk"
    if not python.exists():
        _run([sys.executable, "-m", "venv", str(AWS_DIR / ".venv")])
        _run(
            [
                str(AWS_DIR / ".venv" / "bin" / "pip"),
                "install",
                "--require-hashes",
                "--only-binary=:all:",
                "-r",
                str(AWS_DIR / "requirements.txt"),
            ]
        )
    if not cdk.exists():
        _run(["npm", "ci"], cwd=AWS_DIR)
    return python, cdk


def _cdk_environment(config_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["OSTIARI_DEPLOY_CONFIG"] = str(config_path)
    env.setdefault("JSII_RUNTIME_PACKAGE_CACHE", str(Path("/tmp") / "ostiari-jsii-cache"))
    return env


def _stack_name(config: dict[str, Any]) -> str:
    return _stack_name_for(str(config["name"]))


def _stack_name_for(name: str) -> str:
    safe = "".join(c if c.isalnum() else "-" for c in name).strip("-")
    return f"Ostiari-{safe}"


def _cdk_app() -> str:
    return f"{AWS_DIR / '.venv/bin/python'} {AWS_DIR / 'app.py'}"


def _bootstrap(cdk: Path, config: dict[str, Any], env: dict[str, str]) -> None:
    print("Ensuring the standard CDK bootstrap stack is current...")
    _run(
        [
            str(cdk),
            "bootstrap",
            f"aws://{config['account']}/{config['region']}",
            "--toolkit-stack-name",
            "CDKToolkit",
            "--termination-protection",
        ],
        env=env,
    )


def _preflight(profile: Profile, config: dict[str, Any]) -> None:
    region = str(config["region"])
    stack = _stack_name(config)
    print(f"Running AWS preflight for {profile.name}...")

    stack_result = subprocess.run(
        [
            "aws",
            "cloudformation",
            "describe-stacks",
            "--region",
            region,
            "--stack-name",
            stack,
            "--output",
            "json",
            "--no-cli-pager",
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    stack_exists = stack_result.returncode == 0
    if stack_result.returncode == 0:
        try:
            status = str(json.loads(stack_result.stdout)["Stacks"][0]["StackStatus"])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise DeployError(f"Could not parse CloudFormation status for {stack}.") from exc
        blocked = {
            "CREATE_FAILED",
            "DELETE_FAILED",
            "ROLLBACK_COMPLETE",
            "ROLLBACK_FAILED",
            "UPDATE_ROLLBACK_FAILED",
            "IMPORT_ROLLBACK_FAILED",
        }
        if status in blocked:
            raise DeployError(f"{stack} is in {status}; recover or delete it before deploying.")
        if "IN_PROGRESS" in status and status != "REVIEW_IN_PROGRESS":
            raise DeployError(f"{stack} is in {status}; wait for that operation to finish.")
        print(f"  existing stack: {status}")
    elif "does not exist" not in stack_result.stderr:
        raise subprocess.CalledProcessError(
            stack_result.returncode,
            stack_result.args,
            output=stack_result.stdout,
            stderr=stack_result.stderr,
        )

    stack_resources: list[dict[str, Any]] = []
    if stack_exists:
        try:
            stack_resources = json.loads(
                _aws_capture(
                    [
                        "cloudformation",
                        "list-stack-resources",
                        "--region",
                        region,
                        "--stack-name",
                        stack,
                        "--output",
                        "json",
                    ]
                )
            ).get("StackResourceSummaries", [])
        except (AttributeError, json.JSONDecodeError) as exc:
            raise DeployError(f"Could not inspect existing resources for {stack}.") from exc

    existing_vpcs = sum(
        resource.get("ResourceType") == "AWS::EC2::VPC" for resource in stack_resources
    )
    if not existing_vpcs:
        try:
            quota = float(
                _aws_capture(
                    [
                        "service-quotas",
                        "get-service-quota",
                        "--service-code",
                        "vpc",
                        "--quota-code",
                        "L-F678F1CE",
                        "--region",
                        region,
                        "--query",
                        "Quota.Value",
                        "--output",
                        "text",
                    ]
                )
            )
            used = len(
                json.loads(
                    _aws_capture(
                        [
                            "ec2",
                            "describe-vpcs",
                            "--region",
                            region,
                            "--output",
                            "json",
                        ]
                    )
                ).get("Vpcs", [])
            )
            available = int(quota) - used
            if available < 1:
                raise DeployError(
                    f"{profile.name} needs one VPC, but the {region} quota is full "
                    f"({used}/{int(quota)} used). Request a quota increase or remove "
                    "an owned VPC before deploying."
                )
            print(f"  VPC quota: {available} available; 1 required by this profile")
        except (subprocess.CalledProcessError, ValueError) as exc:
            print(f"warning: could not verify VPC quota with current credentials ({exc})")

    required_eips = 2 if profile.production else (1 if profile.agentcore else 0)
    existing_eips = sum(
        resource.get("ResourceType") == "AWS::EC2::EIP" for resource in stack_resources
    )
    additional_eips = max(0, required_eips - existing_eips)
    if additional_eips:
        try:
            quota = float(
                _aws_capture(
                    [
                        "service-quotas",
                        "get-service-quota",
                        "--service-code",
                        "ec2",
                        "--quota-code",
                        "L-0263D0A3",
                        "--region",
                        region,
                        "--query",
                        "Quota.Value",
                        "--output",
                        "text",
                    ]
                )
            )
            used = len(
                json.loads(
                    _aws_capture(
                        [
                            "ec2",
                            "describe-addresses",
                            "--region",
                            region,
                            "--output",
                            "json",
                        ]
                    )
                ).get("Addresses", [])
            )
            available = int(quota) - used
            if available < additional_eips:
                raise DeployError(
                    f"{profile.name} needs {additional_eips} additional Elastic IPs for NAT "
                    f"gateways, but only {available} are available "
                    f"({used}/{int(quota)} already used)."
                )
            print(
                f"  Elastic IP quota: {available} available; "
                f"{additional_eips} additional required by this profile"
            )
        except (subprocess.CalledProcessError, ValueError) as exc:
            print(f"warning: could not verify Elastic IP quota with current credentials ({exc})")
    elif required_eips:
        print(
            f"  existing stack provides {existing_eips} Elastic IPs; no additional quota required"
        )

    if not profile.production:
        _require("docker", "Install Docker for CDK image assets.")
        if subprocess.run(
            ["docker", "buildx", "version"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode:
            raise DeployError("Docker Buildx is required to build deployment images.")
        if subprocess.run(
            ["docker", "info"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode:
            raise DeployError("Docker is installed but the Docker daemon is not running.")
        print("AWS preflight passed.")
        return

    for key, arn in config.get("secrets", {}).items():
        _aws_capture(
            [
                "secretsmanager",
                "describe-secret",
                "--region",
                region,
                "--secret-id",
                str(arn),
                "--output",
                "json",
            ]
        )
        print(f"  secret {key}: available")

    certificate_arn = config.get("domains", {}).get("certificate_arn")
    status = _aws_capture(
        [
            "acm",
            "describe-certificate",
            "--region",
            region,
            "--certificate-arn",
            str(certificate_arn),
            "--query",
            "Certificate.Status",
            "--output",
            "text",
        ]
    )
    if status != "ISSUED":
        raise DeployError(f"ACM certificate is {status}, not ISSUED.")

    for key, image in config.get("images", {}).items():
        match = re.fullmatch(
            r"\d{12}\.dkr\.ecr\.[^.]+\.amazonaws\.com/(.+)@sha256:([a-f0-9]{64})",
            str(image),
        )
        if not match:
            raise DeployError(f"images.{key} is not a digest-pinned ECR URI")
        repository, digest = match.groups()
        _aws_capture(
            [
                "ecr",
                "describe-images",
                "--region",
                region,
                "--repository-name",
                repository,
                "--image-ids",
                f"imageDigest=sha256:{digest}",
                "--output",
                "json",
            ]
        )
        print(f"  image {key}: available")
    print("AWS preflight passed.")


def aws_plan(args: argparse.Namespace) -> None:
    profile, config_path, config = _aws_config(args)
    _, cdk = _ensure_aws_tools()
    env = _cdk_environment(config_path)
    output = STATE_DIR / args.name / "cdk.out"
    _run(
        [
            str(cdk),
            "synth",
            _stack_name(config),
            "--app",
            _cdk_app(),
            "--output",
            str(output),
            "--exclusively",
        ],
        env=env,
    )
    _run(
        [
            str(cdk),
            "diff",
            _stack_name(config),
            "--app",
            _cdk_app(),
            "--exclusively",
            "--no-fail",
        ],
        env=env,
        check=False,
    )
    print(f"Plan ready for {profile.name}: {output}")


def aws_deploy(args: argparse.Namespace) -> None:
    profile, config_path, config = _aws_config(args)
    _, cdk = _ensure_aws_tools()
    env = _cdk_environment(config_path)
    _preflight(profile, config)
    if not args.no_bootstrap:
        _bootstrap(cdk, config, env)
    outputs = STATE_DIR / args.name / "outputs.json"
    command = [
        str(cdk),
        "deploy",
        _stack_name(config),
        "--app",
        _cdk_app(),
        "--exclusively",
        "--require-approval",
        "never",
        "--outputs-file",
        str(outputs),
        "--progress",
        "events",
    ]
    if profile.production:
        change_set = f"ostiari-{args.name}-{int(time.time())}"
        command.extend(
            [
                "--method",
                "prepare-change-set",
                "--change-set-name",
                change_set,
            ]
        )
        _run(command, env=env)
        details = _change_set_details(
            stack=_stack_name(config),
            change_set=change_set,
            region=args.region,
        )
        review = STATE_DIR / args.name / f"{change_set}.json"
        review.write_text(json.dumps(details, indent=2, sort_keys=True) + "\n")
        summary = _change_summary(details)
        print()
        print("Production change set prepared but not executed:")
        print(f"  Stack:      {_stack_name(config)}")
        print(f"  Change set: {change_set}")
        print(
            "  Changes:    "
            f"{summary['Add']} add, {summary['Modify']} modify, "
            f"{summary['Remove']} remove, {summary['Replace']} replace"
        )
        print(f"  Review:     {review}")
        print(
            "Review it, then run: "
            f"./deploy/ostiari aws execute --name {args.name} "
            f"--region {args.region} --change-set {change_set}"
        )
        return
    _run(command, env=env)
    values = _print_aws_outputs(outputs)
    if not args.no_verify:
        _verify_aws_outputs(values)


def _print_aws_outputs(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    values: dict[str, str] = {}
    for _, outputs in data.items():
        values.update({str(key): str(value) for key, value in outputs.items()})
        print()
        print("Ostiari is ready")
        for key in ("DashboardUrl", "GatewayUrl", "AgentCoreRuntimeArn"):
            if key in outputs:
                print(f"  {key}: {outputs[key]}")
    if "AdminSecretArn" in values:
        print(f"  Admin user: {values.get('AdminEmail', 'admin@ostiari.ai')}")
        print(
            "  Admin password: aws secretsmanager get-secret-value "
            f"--secret-id {values['AdminSecretArn']} "
            "--query SecretString --output text --no-cli-pager"
        )
    return values


def _verify_aws_outputs(outputs: dict[str, str]) -> None:
    dashboard = outputs.get("DashboardUrl")
    gateway = outputs.get("GatewayUrl")
    if not dashboard or not gateway:
        raise DeployError("Deployment outputs are missing DashboardUrl or GatewayUrl.")
    print("Verifying deployed endpoints...")
    _http_ready(f"{dashboard.rstrip('/')}/api/ready", timeout=300)
    _http_ready(f"{gateway.rstrip('/')}/ready", timeout=300)
    print("Deployment verification passed.")


def _change_set_details(*, stack: str, change_set: str, region: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            "aws",
            "cloudformation",
            "describe-change-set",
            "--region",
            region,
            "--stack-name",
            stack,
            "--change-set-name",
            change_set,
            "--include-property-values",
            "--output",
            "json",
            "--no-cli-pager",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def _change_summary(details: dict[str, Any]) -> dict[str, int]:
    summary = {"Add": 0, "Modify": 0, "Remove": 0, "Replace": 0}
    for change in details.get("Changes", []):
        resource = change.get("ResourceChange", {})
        action = str(resource.get("Action", ""))
        if action in summary:
            summary[action] += 1
        if str(resource.get("Replacement", "")) in {"True", "Conditional"}:
            summary["Replace"] += 1
    return summary


def _wait_stack(stack: str, region: str, *, timeout: int = 3600) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "aws",
                "cloudformation",
                "describe-stacks",
                "--region",
                region,
                "--stack-name",
                stack,
                "--output",
                "json",
                "--no-cli-pager",
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        current = json.loads(result.stdout)["Stacks"][0]
        status = str(current["StackStatus"])
        print(f"CloudFormation status: {status}")
        if status.endswith("_COMPLETE") and not any(
            marker in status for marker in ("ROLLBACK", "DELETE", "IMPORT_ROLLBACK")
        ):
            return current
        if any(marker in status for marker in ("FAILED", "ROLLBACK_COMPLETE", "DELETE_COMPLETE")):
            print("\nRecent CloudFormation failures:")
            _run(
                [
                    "aws",
                    "cloudformation",
                    "describe-stack-events",
                    "--region",
                    region,
                    "--stack-name",
                    stack,
                    "--query",
                    (
                        "StackEvents[?contains(ResourceStatus, 'FAILED')]."
                        "[Timestamp,LogicalResourceId,ResourceStatusReason]"
                    ),
                    "--output",
                    "table",
                    "--no-cli-pager",
                ],
                check=False,
            )
            raise DeployError(f"CloudFormation entered terminal state {status}.")
        time.sleep(15)
    raise DeployError(f"Timed out waiting for stack {stack}.")


def aws_execute(args: argparse.Namespace) -> None:
    _require("aws", "Install AWS CLI v2.")
    stack = _stack_name_for(args.name)
    details = _change_set_details(stack=stack, change_set=args.change_set, region=args.region)
    if details.get("Status") != "CREATE_COMPLETE" or details.get("ExecutionStatus") != "AVAILABLE":
        raise DeployError("Change set is not CREATE_COMPLETE/AVAILABLE.")
    replacements = _change_summary(details)["Replace"]
    if replacements and not args.allow_replacements:
        raise DeployError(
            f"Change set contains {replacements} replacement(s); review them and "
            "re-run with --allow-replacements only when intentional."
        )
    _run(
        [
            "aws",
            "cloudformation",
            "execute-change-set",
            "--region",
            args.region,
            "--stack-name",
            stack,
            "--change-set-name",
            args.change_set,
        ]
    )
    current = _wait_stack(stack, args.region)
    outputs = {item["OutputKey"]: item["OutputValue"] for item in current.get("Outputs", [])}
    output_path = STATE_DIR / args.name / "outputs.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps({stack: outputs}, indent=2, sort_keys=True) + "\n")
    values = _print_aws_outputs(output_path)
    if not args.no_verify:
        _verify_aws_outputs(values)


def aws_status(args: argparse.Namespace) -> None:
    _require("aws", "Install AWS CLI v2.")
    _run(
        [
            "aws",
            "cloudformation",
            "describe-stacks",
            "--region",
            args.region,
            "--stack-name",
            _stack_name_for(args.name),
            "--query",
            "Stacks[0].{Status:StackStatus,Outputs:Outputs}",
            "--output",
            "json",
            "--no-cli-pager",
        ]
    )


def _agentcore_network_interfaces(stack: str, region: str) -> list[dict[str, Any]]:
    try:
        resources = json.loads(
            _aws_capture(
                [
                    "cloudformation",
                    "list-stack-resources",
                    "--region",
                    region,
                    "--stack-name",
                    stack,
                    "--output",
                    "json",
                ]
            )
        ).get("StackResourceSummaries", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    security_groups = [
        str(resource.get("PhysicalResourceId", ""))
        for resource in resources
        if isinstance(resource, dict)
        and resource.get("ResourceType") == "AWS::EC2::SecurityGroup"
        and str(resource.get("LogicalResourceId", "")).startswith("AgentCoreSecurityGroup")
        and str(resource.get("PhysicalResourceId", "")).startswith("sg-")
    ]
    if not security_groups:
        return []
    try:
        interfaces = json.loads(
            _aws_capture(
                [
                    "ec2",
                    "describe-network-interfaces",
                    "--region",
                    region,
                    "--filters",
                    f"Name=group-id,Values={','.join(security_groups)}",
                    "--output",
                    "json",
                ]
            )
        ).get("NetworkInterfaces", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    return [
        interface
        for interface in interfaces
        if isinstance(interface, dict)
        and interface.get("InterfaceType") == "agentic_ai"
        and interface.get("Status") in {"available", "in-use"}
    ]


def aws_destroy(args: argparse.Namespace) -> None:
    profile = _profile(args.profile, "aws")
    if profile.production:
        raise DeployError(
            "Production destruction is intentionally not automated. Disable stack "
            "termination/deletion protection through an approved recovery procedure."
        )
    if not args.yes:
        raise DeployError("Destruction requires --yes.")
    _, config_path, config = _aws_config(args)
    _, cdk = _ensure_aws_tools()
    command = [
        str(cdk),
        "destroy",
        _stack_name(config),
        "--app",
        _cdk_app(),
        "--exclusively",
        "--force",
    ]
    try:
        _run(command, env=_cdk_environment(config_path))
    except subprocess.CalledProcessError as exc:
        if profile.agentcore:
            blockers = _agentcore_network_interfaces(_stack_name(config), args.region)
            if blockers:
                interface_ids = ", ".join(
                    str(interface["NetworkInterfaceId"]) for interface in blockers
                )
                raise DeployError(
                    "AgentCore runtime deletion is complete, but its VPC network "
                    f"interfaces are still being released: {interface_ids}. AWS can "
                    "retain these interfaces after runtime deletion; rerun this destroy "
                    "command after they disappear."
                ) from exc
        raise


def aws_bootstrap(args: argparse.Namespace) -> None:
    profile, config_path, config = _aws_config(args)
    del profile
    _, cdk = _ensure_aws_tools()
    _bootstrap(cdk, config, _cdk_environment(config_path))


def aws_preflight(args: argparse.Namespace) -> None:
    profile, _, config = _aws_config(args)
    _preflight(profile, config)


def _ensure_ecr_repository(repository: str, region: str) -> None:
    describe = subprocess.run(
        [
            "aws",
            "ecr",
            "describe-repositories",
            "--region",
            region,
            "--repository-names",
            repository,
            "--output",
            "json",
            "--no-cli-pager",
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    if describe.returncode:
        if "RepositoryNotFoundException" not in describe.stderr:
            raise subprocess.CalledProcessError(
                describe.returncode,
                describe.args,
                output=describe.stdout,
                stderr=describe.stderr,
            )
        _run(
            [
                "aws",
                "ecr",
                "create-repository",
                "--region",
                region,
                "--repository-name",
                repository,
                "--image-tag-mutability",
                "IMMUTABLE",
                "--image-scanning-configuration",
                "scanOnPush=true",
                "--encryption-configuration",
                "encryptionType=AES256",
                "--no-cli-pager",
            ]
        )
    _run(
        [
            "aws",
            "ecr",
            "put-image-tag-mutability",
            "--region",
            region,
            "--repository-name",
            repository,
            "--image-tag-mutability",
            "IMMUTABLE",
            "--no-cli-pager",
        ]
    )
    _run(
        [
            "aws",
            "ecr",
            "put-image-scanning-configuration",
            "--region",
            region,
            "--repository-name",
            repository,
            "--image-scanning-configuration",
            "scanOnPush=true",
            "--no-cli-pager",
        ]
    )
    lifecycle = subprocess.run(
        [
            "aws",
            "ecr",
            "get-lifecycle-policy",
            "--region",
            region,
            "--repository-name",
            repository,
            "--output",
            "json",
            "--no-cli-pager",
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    if lifecycle.returncode == 0:
        return
    if "LifecyclePolicyNotFoundException" not in lifecycle.stderr:
        raise subprocess.CalledProcessError(
            lifecycle.returncode,
            lifecycle.args,
            output=lifecycle.stdout,
            stderr=lifecycle.stderr,
        )
    policy = json.dumps(
        {
            "rules": [
                {
                    "rulePriority": 1,
                    "description": "Retain the newest 20 release images",
                    "selection": {
                        "tagStatus": "any",
                        "countType": "imageCountMoreThan",
                        "countNumber": 20,
                    },
                    "action": {"type": "expire"},
                }
            ]
        },
        separators=(",", ":"),
    )
    _run(
        [
            "aws",
            "ecr",
            "put-lifecycle-policy",
            "--region",
            region,
            "--repository-name",
            repository,
            "--lifecycle-policy-text",
            policy,
            "--no-cli-pager",
        ]
    )


def _published_digest(repository: str, tag: str, region: str) -> str:
    return _aws_capture(
        [
            "ecr",
            "describe-images",
            "--region",
            region,
            "--repository-name",
            repository,
            "--image-ids",
            f"imageTag={tag}",
            "--query",
            "imageDetails[0].imageDigest",
            "--output",
            "text",
        ]
    )


def _ensure_buildx_publisher() -> str:
    name = "ostiari-publisher"
    inspected = subprocess.run(
        ["docker", "buildx", "inspect", name],
        check=False,
        text=True,
        capture_output=True,
    )
    if inspected.returncode:
        _run(
            [
                "docker",
                "buildx",
                "create",
                "--name",
                name,
                "--driver",
                "docker-container",
            ]
        )
    else:
        driver = re.search(r"^Driver:\s+(\S+)", inspected.stdout, re.MULTILINE)
        if driver is None or driver.group(1) != "docker-container":
            raise DeployError(
                f"Buildx builder {name!r} exists but does not use the "
                "docker-container driver required for SBOM and provenance attestations."
            )
    _run(["docker", "buildx", "inspect", name, "--bootstrap"])
    return name


def aws_publish_images(args: argparse.Namespace) -> None:
    _require("aws", "Install AWS CLI v2 and authenticate.")
    _require("docker", "Install Docker with Buildx.")
    if subprocess.run(
        ["docker", "buildx", "version"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode:
        raise DeployError("Docker Buildx is required.")
    if subprocess.run(
        ["docker", "info"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode:
        raise DeployError("Docker is installed but the Docker daemon is not running.")
    builder = _ensure_buildx_publisher()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    if dirty and not args.allow_dirty:
        raise DeployError(
            "Refusing to publish images from a dirty worktree. Commit the release "
            "or pass --allow-dirty for an explicitly non-release build."
        )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    if dirty and not args.tag:
        raise DeployError("--allow-dirty requires an explicit, unique --tag.")
    tag = args.tag or commit
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
        raise DeployError(f"{tag!r} is not a valid ECR image tag.")
    identity = _aws_identity(args.region)
    account = identity["Account"]
    registry = f"{account}.dkr.ecr.{args.region}.amazonaws.com"

    password = _aws_capture(["ecr", "get-login-password", "--region", args.region])
    print(f"+ docker login --username AWS --password-stdin {registry}")
    subprocess.run(
        ["docker", "login", "--username", "AWS", "--password-stdin", registry],
        input=password,
        text=True,
        check=True,
    )

    prefix = args.repository_prefix.strip("/")
    images: list[tuple[str, str, str, str, list[str]]] = [
        (
            "control_plane",
            f"{prefix}/control-plane",
            "deploy/docker/Dockerfile.control-plane",
            "linux/amd64",
            [],
        ),
        (
            "gateway",
            f"{prefix}/gateway",
            "deploy/docker/Dockerfile.gateway",
            "linux/amd64",
            [],
        ),
        (
            "frontend",
            f"{prefix}/frontend",
            "deploy/docker/Dockerfile.frontend",
            "linux/amd64",
            ["--build-arg", "VITE_API_URL="],
        ),
    ]
    if args.include_agentcore:
        images.append(
            (
                "agentcore",
                f"{prefix}/agentcore",
                "deploy/agentcore/Dockerfile",
                "linux/arm64",
                [],
            )
        )

    existing_tags: dict[str, bool] = {}
    for _, repository, _, _, _ in images:
        _ensure_ecr_repository(repository, args.region)
        existing = subprocess.run(
            [
                "aws",
                "ecr",
                "describe-images",
                "--region",
                args.region,
                "--repository-name",
                repository,
                "--image-ids",
                f"imageTag={tag}",
                "--output",
                "json",
                "--no-cli-pager",
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        if existing.returncode and "ImageNotFoundException" not in existing.stderr:
            raise subprocess.CalledProcessError(
                existing.returncode,
                existing.args,
                output=existing.stdout,
                stderr=existing.stderr,
            )
        existing_tags[repository] = existing.returncode == 0

    collisions = [repository for repository, exists in existing_tags.items() if exists]
    if collisions and tag != commit:
        repositories = ", ".join(collisions)
        raise DeployError(
            f"Custom tag {tag!r} already exists in: {repositories}. "
            "Use a unique custom tag or the default commit-SHA tag."
        )

    published: dict[str, str] = {}
    for key, repository, dockerfile, platform, extra in images:
        if existing_tags[repository]:
            print(f"  {repository}:{tag} already exists; reusing immutable image")
        else:
            _run(
                [
                    "docker",
                    "buildx",
                    "build",
                    "--builder",
                    builder,
                    "--platform",
                    platform,
                    "--file",
                    dockerfile,
                    "--tag",
                    f"{registry}/{repository}:{tag}",
                    "--label",
                    f"org.opencontainers.image.revision={commit}",
                    "--label",
                    "org.opencontainers.image.source=https://github.com/hk-775/ostiari",
                    "--label",
                    f"org.opencontainers.image.version={tag}",
                    "--provenance=mode=max",
                    "--sbom=true",
                    "--push",
                    *extra,
                    ".",
                ]
            )
        digest = _published_digest(repository, tag, args.region)
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise DeployError(f"ECR returned an invalid digest for {repository}: {digest}")
        published[key] = f"{registry}/{repository}@{digest}"
        print(f"  {key}: {published[key]}")

    directory = STATE_DIR / args.name
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "images.json"
    output.write_text(
        json.dumps(
            {
                "account": account,
                "region": args.region,
                "release_commit": commit,
                "dirty_source": bool(dirty),
                "tag": tag,
                "images": published,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    print(f"Production image manifest: {output}")


def profiles(_: argparse.Namespace) -> None:
    width = max(len(name) for name in PROFILES)
    for profile in PROFILES.values():
        print(f"{profile.name:<{width}}  {profile.description}")


def _local_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", choices=("local-demo", "local-empty"), default="local-demo")
    parser.add_argument("--gateway-port", type=int, default=8421)
    parser.add_argument("--control-plane-port", type=int, default=8400)
    parser.add_argument("--frontend-port", type=int, default=9000)
    parser.add_argument("--demo-tools-port", type=int, default=9300)
    parser.add_argument("--redis-port", type=int, default=6379)


def _aws_options(parser: argparse.ArgumentParser, *, profile: bool = True) -> None:
    if profile:
        parser.add_argument(
            "--profile",
            choices=tuple(name for name, value in PROFILES.items() if value.target == "aws"),
            default="aws-demo",
        )
    parser.add_argument("--name", default="dev")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--allowed-cidr", default="auto")
    parser.add_argument("--config", help="JSON config; required for production")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="deploy/ostiari")
    commands = root.add_subparsers(dest="command", required=True)

    show = commands.add_parser("profiles", help="List deployment profiles")
    show.set_defaults(handler=profiles)

    local = commands.add_parser("local", help="Run Ostiari on this machine")
    local_commands = local.add_subparsers(dest="local_command", required=True)
    for name, handler in (
        ("up", local_up),
        ("down", local_down),
        ("status", local_status),
        ("logs", local_logs),
    ):
        item = local_commands.add_parser(name)
        _local_options(item)
        item.set_defaults(handler=handler)
        if name == "down":
            item.add_argument("--purge", action="store_true")
        if name == "logs":
            item.add_argument("--follow", action="store_true")
            item.add_argument("--tail", type=int, default=200)

    aws = commands.add_parser("aws", help="Plan and operate an AWS deployment")
    aws_commands = aws.add_subparsers(dest="aws_command", required=True)
    for name, handler in (
        ("plan", aws_plan),
        ("deploy", aws_deploy),
        ("destroy", aws_destroy),
    ):
        item = aws_commands.add_parser(name)
        _aws_options(item)
        item.set_defaults(handler=handler)
        if name == "deploy":
            item.add_argument("--no-bootstrap", action="store_true")
            item.add_argument("--no-verify", action="store_true")
        if name == "destroy":
            item.add_argument("--yes", action="store_true")
    bootstrap = aws_commands.add_parser("bootstrap")
    _aws_options(bootstrap)
    bootstrap.set_defaults(handler=aws_bootstrap)
    preflight = aws_commands.add_parser("preflight")
    _aws_options(preflight)
    preflight.set_defaults(handler=aws_preflight)
    publish = aws_commands.add_parser("publish-images")
    publish.add_argument("--name", default="production")
    publish.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    publish.add_argument("--repository-prefix", default="ostiari")
    publish.add_argument("--tag")
    publish.add_argument("--include-agentcore", action="store_true")
    publish.add_argument("--allow-dirty", action="store_true")
    publish.set_defaults(handler=aws_publish_images)
    execute = aws_commands.add_parser("execute")
    _aws_options(execute, profile=False)
    execute.add_argument("--change-set", required=True)
    execute.add_argument("--allow-replacements", action="store_true")
    execute.add_argument("--no-verify", action="store_true")
    execute.set_defaults(handler=aws_execute)
    status = aws_commands.add_parser("status")
    _aws_options(status, profile=False)
    status.set_defaults(handler=aws_status)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        args.handler(args)
    except (DeployError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
