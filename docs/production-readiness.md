# Production readiness ledger

This document records the remaining launch gates after the Ostiari production
hardening work. It is intentionally narrower than the feature roadmap.

## Supported production boundary

The current deployment contract supports:

- a multi-tenant control plane with tenant-qualified natural keys, foreign
  keys, idempotency constraints, users, wallets, policies, and audit chains;
- gateways assigned to one explicit tenant per deployment;
- multiple control-plane replicas backed by PostgreSQL and Redis;
- multiple gateway replicas backed by Redis;
- OIDC-authenticated agent traffic with an exact issuer, audience, and tenant;
- immutable application image references;
- x402 disabled or configured for live settlement.

## Completed in Ostiari

- Production startup fails closed on missing authentication, machine secrets,
  PostgreSQL, encryption, tenant, CORS, Redis, callback allowlist, request rate
  limit, and payment posture.
- Tool, validation, native invoke, Claude/OpenAI shim, MCP, A2A, and metadata
  ingress use verified token identities rather than trusting `X-Agent-Id`.
- Control-plane roles are restricted to `admin`, `operator`, and `viewer`.
- Operator and gateway proxy responses have absolute deadlines and byte limits;
  operator browser credentials are not forwarded downstream.
- Approvals, sanitized traces, SSO state, audit-chain state, configuration,
  quotas, pricing, routing, providers, experiments, and offline updates are
  durable in SQL.
- Shared gateway rate, budget, and wallet enforcement is atomic in Redis and
  fails readiness closed in production.
- Trace, cost, payment, and budget-alert events are persisted to gateway-scoped
  Redis Streams before delivery, acknowledged only after a successful
  control-plane commit, and recovered after gateway restarts. Their receiving
  APIs are idempotent on stable event IDs, and production readiness fails
  closed when the durable stream path is unavailable.
- Gateway lifecycle and event APIs use short-lived workload OIDC credentials.
  The control plane immutably binds each verified issuer/subject pair to one
  gateway, enforces optional gateway-id and tenant claims, and rejects
  fleet-wide service/ingest keys in production. Official Kubernetes, Helm, and
  ECS manifests use per-gateway OAuth client credentials; projected token files
  are also supported.
- Control-plane runtime configuration uses transactionally incremented,
  tenant/namespace revisions in PostgreSQL. Replicas refresh only changed
  namespaces, live traces fan out through Redis with SQL catch-up, session roots
  are assigned atomically, and singleton health sweeps use a Redis lease.
  Production rate limiting and `/api/ready` fail closed when Redis or replica
  synchronization is unhealthy.
- Reusable gateway, wallet, user, policy, usage, and payment identifiers are
  tenant-qualified in SQL. Gateway child foreign keys include the tenant,
  machine and user lookups always scope by verified tenant identity, and audit
  chains have independent per-tenant heads. Multi-tenant tokens without an
  explicit tenant claim are rejected.
- Gateway callback destinations are restricted to an explicit hostname/CIDR
  allowlist and may never target link-local metadata addresses.
- Production manifests run migrations first, use non-root/read-only containers,
  separate liveness from readiness, and require digest-pinned application images.
- AxonLLM `v0.3.1` is bundled under `vendor/axonllm`, installed in source and
  container builds, initialized through its public embedded router API, and
  required automatically for production LLM traffic.
- The gateway exposes governed Anthropic Messages, OpenAI Chat Completions, and
  a stateless OpenAI Responses subset. Unsupported Responses state/background
  fields fail closed instead of being ignored.
- CI exercises tests, lint, typing, migrations, packages, manifests, images,
  dependency audits, tracked-secret scanning, and SBOM generation.
- CI builds the root, AxonLLM, gateway, and control-plane wheels, installs them
  into a clean environment outside the checkout, and initializes the embedded
  router from configuration packaged inside the gateway wheel.
- The release workflow builds, re-verifies, publishes, and attaches those same
  four distributions as one release set rather than publishing only the Guard
  library.
- Every external GitHub Action is pinned to a verified commit SHA. Container
  build inputs are digest-pinned, and the local development state service uses
  a digest-pinned Valkey image rather than a mutable Redis tag.
- Codex CLI `0.148.0` is supported through a pinned capability catalog and a
  protected loopback conformance gate. CI verifies stateless request shape,
  typed streaming, a function-call/output round trip, OpenAI-shaped errors, and
  cancellation. Unsupported stateful fields and model-reasoning requests
  continue to fail closed. Codex's no-op `effort/summary = none` shape and its
  standard encrypted-context include (with an empty reasoning object or
  `context=all_turns`) is accepted without generating or persisting reasoning
  content. The profile emits only direct function tools and disables freeform
  patching, multi-agent namespaces, and hosted search. When Codex requests
  `parallel_tool_calls=false`, any upstream response containing multiple
  function calls is rejected.
- A protected retention workflow verifies seven production-rehearsal evidence
  classes against the exact release commit and deployed image digests. It
  rejects stale, incomplete, mismatched, over-budget, or threshold-violating
  results and archives the hashed bundle for 90 days. The evidence format and
  operator procedure are documented in
  [`production-evidence.md`](production-evidence.md).

## Remaining Ostiari gates

1. **Registry and image publication authorization.** The workflow now publishes
   the complete Python release set, but the maintainers must configure and test
   trusted-publisher ownership for all four package names and publish signed,
   digest-pinned platform images before claiming a public package/container
   install path.

2. **Run the retained production-evidence gate.** The verifier and protected
   90-day retention workflow are complete. Before release approval, execute the
   dedicated rehearsal for the exact release digest and retain passing scan,
   load/failure, PostgreSQL restore, rollback, alarm-delivery, authenticated
   canary, and capped live-payment results.

3. **Legal release approval.** Confirm authorization for repository copyright,
   trademarks, and third-party notices before the public release.

## AxonLLM dependency record

Ostiari carries an immutable AxonLLM source snapshot:

- upstream tag: `v0.3.1`;
- upstream commit: `a7730a516928272c570da53845248f1f61c31f7c`;
- package: `axon-llm==0.3.1`;
- license: MIT-0;
- provenance and refresh procedure:
  [`vendor/axonllm/UPSTREAM.md`](../vendor/axonllm/UPSTREAM.md).

`make install`, CI, and the production gateway image install the bundled
`server` extra and exercise a real router initialization. The gateway wheel
declares the exact companion AxonLLM dependency and contains the reviewed
routing configuration, licenses, and provenance needed to initialize outside a
source checkout. Production LLM traffic fails closed when the router cannot
initialize or fails during a call. Tool-only gateways may leave the LLM module
disabled.
