"""Contracts tying advertised Ostiari capabilities to executable evidence."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MATRIX_PATH = ROOT / "config" / "feature-test-matrix.json"

CAPABILITY_AREAS = {
    "Runtime decisions",
    "Reliability",
    "Tool governance",
    "LLM governance",
    "LLM-compatible APIs",
    "LLM security",
    "MCP",
    "A2A",
    "Human approval",
    "Cost control",
    "Payments",
    "Observability",
    "Fleet operations",
    "Administration",
    "Reporting",
    "Deployment",
}
EXTRA_AREAS = {
    "Frontend operator workflows",
    "Persistence, scaling, and tenancy",
    "Codex client compatibility",
    "Production rehearsal evidence",
}
LIVE_EVIDENCE_KINDS = {
    "scans",
    "load",
    "backup_restore",
    "rollback",
    "alarm",
    "canary",
    "payment",
}
FRONTEND_TESTS = {
    "control-plane/frontend/tests/api.test.ts",
    "control-plane/frontend/tests/authStore.test.ts",
    "control-plane/frontend/tests/efficiency.test.ts",
    "control-plane/frontend/tests/layout.test.tsx",
    "control-plane/frontend/tests/sandboxRunner.test.ts",
    "control-plane/frontend/tests/sso.test.ts",
}


def _matrix() -> dict[str, object]:
    return json.loads(MATRIX_PATH.read_text())


def test_every_advertised_capability_has_executable_evidence() -> None:
    matrix = _matrix()
    assert matrix["schema_version"] == 1
    capabilities = matrix["capabilities"]
    assert isinstance(capabilities, list)

    ids = [entry["id"] for entry in capabilities]
    areas = {entry["area"] for entry in capabilities}
    feature_document = (ROOT / "docs/features-and-flows.md").read_text()
    summary = feature_document.split("## 2. Capability Summary", 1)[1].split(
        "## 3. Embedded Guard Flow",
        1,
    )[0]
    documented_areas = {
        cells[0]
        for line in summary.splitlines()
        if line.startswith("| ")
        and not line.startswith("| Area ")
        and not line.startswith("|---")
        and len(cells := [cell.strip() for cell in line.strip("|").split("|")]) == 3
    }

    assert len(ids) == len(set(ids))
    assert documented_areas == CAPABILITY_AREAS
    assert areas == CAPABILITY_AREAS | EXTRA_AREAS

    for entry in capabilities:
        automated_tests = entry["automated_tests"]
        protected_checks = entry["protected_checks"]
        live_evidence = set(entry["live_evidence"])
        assert automated_tests, f"{entry['id']} has no automated tests"
        assert protected_checks, f"{entry['id']} has no protected check"
        assert live_evidence <= LIVE_EVIDENCE_KINDS
        for relative in [*automated_tests, *protected_checks]:
            assert (ROOT / relative).is_file(), (
                f"{entry['id']} references missing {relative}"
            )


def test_frontend_behavior_is_fully_represented_in_the_matrix() -> None:
    matrix = _matrix()
    referenced = {
        test
        for entry in matrix["capabilities"]
        for test in entry["automated_tests"]
        if test.startswith("control-plane/frontend/tests/")
    }
    assert referenced == FRONTEND_TESTS


def test_live_evidence_matrix_matches_the_retained_rehearsal_schema() -> None:
    matrix = _matrix()
    rehearsal = next(
        entry
        for entry in matrix["capabilities"]
        if entry["id"] == "production_rehearsal"
    )
    assert set(rehearsal["live_evidence"]) == LIVE_EVIDENCE_KINDS


def test_ci_enforces_frontend_behavior_and_root_coverage() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "run: npm test" in workflow
    assert "--cov=ostiari" in workflow
    assert "--cov-fail-under=70" in workflow
