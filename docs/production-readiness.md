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
- Gateway callback destinations are restricted to an explicit hostname/CIDR
  allowlist and may never target link-local metadata addresses.
- Production manifests run migrations first, use non-root/read-only containers,
  separate liveness from readiness, and require digest-pinned application images.
- CI exercises tests, lint, typing, migrations, packages, manifests, images,
  dependency audits, tracked-secret scanning, and SBOM generation.

## Remaining Ostiari gates

1. **Durable gateway event outbox.** Trace, cost, payment, and budget-alert
   reporters retry in memory. A gateway process crash during a control-plane
   outage can still lose an unconfirmed event. Persist idempotent events in a
   Redis Stream (or equivalent) and acknowledge only after control-plane commit.

2. **Codex Responses API.** The shipped OpenAI compatibility route is
   `/v1/chat/completions`. Add and test `/v1/responses` before claiming current
   Codex client support.

3. **Per-gateway machine identity.** Gateway lifecycle/event APIs currently use
   one fleet service token. Replace it with workload-specific OIDC credentials
   and bind the verified client identity to the gateway path/body.

4. **Control-plane horizontal scaling.** SQL is authoritative, but several
   routers retain process-local hot caches. Add transactional cache invalidation
   or read-through SQL before running more than one control-plane replica.

5. **Multi-tenant schema.** Several legacy tables still use globally scoped
   primary or unique keys. Convert them to tenant-qualified constraints before
   changing production tenancy mode from `single`.

6. **Immutable upstream inputs.** Pin Docker base images by verified digest and
   GitHub Actions by verified commit SHA. Do not guess these values; resolve and
   review them from their official registries before the release cut.

7. **Retained production evidence.** Complete and archive dependency/container
   scan results, load and failure tests, PostgreSQL backup restore, rollback,
   alarm delivery, authenticated canary, and capped live-payment evidence for
   the exact release digest.

8. **Legal release approval.** Confirm authorization for repository copyright,
   trademarks, and third-party notices before the public release.

## AxonLLM dependency record

No AxonLLM source is changed by this hardening branch.

Before Ostiari enables LLM routing in a production image, AxonLLM must provide:

- a versioned, immutable install artifact compatible with Ostiari's model
  registry and provider-route APIs;
- a supported release/version contract for tool pass-through and route pools;
- an integration environment that public Ostiari CI can exercise.

Ostiari must then pin that artifact in the gateway image, set
`OSTIARI_REQUIRE_AXON=1`, and pass the real Axon integration suite. Until that
exists, launch Ostiari with LLM routing disabled rather than accepting the
direct-provider fallback as governed traffic.
