"""Contracts for the adopter-facing deployment matrix."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


deploy = _module("ostiari_deploy_test", ROOT / "deploy/ostiari_deploy.py")
aws_config = _module("ostiari_aws_config_test", ROOT / "deploy/aws/config.py")


def test_all_requested_deployment_profiles_are_exposed() -> None:
    assert set(deploy.PROFILES) == {
        "local-demo",
        "local-empty",
        "aws-demo",
        "aws-empty",
        "aws-agentcore-demo",
        "aws-agentcore-empty",
        "production",
        "production-agentcore",
    }
    assert deploy.PROFILES["local-demo"].demo
    assert not deploy.PROFILES["local-empty"].demo
    assert deploy.PROFILES["aws-agentcore-demo"].agentcore
    assert deploy.PROFILES["production"].production
    assert deploy.PROFILES["production-agentcore"].agentcore
    assert not deploy.PROFILES["production-agentcore"].demo


def test_local_demo_overlay_is_idempotent_and_health_gated() -> None:
    base = yaml.safe_load((ROOT / "deploy/docker/docker-compose.yml").read_text())
    demo = yaml.safe_load((ROOT / "deploy/docker/docker-compose.demo.yml").read_text())
    assert {"gateway", "control-plane-backend", "control-plane-frontend", "redis"} <= set(
        base["services"]
    )
    assert {"demo-tools", "demo-seed"} == set(demo["services"])
    seed = demo["services"]["demo-seed"]
    assert seed["restart"] == "no"
    assert seed["build"] == demo["services"]["demo-tools"]["build"]
    assert all(value["condition"] == "service_healthy" for value in seed["depends_on"].values())


def test_production_configuration_rejects_mutable_images_and_partial_secrets() -> None:
    raw = {
        "name": "production",
        "profile": "production",
        "account": "123456789012",
        "region": "us-east-1",
        "allowed_cidr": "203.0.113.10/32",
        "demo": False,
        "agentcore": False,
        "production": True,
        "images": {
            "control_plane": "example/control:latest",
            "gateway": "example/gateway:latest",
            "frontend": "example/frontend:latest",
        },
        "domains": {},
        "auth": {},
        "secrets": {},
    }
    with pytest.raises(aws_config.ConfigurationError, match="same-account"):
        aws_config.DeploymentConfig.from_dict(raw)


def test_aws_configuration_requires_a_path_and_valid_cidr(monkeypatch) -> None:
    monkeypatch.delenv("OSTIARI_DEPLOY_CONFIG", raising=False)
    with pytest.raises(aws_config.ConfigurationError, match="required"):
        aws_config.DeploymentConfig.load()

    raw = {
        "name": "evaluation",
        "profile": "aws-empty",
        "account": "123456789012",
        "region": "us-east-1",
        "allowed_cidr": "not-a-cidr",
        "demo": False,
        "agentcore": False,
        "production": False,
    }
    with pytest.raises(aws_config.ConfigurationError, match="invalid allowed CIDR"):
        aws_config.DeploymentConfig.from_dict(raw)


def test_aws_configuration_enforces_profile_and_production_redundancy() -> None:
    raw = {
        "name": "evaluation",
        "profile": "aws-empty",
        "account": "123456789012",
        "region": "us-east-1",
        "allowed_cidr": "203.0.113.10/32",
        "demo": True,
        "agentcore": False,
        "production": False,
    }
    with pytest.raises(aws_config.ConfigurationError, match="does not match"):
        aws_config.DeploymentConfig.from_dict(raw)

    raw.update(
        {
            "profile": "production",
            "demo": False,
            "production": True,
            "desired_count": 1,
            "images": {},
            "domains": {},
            "auth": {},
            "secrets": {},
        }
    )
    with pytest.raises(aws_config.ConfigurationError, match="at least 2"):
        aws_config.DeploymentConfig.from_dict(raw)


def test_cli_validates_config_before_persisting(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(deploy, "STATE_DIR", tmp_path)
    monkeypatch.setattr(deploy, "_aws_identity", lambda region: {"Account": "123456789012"})
    args = deploy.parser().parse_args(
        [
            "aws",
            "preflight",
            "--profile",
            "aws-empty",
            "--name",
            "invalid",
            "--allowed-cidr",
            "not-a-cidr",
        ]
    )
    with pytest.raises(deploy.DeployError, match="invalid allowed CIDR"):
        deploy._aws_config(args)
    assert not (tmp_path / "invalid" / "config.json").exists()


def test_preflight_rejects_failed_stack_before_other_checks(monkeypatch) -> None:
    result = deploy.subprocess.CompletedProcess(
        args=["aws"],
        returncode=0,
        stdout='{"Stacks":[{"StackStatus":"ROLLBACK_COMPLETE"}]}',
        stderr="",
    )
    monkeypatch.setattr(deploy.subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(deploy.DeployError, match="recover or delete"):
        deploy._preflight(
            deploy.PROFILES["aws-empty"],
            {"name": "failed", "region": "us-east-1"},
        )


def test_change_set_summary_counts_conditional_replacements() -> None:
    summary = deploy._change_summary(
        {
            "Changes": [
                {"ResourceChange": {"Action": "Add", "Replacement": "False"}},
                {"ResourceChange": {"Action": "Modify", "Replacement": "Conditional"}},
                {"ResourceChange": {"Action": "Remove", "Replacement": "False"}},
            ]
        }
    )
    assert summary == {"Add": 1, "Modify": 1, "Remove": 1, "Replace": 1}


def test_aws_frontend_uses_explicit_same_origin_build_contract() -> None:
    api = (ROOT / "control-plane/frontend/src/lib/api.ts").read_text()
    stack = (ROOT / "deploy/aws/stack.py").read_text()
    dockerfile = (ROOT / "deploy/docker/Dockerfile.frontend").read_text()
    assert "configuredApiBase === undefined" in api
    assert 'build_args={"VITE_API_URL": ""}' in stack
    assert '["/api/*", "/docs*", "/openapi.json", "/ws/*"]' in stack
    assert "frontend-tmp" not in stack
    assert "pid /dev/shm/nginx.pid;" in dockerfile
    assert "client_body_temp_path /dev/shm/client_temp;" in dockerfile


def test_gateway_uses_runtime_tmpfs_without_platform_volumes() -> None:
    stack = (ROOT / "deploy/aws/stack.py").read_text()
    dockerfile = (ROOT / "deploy/docker/Dockerfile.gateway").read_text()
    compose = yaml.safe_load(
        (ROOT / "deploy/docker/docker-compose.yml").read_text()
    )
    ecs = yaml.safe_load((ROOT / "deploy/ecs/task-definition.json").read_text())

    assert "ENV TMPDIR=/dev/shm" in dockerfile
    assert "gateway-tmp" not in stack
    assert "tmpfs" not in compose["services"]["gateway"]
    assert "mountPoints" not in ecs["containerDefinitions"][0]
    assert "volumes" not in ecs


def test_empty_aws_profiles_do_not_create_demo_infrastructure() -> None:
    stack = (ROOT / "deploy/aws/stack.py").read_text()
    validator = (ROOT / "deploy/aws/validate.py").read_text()

    assert "self.demo_sg: ec2.SecurityGroup | None = None" in stack
    assert "if self.config.demo:" in stack
    assert "assert bool(demo_resources) is demo" in validator


def test_operational_aws_subcommands_are_exposed() -> None:
    parsed = deploy.parser().parse_args(["aws", "preflight"])
    assert parsed.handler is deploy.aws_preflight
    parsed = deploy.parser().parse_args(["aws", "publish-images"])
    assert parsed.handler is deploy.aws_publish_images
    parsed = deploy.parser().parse_args(["aws", "execute", "--change-set", "reviewed-change-set"])
    assert parsed.handler is deploy.aws_execute


def test_local_readiness_failure_prints_compose_diagnostics(monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        deploy,
        "_compose",
        lambda profile, args: (["docker", "compose", "--project-name", "test"], {}),
    )
    monkeypatch.setattr(
        deploy,
        "_run",
        lambda command, **kwargs: commands.append(list(command)),
    )
    monkeypatch.setattr(
        deploy,
        "_http_ready",
        lambda url: (_ for _ in ()).throw(deploy.DeployError("not ready")),
    )
    args = deploy.parser().parse_args(["local", "up", "--profile", "local-empty"])

    with pytest.raises(deploy.DeployError, match="not ready"):
        deploy.local_up(args)

    assert commands[-2][-2:] == ["ps", "--all"]
    assert commands[-1][-3:] == ["logs", "--tail", "120"]
