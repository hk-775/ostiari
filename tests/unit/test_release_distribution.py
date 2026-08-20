"""Release-set contracts for the public Python distributions."""

from pathlib import Path

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
