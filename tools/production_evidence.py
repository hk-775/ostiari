"""Validate and assemble release-bound production evidence.

Each input is a JSON document produced by a dedicated rehearsal probe. This
tool deliberately does not execute destructive tests; it verifies that every
required result is fresh, passed, and bound to the exact release under review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

KINDS = {
    "scans",
    "load",
    "backup_restore",
    "rollback",
    "alarm",
    "canary",
    "payment",
}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class EvidenceError(ValueError):
    """Raised when production evidence is incomplete or inconsistent."""


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"{path}: evidence must be a JSON object")
    return value


def _timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise EvidenceError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise EvidenceError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _number(data: dict[str, Any], field: str) -> float:
    value = data.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvidenceError(f"data.{field} must be numeric")
    return float(value)


def _nonempty(data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"data.{field} must be non-empty")
    return value


def _validate_kind(kind: str, data: dict[str, Any]) -> None:
    if kind == "scans":
        if _number(data, "critical_findings") != 0:
            raise EvidenceError("scans must contain zero critical findings")
        if _number(data, "high_findings") != 0:
            raise EvidenceError("scans must contain zero high findings")
        sboms = data.get("sbom_sha256")
        if not isinstance(sboms, list) or not sboms:
            raise EvidenceError("scans require at least one SBOM digest")
        if not all(DIGEST_RE.fullmatch(item or "") for item in sboms):
            raise EvidenceError("scans contain an invalid SBOM digest")
    elif kind == "load":
        if _number(data, "requests") < 100:
            raise EvidenceError("load evidence requires at least 100 requests")
        if _number(data, "error_rate") > _number(data, "max_error_rate"):
            raise EvidenceError("load error rate exceeds its approved threshold")
        if _number(data, "p99_ms") > _number(data, "max_p99_ms"):
            raise EvidenceError("load p99 exceeds its approved threshold")
        if _number(data, "healthy_replicas") < 2:
            raise EvidenceError("load evidence requires at least two healthy replicas")
    elif kind == "backup_restore":
        if data.get("isolated_restore") is not True:
            raise EvidenceError("backup restore must target an isolated database")
        if _nonempty(data, "source_schema_sha256") != _nonempty(
            data, "restored_schema_sha256"
        ):
            raise EvidenceError("restored schema does not match the source")
        if _nonempty(data, "source_data_sha256") != _nonempty(
            data, "restored_data_sha256"
        ):
            raise EvidenceError("restored data does not match the source")
    elif kind == "rollback":
        if _nonempty(data, "candidate_digest") == _nonempty(
            data, "approved_digest"
        ):
            raise EvidenceError("rollback candidate must differ from the approved image")
        if _nonempty(data, "restored_digest") != _nonempty(
            data, "approved_digest"
        ):
            raise EvidenceError("rollback did not restore the approved image")
        if data.get("automatic") is not True or _number(data, "healthy_replicas") < 2:
            raise EvidenceError("rollback must be automatic and restore two replicas")
    elif kind == "alarm":
        if data.get("alarm_state") != "ALARM":
            raise EvidenceError("alarm evidence must observe an ALARM transition")
        _nonempty(data, "alarm_name")
        _nonempty(data, "delivery_receipt")
        _timestamp(data.get("delivered_at"), field="data.delivered_at")
    elif kind == "canary":
        checks = data.get("checks")
        if not isinstance(checks, dict):
            raise EvidenceError("canary evidence requires a checks object")
        required = {"gateway_authenticated", "control_plane_authenticated", "governed_call"}
        if any(checks.get(name) is not True for name in required):
            raise EvidenceError("authenticated canary checks did not all pass")
        if data.get("credentials_redacted") is not True:
            raise EvidenceError("canary evidence must attest credential redaction")
    elif kind == "payment":
        if data.get("mode") != "live" or data.get("settled") is not True:
            raise EvidenceError("payment evidence must be a settled live payment")
        amount = _number(data, "amount_usdc")
        if amount <= 0 or amount > _number(data, "cap_usdc"):
            raise EvidenceError("payment amount is outside the approved cap")
        _nonempty(data, "transaction_reference")


def validate_evidence(
    path: Path,
    *,
    release_sha: str,
    release_tag: str,
    gateway_digest: str,
    control_plane_digest: str,
    environment: str,
    now: datetime,
    max_age: timedelta,
) -> tuple[str, dict[str, Any]]:
    document = _read(path)
    if document.get("schema_version") != 1:
        raise EvidenceError(f"{path}: unsupported schema version")
    kind = document.get("kind")
    if kind not in KINDS:
        raise EvidenceError(f"{path}: unsupported evidence kind {kind!r}")
    if document.get("status") != "passed":
        raise EvidenceError(f"{path}: evidence status is not passed")
    identity = document.get("release")
    expected = {
        "sha": release_sha,
        "tag": release_tag,
        "gateway_image_digest": gateway_digest,
        "control_plane_image_digest": control_plane_digest,
    }
    if identity != expected:
        raise EvidenceError(f"{path}: release identity does not match the requested release")
    if document.get("environment") != environment:
        raise EvidenceError(f"{path}: environment does not match")
    started = _timestamp(document.get("started_at"), field=f"{path}.started_at")
    completed = _timestamp(document.get("completed_at"), field=f"{path}.completed_at")
    if completed < started:
        raise EvidenceError(f"{path}: completed_at precedes started_at")
    if completed > now + timedelta(minutes=5):
        raise EvidenceError(f"{path}: completed_at is in the future")
    if now - completed > max_age:
        raise EvidenceError(f"{path}: evidence is stale")
    data = document.get("data")
    if not isinstance(data, dict):
        raise EvidenceError(f"{path}: data must be an object")
    _validate_kind(kind, data)
    return kind, document


def assemble(args: argparse.Namespace) -> None:
    release_sha = args.release_sha.lower()
    gateway_digest = args.gateway_digest.lower()
    control_plane_digest = args.control_plane_digest.lower()
    if not SHA_RE.fullmatch(release_sha):
        raise EvidenceError("release SHA must be 40 lowercase hexadecimal characters")
    for label, value in (
        ("gateway digest", gateway_digest),
        ("control-plane digest", control_plane_digest),
    ):
        if not DIGEST_RE.fullmatch(value):
            raise EvidenceError(f"{label} must be a sha256 digest")

    evidence_dir = Path(args.evidence_dir)
    now = datetime.now(UTC)
    documents: dict[str, dict[str, Any]] = {}
    files: dict[str, dict[str, str | int]] = {}
    for path in sorted(evidence_dir.glob("*.json")):
        kind, document = validate_evidence(
            path,
            release_sha=release_sha,
            release_tag=args.release_tag,
            gateway_digest=gateway_digest,
            control_plane_digest=control_plane_digest,
            environment=args.environment,
            now=now,
            max_age=timedelta(hours=args.max_age_hours),
        )
        if kind in documents:
            raise EvidenceError(f"duplicate evidence kind: {kind}")
        raw = path.read_bytes()
        documents[kind] = document
        files[kind] = {
            "file": path.name,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    missing = sorted(KINDS - documents.keys())
    if missing:
        raise EvidenceError(f"missing evidence kinds: {', '.join(missing)}")

    manifest = {
        "schema_version": 1,
        "status": "passed",
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "release": {
            "sha": release_sha,
            "tag": args.release_tag,
            "gateway_image_digest": gateway_digest,
            "control_plane_image_digest": control_plane_digest,
        },
        "environment": args.environment,
        "max_age_hours": args.max_age_hours,
        "evidence": files,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"verified {len(documents)} evidence classes for {args.release_tag}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--evidence-dir", required=True)
    result.add_argument("--release-sha", required=True)
    result.add_argument("--release-tag", required=True)
    result.add_argument("--gateway-digest", required=True)
    result.add_argument("--control-plane-digest", required=True)
    result.add_argument("--environment", required=True)
    result.add_argument("--max-age-hours", type=int, default=24)
    result.add_argument("--output", required=True)
    return result


def main() -> int:
    try:
        assemble(parser().parse_args())
    except EvidenceError as exc:
        print(f"production evidence rejected: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
