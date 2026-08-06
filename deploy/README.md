# Ostiari Deployment Guide

## Deployment Options

### Docker Compose (Local Development)

Full local stack with gateway, control-plane backend + frontend, and Redis:

```bash
cd deploy/docker
docker compose up --build
```

Gateway: http://localhost:8421
Control Plane API: http://localhost:8400
Control Plane UI: http://localhost:9000

The three images can also be built directly (contexts are the **repo root**,
because the gateway and control-plane images install the local `ostiari` core
package):

```bash
docker build -f deploy/docker/Dockerfile.gateway       -t ostiari-gateway:latest .
docker build -f deploy/docker/Dockerfile.control-plane -t ostiari-control-plane:latest .
docker build -f deploy/docker/Dockerfile.frontend \
  --build-arg VITE_API_URL=http://localhost:8400       -t ostiari-frontend:latest .
```

> The frontend's `VITE_API_URL` is baked into the bundle at build time and is
> called from the user's **browser**, so set it to a URL the browser can reach
> (a published host/ingress address), not an in-cluster service name.

### Kubernetes - Sidecar Pattern

Deploy the gateway as a sidecar alongside your agent container:

```bash
kubectl apply -f deploy/kubernetes/gateway-sidecar.yaml
kubectl apply -f deploy/kubernetes/control-plane.yaml
```

### Kubernetes - Shared Gateway

Deploy a shared gateway cluster that multiple agents route through:

```bash
kubectl apply -f deploy/kubernetes/gateway-shared.yaml
kubectl apply -f deploy/kubernetes/control-plane.yaml
```

### Helm Chart

```bash
helm install ostiari deploy/helm/ostiari-gateway \
  --set gateway.controlPlaneUrl=http://your-control-plane:8400 \
  --set redis.endpoint=your-redis-host
```

Override values:

```bash
helm install ostiari deploy/helm/ostiari-gateway -f custom-values.yaml
```

### ECS Fargate

1. Create the ECS cluster, VPC, and ALB (or use existing).
2. Replace placeholders in `ecs/task-definition.json` and `ecs/service.json`.
3. Register and deploy:

```bash
aws ecs register-task-definition --cli-input-json file://deploy/ecs/task-definition.json
aws ecs create-service --cli-input-json file://deploy/ecs/service.json
```

### AWS Lambda (Serverless)

Deploy using AWS SAM. The gateway and core packages are installed from the repo
(they are not published to PyPI):

```bash
cd deploy/lambda
pip install mangum -t .
pip install ../.. -t .        # ostiari core
pip install ../../gateway -t . # ostiari-gateway
sam build
sam deploy --guided
```

> **Caveat:** Lambda runs the gateway's request/response validation only. The
> register/heartbeat/config-push background loop does **not** run under Lambda
> (`lifespan="off"`), so a Lambda gateway won't stay registered or receive
> pushed config. For a fully governed gateway, use ECS or Kubernetes.

## Environment Variables

**Gateway:**

| Variable | Default | Description |
|----------|---------|-------------|
| `OSTIARI_GATEWAY_ID` | `sidecar-1` | Unique gateway instance identifier. Must match the gateway's control-plane record id or the control plane can't push it tools and policy. (The CLI flag is still `--sidecar-id`.) |
| `OSTIARI_CONTROL_PLANE_URL` | _(none)_ | Control plane backend URL (enables register/heartbeat) |
| `OSTIARI_PORT` | `8421` | Gateway listen port |
| `OSTIARI_ADVERTISE_HOST` | _(bind host)_ | Host the control plane pushes config back to. Set this to the gateway's network-reachable name (compose service, k8s Service DNS, ECS service). Without it, config pushes may not reach the gateway. |
| `OSTIARI_ENV` | _(unset = dev)_ | `production` (or `prod`) shifts the gateway's defaults toward fail-closed: an unreachable control plane flips agent auth to deny-by-default, SSRF protection also blocks private/internal targets, and startup warns about every control still left open. It does **not** itself set the controls below — see "Production Notes". |
| `OSTIARI_FAIL_CLOSED_ON_CP_LOSS` | _(implied by `OSTIARI_ENV`)_ | Explicit override for the deny-by-default-on-registration-failure behavior. Set `true`/`false` to decide independently of `OSTIARI_ENV`. |
| `OSTIARI_SSRF_ALLOW` | _(none)_ | Comma-separated hosts/CIDRs exempt from the production private-IP block, for tools that legitimately live on internal addresses. Link-local and metadata addresses (169.254.169.254) are blocked in **every** environment and cannot be allowlisted. |
| `OSTIARI_HITL` | `off` | `on` enables human-in-the-loop for the *intervene* tier: a mid-band call returns **202** with an approval id instead of executing, and the caller re-submits with `X-Approval-Id` once a human approves. **Set this in production** — see below. |
| `OSTIARI_STRICT` | _(unset)_ | With `OSTIARI_ENV=production`, makes the startup fail-open warning **fatal** instead of a log line. |
| `OSTIARI_REQUIRE_AXON` | _(unset)_ | Refuse to start when AxonLLM can't embed. Unset, the gateway warns and serves LLM traffic with **no routing governance and no token cost tracking** (`GET /health` → `llm_router` reports it). Not shipped on in the manifests here — AxonLLM is a separate repository, so a gateway that only proxies tools shouldn't need it installed. Set it if you route LLM calls. |
| `OSTIARI_CONFIG_ADMIN_KEY` | _(none)_ | Required in production: without it everything under `/config` (mode, tools, policy, quota, payments) is **unauthenticated**, reads included. When set, it's compared with `hmac.compare_digest`; `GET /config/mode` and `GET /tools` stay open. |
| `OSTIARI_GATEWAY_AUTH` | `off` | Set `required` in production: otherwise `X-Agent-Id` is trusted with no token, so any caller can impersonate any agent. |
| `REDIS_ENDPOINT` | _(none)_ | Redis host for fleet-wide rate-limit / budget / wallet state |
| `REDIS_PORT` | `6379` | Redis port |
| `OSTIARI_REDIS_URL` | _(none)_ | Full URL alternative to the two above (`redis://[:pass@]host:port/db`); checked first |
| `OSTIARI_REDIS_PREFIX` | `ostiari` | Key namespace, so several gateways or tenants can share one Redis |
| `ANTHROPIC_API_KEY` | _(none)_ | Anthropic API key for LLM routing |
| `OPENAI_API_KEY` | _(none)_ | OpenAI API key for LLM routing |

**Control plane:**

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | SQLite in `OSTIARI_DATA_DIR` | `sqlite+aiosqlite:///…` (dev) or `postgresql+asyncpg://user:pass@host:5432/ostiari` (prod) |
| `OSTIARI_DATA_DIR` | `control-plane/data` | Directory for writable runtime state: the default SQLite database and `state.json`. Set to a mounted path (`/data` in the images here) so nothing is written into the app directory — required for `readOnlyRootFilesystem`, and required for `state.json` to survive a restart at all. |
| `OSTIARI_NO_DEMO` | _(unset)_ | Set to `1` to start with an empty control plane (no seeded demo data) |
| `OSTIARI_CORS_ORIGINS` | _(all, no creds)_ | Comma-separated allowed origins (enables credentialed CORS) |
| `OSTIARI_REQUIRE_AUTH` | _(unset)_ | Set to require API authentication |
| `OSTIARI_ENV` | _(unset = dev)_ | `production` makes the four variables below **required** — the control plane refuses to start without them, rather than seeding a default admin. |
| `OSTIARI_ADMIN_PASSWORD` | _(dev seed)_ | **Required in production.** Without it the control plane refuses to seed an admin at all. |
| `OSTIARI_JWT_SECRET` | _(dev default)_ | **Required in production**, ≥32 chars. Startup fails otherwise. |
| `OSTIARI_INGEST_KEY` | _(none)_ | Gates `POST /api/traces/ingest` with an `X-Ingest-Key` header; unset in production, every trace ingest is 401. **Read the caveats before setting it** — see below. |
| `OSTIARI_ENCRYPTION_KEY` | _(ephemeral)_ | Encrypts stored provider API keys. Unset, a new key is minted per process — stored keys become unreadable after restart. |

### `OSTIARI_INGEST_KEY` — two gaps

This variable is not yet the control it's meant to be. Both of these were verified
against a running control plane:

- **The gateway never sends the header.** Nothing in `gateway/` reads
  `OSTIARI_INGEST_KEY`, and `trace_reporter.py` posts to `/api/traces/ingest` with
  no headers. Setting the key on the control plane therefore **stops trace
  reporting** — every post 401s, and `report()` swallows the failure at debug
  level, so Live Traces goes quiet with no visible error. Setting it on the gateway
  side does nothing at all today.
- **It only covers trace ingest.** `POST /api/costs/record`, `/api/costs/record/batch`,
  `/api/approvals`, and `/api/payments/ingest` have no such check and accept an
  anonymous POST even with the key set — so metering, cost, approval, and payment
  records can still be forged.

In production, an empty trace view beats a poisoned one, so setting it is still
defensible — but expect a blank dashboard, and don't read it as "ingest is
authenticated."

## Secrets Management

For Kubernetes, create a secret:

```bash
kubectl create secret generic ostiari-secrets \
  --from-literal=anthropic-api-key=sk-ant-... \
  --from-literal=openai-api-key=sk-...
```

For ECS, store secrets in AWS Secrets Manager and reference them in the task definition.

## Production Notes

- **Containers run as non-root.** All three images set a `USER` (gateway and
  control plane `10001`, frontend `101` — nginx's own uid), and every manifest here
  re-asserts it. Both layers matter: a Dockerfile `USER` is only a default that a
  manifest can override, while Kubernetes `runAsNonRoot: true` makes the kubelet
  *refuse* to start a container that would run as uid 0 — so an image rebuilt
  without `USER` fails loudly instead of quietly regaining root. Alongside it:
  `allowPrivilegeEscalation: false`, all capabilities dropped, and
  `seccompProfile: RuntimeDefault`.

  All three also run with a **read-only root filesystem**, so a compromised
  container cannot rewrite its own code or install anything. Each declares exactly
  one writable mount:
  - **gateway** → `/tmp`, where it renders pushed policies to a tempfile
    (`config_manager._policy_file`);
  - **frontend** → `/tmp`, where nginx keeps its pid file and all five temp dirs;
  - **control-plane backend** → `/data` (the PVC / named volume), holding the
    SQLite database and `persistence.STATE_FILE`.

  Note that removing a writable mount fails at three different *times*, which
  mislead differently:
  - Dropping the gateway's `/tmp` fails **late**: it goes Ready, passes its health
    check, and then 500s on the first policy push.
  - Dropping the backend's `OSTIARI_DATA_DIR` fails **at import**, before the app
    exists, so it crash-loops with a traceback and never answers a probe.
  - Getting the backend's data dir only *half* right fails **at shutdown**, which is
    the worst of the three — see below.
- **`OSTIARI_DATA_DIR` is what makes the backend's read-only root possible.** Unset,
  both writable paths are derived from `__file__` and resolve relative to the app
  directory (`/app` in the image), which is root-owned and uncreatable by the
  non-root user. Setting it (the image defaults to `/data`) moves both onto the
  mounted volume.

  It also fixes a real bug that predates the read-only work. The database and
  `state.json` resolved to *different* directories, one level apart, so
  `DATABASE_URL` — which only redirects the database — left `state.json` behind in
  `/app/data`. `save_state` then raised `PermissionError` in the lifespan shutdown
  hook, which uvicorn logs as `Application shutdown failed` *after* the container
  has already served traffic normally: every restart silently discarded the
  persisted quotas, experiments, models, and provider config.

  On ECS the gateway's writable mount is a plain empty `volumes` entry, **not**
  `linuxParameters.tmpfs` — tmpfs is unsupported on the Fargate launch type this
  task family declares, and a task definition using it fails to launch.
- **Database**: The control plane uses SQLite for dev. For production, configure PostgreSQL via RDS.
- **TLS**: Terminate TLS at the load balancer or ingress controller, not at the gateway.
- **`OSTIARI_ENV=production` and `OSTIARI_HITL` travel together.** Production is
  fail-closed, which changes what the *middle* risk tier means. Ostiari scores each
  call 0–100 into allow / **intervene** / block; intervene means "a human should
  look at this." In dev (fail-open) an unresolved intervene is *allowed through*.
  In production the same call is **refused** — unless HITL is on, in which case it
  is deferred to the control plane's Approvals queue instead. So
  `OSTIARI_ENV=production` with `OSTIARI_HITL=off` silently collapses three tiers
  to two: every intervene becomes a 403 and the Approvals page stays empty. The
  Helm chart, k8s manifests, and ECS task definition here all ship
  `OSTIARI_HITL=on` for that reason.
- **HITL has an operational cost — budget for it before you deploy.** With it on, a
  mid-band call does not execute: the gateway answers **202** with an approval id
  and waits. Two things must be true or traffic stalls:
  1. **Someone staffs the queue.** An unattended queue means those calls never
     complete; a queue too large to read gets rubber-stamped, which is worse than
     not having one. Tune thresholds so the intervene band is *rare* first —
     validate with a gateway in `shadow` mode and read the Shadow Report.
  2. **Callers handle 202.** The gateway does not hold a thread open waiting for a
     human. The *caller* must re-submit the same request with an
     `X-Approval-Id: <id>` header once approved. An agent framework that treats
     any non-200 as failure will look like it silently lost the call.

  Full walkthrough: [`docs/control-plane-guide.md`](../docs/control-plane-guide.md) §7.4.
- **Close the remaining fail-open controls.** `OSTIARI_ENV=production` warns at
  startup about each control still left open but starts anyway. At minimum set
  `OSTIARI_CONFIG_ADMIN_KEY` (else `/config/*` is unauthenticated — anyone reaching
  the port can rewrite your policy or flip enforcement to shadow) and
  `OSTIARI_GATEWAY_AUTH=required` (else `X-Agent-Id` is trusted with no token, so
  any caller can impersonate any agent). Set `OSTIARI_STRICT=1` to make that
  warning fatal rather than a log line someone scrolls past.
- **Scaling & fleet-wide limits**: Enforcement state — the rate limiter,
  quota/budget counters, and payment wallets — is **in-process by default**, so
  a horizontally-scaled fleet enforces limits **per replica** (N instances ⇒ N×
  the effective `rate_limit_rpm`/`budget_limit_usd`; wallet balances diverge per
  pod). To make limits hold **fleet-wide**, point the gateway at Redis (below);
  the rate limiter, budget reservations, and wallet debits then run as atomic
  operations against shared Redis state, correct across replicas.
- **Redis (shared state)**: Install the extra (`pip install "ostiari-gateway[redis]"`,
  already in the deploy images if you add it) and set `REDIS_ENDPOINT`
  (+ optional `REDIS_PORT`, default 6379) or `OSTIARI_REDIS_URL`
  (`redis://[:pass@]host:port/db`). On startup the gateway PINGs Redis and, if
  reachable, shares rate-limit/budget/wallet state across the fleet; if Redis is
  **unset or unreachable**, it logs and falls back to per-process limits (never a
  hard failure). `OSTIARI_REDIS_PREFIX` (default `ostiari`) namespaces keys so
  several gateways/tenants can share one Redis; a shared `budget_key` in the
  pushed quota config lets gateways share (or partition) one budget.

  Because the fallback is silent by design, a typo'd endpoint looks exactly like
  a working one from the outside. Confirm from the startup log rather than
  assuming — a fleet you *believe* is sharing state but isn't enforces N× your
  configured limits.
- **Authenticating `/config`.** With `OSTIARI_CONFIG_ADMIN_KEY` set, callers
  present it as `X-Config-Admin-Key: <key>` or `Authorization: Bearer <key>`.
