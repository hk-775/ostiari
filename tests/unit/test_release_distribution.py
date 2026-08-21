"""Release-set contracts for the public Python distributions."""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"[0-9a-f]{40}$")


def test_publish_workflow_builds_and_verifies_complete_platform_set() -> None:
    workflow = (ROOT / ".github/workflows/publish.yml").read_text()

    assert "python -m build --outdir \"$GITHUB_WORKSPACE/release-dist\" ." in workflow
    assert (
        "python -m build --outdir \"$GITHUB_WORKSPACE/release-dist\" "
        "vendor/axonllm"
    ) in workflow
    assert (
        "python -m build --outdir \"$GITHUB_WORKSPACE/release-dist\" gateway"
    ) in workflow
    assert (
        "python -m build --outdir \"$GITHUB_WORKSPACE/release-dist\" "
        "control-plane/backend"
    ) in workflow
    assert "/tmp/ostiari-release-verify/bin/pip install release-dist/*.whl" in workflow
    assert "packages-dir: release-dist/" in workflow
    assert "release-dist/* python-sbom.cdx.json --clobber" in workflow


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


def test_container_build_and_local_state_images_are_digest_pinned() -> None:
    dockerfiles = sorted((ROOT / "deploy/docker").glob("Dockerfile*"))
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
