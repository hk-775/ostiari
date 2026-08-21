# Retained production evidence

Ostiari public releases require one retained evidence bundle for the exact
release commit and deployed image digests. Evidence is produced in a dedicated
rehearsal environment, never by deliberately breaking the production fleet.

## Required evidence

The rehearsal run must upload an artifact named
`ostiari-production-rehearsal-evidence-<release-sha>` containing exactly one
JSON document for each class:

| Kind | Required proof |
|---|---|
| `scans` | Zero HIGH/CRITICAL findings and the SHA-256 digest of every SBOM |
| `load` | At least 100 requests, approved error/p99 thresholds, and two healthy replicas |
| `backup_restore` | Isolated PostgreSQL restore with matching schema and data digests |
| `rollback` | Automatic rollback from a different candidate image to the approved digest |
| `alarm` | Observed `ALARM` transition and downstream delivery receipt |
| `canary` | Authenticated gateway/control-plane checks and one governed request |
| `payment` | Settled live payment below an explicit USDC cap with a transaction reference |

Every document uses this envelope:

```json
{
  "schema_version": 1,
  "kind": "canary",
  "status": "passed",
  "release": {
    "sha": "40 lowercase hexadecimal characters",
    "tag": "v1.2.3",
    "gateway_image_digest": "sha256:...",
    "control_plane_image_digest": "sha256:..."
  },
  "environment": "production-rehearsal",
  "started_at": "2026-08-21T12:00:00Z",
  "completed_at": "2026-08-21T12:05:00Z",
  "data": {}
}
```

Tokens, private keys, connection strings, payment credentials, and raw
customer payloads must never be included. Evidence should contain identifiers,
digests, counts, thresholds, and redacted receipts only.

## Retention workflow

Run `Retain production evidence` with:

- the exact immutable release tag and 40-character commit;
- the deployed gateway and control-plane image digests;
- the successful protected rehearsal run ID;
- the rehearsal environment name.

The workflow checks out the exact release, verifies the tag and rehearsal run
resolve to that commit, rejects evidence older than 24 hours, enforces every
type-specific threshold, hashes each source file, and retains the resulting
bundle for 90 days.

The `production-evidence` GitHub environment must require reviewer approval.
Do not approve a retention run until the rehearsal environment has been
returned to its approved image and all alarms are healthy.

## Local validation

Operators can validate a downloaded rehearsal bundle before retention:

```bash
python tools/production_evidence.py \
  --evidence-dir evidence-input \
  --release-sha "$RELEASE_SHA" \
  --release-tag "$RELEASE_TAG" \
  --gateway-digest "$GATEWAY_DIGEST" \
  --control-plane-digest "$CONTROL_PLANE_DIGEST" \
  --environment production-rehearsal \
  --max-age-hours 24 \
  --output retained-evidence/manifest.json
```

The command is fail closed: missing, duplicate, stale, failed,
release-mismatched, over-budget, or threshold-violating evidence prevents the
manifest from being created.
