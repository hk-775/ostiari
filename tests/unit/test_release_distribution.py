"""Release-set contracts for the public Python distributions."""

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

from tools.check_release_versions import _module_version, _semver

ROOT = Path(__file__).resolve().parents[2]
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"[0-9a-f]{40}$")


def test_publish_workflow_builds_and_verifies_complete_platform_set() -> None:
    workflow = (ROOT / ".github/workflows/publish.yml").read_text()

    assert "python -m build --outdir \"$GITHUB_WORKSPACE/release-dist\" ." in workflow
    assert (
        "python -m build --outdir \"$GITHUB_WORKSPACE/companion-dist\" "
        "vendor/axonllm"
    ) in workflow
    assert (
        "python -m build --outdir \"$GITHUB_WORKSPACE/release-dist\" gateway"
    ) in workflow
    assert (
        "python -m build --outdir \"$GITHUB_WORKSPACE/release-dist\" "
        "control-plane/backend"
    ) in workflow
    assert "release-dist/*.whl companion-dist/*.whl" in workflow
    assert (
        'python tools/check_release_versions.py \\\n'
        '            --release-tag "$GITHUB_REF_NAME"'
    ) in workflow
    assert "name: Publish bundled AxonLLM distribution" in workflow
    assert "packages-dir: companion-dist/" in workflow
    assert "packages-dir: release-dist/" in workflow
    assert workflow.count("pypa/gh-action-pypi-publish@") == 2
    assert workflow.index("packages-dir: companion-dist/") < workflow.index(
        "packages-dir: release-dist/"
    )
    assert (
        "release-dist/* companion-dist/* python-sbom.cdx.json --clobber"
        in workflow
    )


def test_release_tag_contract_uses_the_exact_pep440_version() -> None:
    version = _module_version("src/ostiari/__init__.py")
    valid = subprocess.run(
        [
            sys.executable,
            "tools/check_release_versions.py",
            "--release-tag",
            f"v{version}",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert valid.returncode == 0, valid.stdout + valid.stderr

    semver = _semver(version)
    invalid_tag = f"v{semver}" if semver != version else f"v{version}-unexpected"
    invalid = subprocess.run(
        [
            sys.executable,
            "tools/check_release_versions.py",
            "--release-tag",
            invalid_tag,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode == 1
    assert f"expected 'v{version}'" in invalid.stdout


def test_external_github_actions_are_pinned_to_commit_shas() -> None:
    workflow_paths = sorted((ROOT / ".github/workflows").glob("*.y*ml"))
    assert workflow_paths

    for path in workflow_paths:
        for line_number, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if "uses:" not in stripped:
                continue
            action = stripped.split("uses:", 1)[1].split("#", 1)[0].strip()
            if action.startswith("./"):
                continue
            assert "@" in action, f"{path}:{line_number}: missing action ref"
            ref = action.rsplit("@", 1)[1]
            assert COMMIT_RE.fullmatch(ref), (
                f"{path}:{line_number}: external action is not commit-pinned"
            )
            assert "#" in line, f"{path}:{line_number}: missing readable version comment"


def test_official_image_signing_is_keyless_release_bound_and_scoped() -> None:
    workflow_path = ROOT / ".github/workflows/sign-official-images.yml"
    workflow = yaml.safe_load(workflow_path.read_text())
    text = workflow_path.read_text()
    job = workflow["jobs"]["sign"]

    assert workflow["permissions"] == {"contents": "write", "id-token": "write"}
    assert job["environment"] == "production-signing"
    assert "test \"$GITHUB_REF\" = \"refs/heads/main\"" in text
    assert (
        'python tools/check_release_versions.py --release-tag "$RELEASE_TAG"'
        in text
    )
    assert "imageTag=${RELEASE_SHA}" in text
    assert "test \"$actual\" = \"$digest\"" in text
    assert "cosign sign --yes" in text
    assert "--bundle \"signing-evidence/${name}.sigstore.json\"" in text
    assert "--certificate-identity \"$certificate_identity\"" in text
    assert (
        "--certificate-oidc-issuer "
        "\"https://token.actions.githubusercontent.com\""
    ) in text
    assert "verificationMaterial.tlogEntries" in text
    assert "retention-days: 90" in text
    assert "gh release upload \"$RELEASE_TAG\" signing-evidence/*" in text

    template = json.loads(
        (ROOT / "deploy/aws/release-signing-role.json").read_text()
    )
    role = template["Resources"]["ImageSignerRole"]["Properties"]
    trust = role["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert trust["Condition"]["StringEquals"][
        "token.actions.githubusercontent.com:aud"
    ] == "sts.amazonaws.com"
    assert trust["Condition"]["StringEquals"][
        "token.actions.githubusercontent.com:sub"
    ] == {
        "Fn::Sub": (
            "repo:${GitHubRepository}:environment:${GitHubEnvironment}"
        )
    }
    policy = role["Policies"][0]["PolicyDocument"]["Statement"]
    assert policy[0] == {
        "Sid": "RegistryLogin",
        "Effect": "Allow",
        "Action": "ecr:GetAuthorizationToken",
        "Resource": "*",
    }
    repository_resources = policy[1]["Resource"]
    assert len(repository_resources) == 4
    assert all(
        resource["Fn::Sub"].startswith(
            "arn:${AWS::Partition}:ecr:${AWS::Region}:"
            "${AWS::AccountId}:repository/${RepositoryPrefix}/"
        )
        for resource in repository_resources
    )
    assert not any(
        statement.get("Action") == "*"
        for statement in policy
    )


def test_container_build_and_local_state_images_are_digest_pinned() -> None:
    dockerfiles = sorted((ROOT / "deploy/docker").glob("Dockerfile*"))
    dockerfiles.append(ROOT / "deploy/agentcore/Dockerfile")
    assert dockerfiles
    for path in dockerfiles:
        for line_number, line in enumerate(path.read_text().splitlines(), start=1):
            if not line.startswith("FROM "):
                continue
            image = line.split()[1]
            assert "@sha256:" in image, f"{path}:{line_number}: mutable base image"
            assert SHA256_RE.search(image), f"{path}:{line_number}: malformed digest"

    compose = yaml.safe_load((ROOT / "deploy/docker/docker-compose.yml").read_text())
    external_images = [
        service["image"]
        for service in compose["services"].values()
        if "image" in service and not service["image"].startswith("ostiari-")
    ]
    assert external_images
    for image in external_images:
        assert "@sha256:" in image, f"mutable external Compose image: {image}"
        assert SHA256_RE.search(image), f"malformed Compose image digest: {image}"

    assert external_images == [
        "valkey/valkey:9.0.5-alpine@sha256:"
        "0cb61366757e2bcd26500b4e8bb63cbd7117610e3e4f05aacb3c812511da7632"
    ]

    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    frontend = (ROOT / "deploy/docker/Dockerfile.frontend").read_text()
    assert 'node-version: "24.16.0"' in ci
    assert "FROM node:24.16.0-alpine@sha256:" in frontend
    assert (
        "FROM golang:1.27.0-alpine@sha256:"
        "4c9fe60190a2a3350ddc51de80d0224b8a6698d12bdfc999fee45ea9d6c46dbc"
        in frontend
    )
    assert (
        "FROM gcr.io/distroless/static-debian13:nonroot@sha256:"
        "1c2c046bc09ed40fad370b599a0b1ae7987f55b01e247cf27a7c27cd97e5bbc7"
        in frontend
    )
    assert "FROM scratch" not in frontend


def test_make_install_covers_the_complete_source_platform() -> None:
    makefile = (ROOT / "Makefile").read_text()

    assert 'pip install -e ".[all,dev]"' in makefile
    assert 'pip install -e "$(AXON_ROOT)[server]"' in makefile
    assert 'pip install -e "gateway[payments,redis]"' in makefile
    assert 'pip install -e "control-plane/backend[aws,dev,otlp]"' in makefile
    assert "cd control-plane/frontend && npm ci" in makefile


def test_production_templates_use_per_gateway_workload_identity() -> None:
    kubernetes = [
        ROOT / "deploy/kubernetes/gateway-shared.yaml",
        ROOT / "deploy/kubernetes/gateway-sidecar.yaml",
        ROOT / "deploy/kubernetes/control-plane.yaml",
    ]
    for path in kubernetes:
        list(yaml.safe_load_all(path.read_text()))
        text = path.read_text()
        assert "OSTIARI_SERVICE_TOKEN" not in text
        assert "OSTIARI_INGEST_KEY" not in text

    gateway_templates = kubernetes[:2]
    for path in gateway_templates:
        text = path.read_text()
        assert "OSTIARI_WORKLOAD_TOKEN_URL" in text
        assert "OSTIARI_WORKLOAD_CLIENT_ID" in text
        assert "OSTIARI_WORKLOAD_CLIENT_SECRET" in text

    control_plane = kubernetes[2].read_text()
    assert "OSTIARI_WORKLOAD_OIDC_ISSUER" in control_plane
    assert "OSTIARI_WORKLOAD_OIDC_AUDIENCE" in control_plane
    documents = list(yaml.safe_load_all(control_plane))
    deployment = next(
        document
        for document in documents
        if document.get("kind") == "Deployment"
    )
    assert deployment["spec"]["replicas"] == 2
    containers = deployment["spec"]["template"]["spec"]["containers"]
    backend = next(
        container for container in containers if container["name"] == "backend"
    )
    backend_env = {item["name"]: item for item in backend["env"]}
    assert backend_env["OSTIARI_CONTROL_PLANE_REPLICAS"]["value"] == "2"
    assert backend_env["OSTIARI_TENANCY_MODE"]["value"] == "multi"
    assert (
        backend_env["REDIS_URL"]["valueFrom"]["secretKeyRef"]["key"]
        == "redis-url"
    )
    assert any(
        document.get("kind") == "PodDisruptionBudget"
        and document["spec"]["minAvailable"] == 1
        for document in documents
    )

    task_definition = json.loads(
        (ROOT / "deploy/ecs/task-definition.json").read_text()
    )
    container = task_definition["containerDefinitions"][0]
    environment_names = {
        item["name"] for item in container["environment"]
    }
    secret_names = {item["name"] for item in container["secrets"]}
    assert {
        "OSTIARI_WORKLOAD_TOKEN_URL",
        "OSTIARI_WORKLOAD_CLIENT_ID",
    } <= environment_names
    assert "OSTIARI_WORKLOAD_CLIENT_SECRET" in secret_names
    assert not {
        "OSTIARI_SERVICE_TOKEN",
        "OSTIARI_INGEST_KEY",
    } & (environment_names | secret_names)

    chart = (
        ROOT / "deploy/helm/ostiari-gateway/templates/deployment.yaml"
    ).read_text()
    assert "OSTIARI_WORKLOAD_CLIENT_SECRET" in chart
    assert "OSTIARI_SERVICE_TOKEN" not in chart
    assert "OSTIARI_INGEST_KEY" not in chart
