"""Release-set contracts for the public Python distributions."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_publish_workflow_builds_and_verifies_complete_platform_set() -> None:
    workflow = (ROOT / ".github/workflows/publish.yml").read_text()

    assert "actions/checkout@v6" in workflow
    assert "actions/setup-python@v6" in workflow
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
