"""Contracts for release-bound retained production evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tools.production_evidence import EvidenceError, assemble

SHA = "a" * 40
GATEWAY = "sha256:" + "b" * 64
CONTROL_PLANE = "sha256:" + "c" * 64
TAG = "v1.2.3"
ENVIRONMENT = "production-rehearsal"


def _identity() -> dict[str, str]:
    return {
        "sha": SHA,
        "tag": TAG,
        "gateway_image_digest": GATEWAY,
        "control_plane_image_digest": CONTROL_PLANE,
    }


def _data() -> dict[str, dict[str, object]]:
    return {
        "scans": {
            "critical_findings": 0,
            "high_findings": 0,
            "sbom_sha256": ["sha256:" + "d" * 64],
        },
        "load": {
            "requests": 1000,
            "error_rate": 0.001,
            "max_error_rate": 0.01,
            "p99_ms": 250,
            "max_p99_ms": 500,
            "healthy_replicas": 2,
        },
        "backup_restore": {
            "isolated_restore": True,
            "source_schema_sha256": "schema",
            "restored_schema_sha256": "schema",
            "source_data_sha256": "data",
            "restored_data_sha256": "data",
        },
        "rollback": {
            "candidate_digest": "sha256:" + "e" * 64,
            "approved_digest": GATEWAY,
            "restored_digest": GATEWAY,
            "automatic": True,
            "healthy_replicas": 2,
        },
        "alarm": {
            "alarm_name": "ostiari-rehearsal",
            "alarm_state": "ALARM",
            "delivery_receipt": "notification-1",
            "delivered_at": datetime.now(UTC).isoformat(),
        },
        "canary": {
            "checks": {
                "gateway_authenticated": True,
                "control_plane_authenticated": True,
                "governed_call": True,
            },
            "credentials_redacted": True,
        },
        "payment": {
            "mode": "live",
            "settled": True,
            "amount_usdc": 0.01,
            "cap_usdc": 0.05,
            "transaction_reference": "0x123",
        },
    }


def _write_evidence(directory: Path) -> None:
    completed = datetime.now(UTC)
    started = completed - timedelta(minutes=5)
    for kind, data in _data().items():
        document = {
            "schema_version": 1,
            "kind": kind,
            "status": "passed",
            "release": _identity(),
            "environment": ENVIRONMENT,
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
            "data": data,
        }
        (directory / f"{kind}.json").write_text(json.dumps(document))


def _args(directory: Path, output: Path) -> SimpleNamespace:
    return SimpleNamespace(
        evidence_dir=str(directory),
        release_sha=SHA,
        release_tag=TAG,
        gateway_digest=GATEWAY,
        control_plane_digest=CONTROL_PLANE,
        environment=ENVIRONMENT,
        max_age_hours=24,
        output=str(output),
    )


def test_assemble_accepts_complete_release_bound_evidence(tmp_path: Path) -> None:
    _write_evidence(tmp_path)
    output = tmp_path / "retained" / "manifest.json"

    assemble(_args(tmp_path, output))

    manifest = json.loads(output.read_text())
    assert manifest["status"] == "passed"
    assert manifest["release"] == _identity()
    assert set(manifest["evidence"]) == set(_data())
    for entry in manifest["evidence"].values():
        assert len(entry["sha256"]) == 64
        assert entry["bytes"] > 0


@pytest.mark.parametrize(
    ("kind", "field", "value"),
    [
        ("scans", "high_findings", 1),
        ("load", "error_rate", 0.1),
        ("load", "p99_ms", 501),
        ("load", "healthy_replicas", 1),
        ("backup_restore", "restored_data_sha256", "different"),
        ("rollback", "automatic", False),
        ("alarm", "alarm_state", "OK"),
        ("canary", "credentials_redacted", False),
        ("payment", "amount_usdc", 0.10),
    ],
)
def test_assemble_rejects_failed_evidence_thresholds(
    tmp_path: Path, kind: str, field: str, value: object
) -> None:
    _write_evidence(tmp_path)
    path = tmp_path / f"{kind}.json"
    document = json.loads(path.read_text())
    document["data"][field] = value
    path.write_text(json.dumps(document))

    with pytest.raises(EvidenceError):
        assemble(_args(tmp_path, tmp_path / "manifest.json"))


def test_assemble_rejects_missing_duplicate_stale_or_mismatched_evidence(
    tmp_path: Path,
) -> None:
    _write_evidence(tmp_path)
    (tmp_path / "payment.json").unlink()
    with pytest.raises(EvidenceError, match="missing evidence"):
        assemble(_args(tmp_path, tmp_path / "missing.json"))

    _write_evidence(tmp_path)
    duplicate = json.loads((tmp_path / "payment.json").read_text())
    (tmp_path / "duplicate.json").write_text(json.dumps(duplicate))
    with pytest.raises(EvidenceError, match="duplicate evidence"):
        assemble(_args(tmp_path, tmp_path / "duplicate-manifest.json"))
    (tmp_path / "duplicate.json").unlink()

    stale = json.loads((tmp_path / "canary.json").read_text())
    stale["started_at"] = (datetime.now(UTC) - timedelta(days=2, minutes=5)).isoformat()
    stale["completed_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    (tmp_path / "canary.json").write_text(json.dumps(stale))
    with pytest.raises(EvidenceError, match="stale"):
        assemble(_args(tmp_path, tmp_path / "stale.json"))

    _write_evidence(tmp_path)
    mismatch = json.loads((tmp_path / "load.json").read_text())
    mismatch["release"]["sha"] = "f" * 40
    (tmp_path / "load.json").write_text(json.dumps(mismatch))
    with pytest.raises(EvidenceError, match="release identity"):
        assemble(_args(tmp_path, tmp_path / "mismatch.json"))


def test_retention_workflow_is_protected_and_fail_closed() -> None:
    root = Path(__file__).resolve().parents[2]
    path = root / ".github/workflows/retain-production-evidence.yml"
    workflow = yaml.safe_load(path.read_text())
    job = workflow["jobs"]["retain"]
    text = path.read_text()

    assert job["environment"] == "production-evidence"
    assert workflow["permissions"] == {"actions": "read", "contents": "read"}
    assert "gh run view \"$REHEARSAL_RUN_ID\"" in text
    assert (
        'python tools/check_release_versions.py --release-tag "$RELEASE_TAG"'
        in text
    )
    assert "--max-age-hours 24" in text
    assert "retention-days: 90" in text
    assert "if-no-files-found: error" in text
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in text
    copy_position = text.index("cp evidence-input/*.json retained-evidence/")
    verify_position = text.index("python tools/production_evidence.py")
    assert copy_position < verify_position
    assert "--evidence-dir retained-evidence" in text
