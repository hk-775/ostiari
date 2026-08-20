# Production readiness ledger

This document records the remaining launch gates after the Ostiari production
hardening work. It is intentionally narrower than the feature roadmap.

## Supported production boundary

The current deployment contract supports:

- one explicitly configured tenant (`OSTIARI_TENANCY_MODE=single`);
- one control-plane replica backed by PostgreSQL;
- multiple gateway replicas backed by Redis;
- OIDC-authenticated agent traffic with an exact issuer, audience, and tenant;
- immutable application image references;
- x402 disabled or configured for live settlement.

Do not enable control-plane horizontal scaling or advertise multi-tenant SaaS
until the corresponding items below are complete.

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

## Remaining Ostiari gates

1. **Control-plane horizontal scaling.** SQL is authoritative, but several
   routers retain process-local hot caches. Add transactional cache invalidation
   or read-through SQL before running more than one control-plane replica.

2. **Multi-tenant schema.** Several legacy tables still use globally scoped
   primary or unique keys. Convert them to tenant-qualified constraints before
   changing production tenancy mode from `single`.

3. **Registry and image publication authorization.** The workflow now publishes
   the complete Python release set, but the maintainers must configure and test
   trusted-publisher ownership for all four package names and publish signed,
   digest-pinned platform images before claiming a public package/container
   install path.

4. **Codex CLI conformance.** The stateless Responses subset is implemented,
   but current Codex uses the Responses wire API and may send reasoning or
   stateful fields that Ostiari deliberately rejects. Capture a supported Codex
   version and pass request, tool, streaming, cancellation, and error-shape
   conformance before advertising Codex compatibility.

5. **Immutable upstream inputs.** Pin the remaining Docker base images by
   verified digest and GitHub Actions by verified commit SHA. The gateway and
   control-plane Python bases are already digest-pinned; frontend and local
   Redis inputs remain to be resolved from their official registries. Do not
   guess these values.

6. **Retained production evidence.** Complete and archive dependency/container
   scan results, load and failure tests, PostgreSQL backup restore, rollback,
   alarm delivery, authenticated canary, and capped live-payment evidence for
   the exact release digest.

7. **Legal release approval.** Confirm authorization for repository copyright,
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
