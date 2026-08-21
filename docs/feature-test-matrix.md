# Feature test matrix

Ostiari treats feature claims as release contracts. The canonical
machine-readable mapping is
[`config/feature-test-matrix.json`](../config/feature-test-matrix.json);
[`tests/unit/test_feature_test_matrix.py`](../tests/unit/test_feature_test_matrix.py)
fails CI when an advertised capability has no automated test, references a
missing test, or names an unsupported live-evidence class.

## Evidence levels

| Level | Meaning |
|---|---|
| Automated | Unit, property, integration, migration, frontend, package, or protocol tests run on every pull request |
| Protected check | A pinned GitHub Actions job or verifier must pass before production artifacts are built |
| Retained rehearsal | Behavior that requires real infrastructure, external providers, alarms, payment rails, or failure injection is verified against the exact release and retained for 90 days |

The matrix covers every area in the capability summary in
[`features-and-flows.md`](features-and-flows.md), plus the cross-cutting
frontend, Codex, persistence/scaling/tenancy, and production-rehearsal
contracts.

## What “fully tested” means

Every advertised capability must have at least one executable automated test.
External service semantics are not simulated and relabeled as production
proof: they remain release-blocking until the protected rehearsal supplies all
seven evidence classes:

- zero-HIGH/CRITICAL scans and SBOM digests;
- two-replica load and failure behavior;
- isolated PostgreSQL backup/restore;
- automatic image rollback;
- alarm transition and delivery;
- authenticated gateway/control-plane canary;
- capped live payment.

The exact thresholds and release binding are enforced by
[`tools/production_evidence.py`](../tools/production_evidence.py) and the
[`Retain production evidence`](../.github/workflows/retain-production-evidence.yml)
workflow.

## Frontend coverage

The React control plane has a dependency-free Node behavioral suite in
`control-plane/frontend/tests`. Protected CI runs it before type checking and
the production build. It covers:

- bearer/session handling and structured API errors;
- local login, SSO validation, logout, and expired-session cleanup;
- role-based navigation visibility;
- safe SSO return-path handling;
- sandbox capability isolation, output/tool limits, cancellation, timeouts,
  and source hashing.

Backend API and database behavior remains covered by the FastAPI control-plane
suite; the frontend tests focus on browser-specific state and security
boundaries.

## Deployment coverage

CI parses the local Compose topology, verifies demo seeding is idempotent and
database-FK safe, builds every runtime image, and synthesizes all six AWS
profiles. Each synthesized template passes `cfn-lint` and contract assertions
for ECS service count, demo isolation, AgentCore inclusion, and production WAF
controls. Live AWS create/update, rollback, restore, alarms, and authenticated
canaries remain retained-rehearsal evidence rather than simulated unit claims.
