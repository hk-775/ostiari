# Ostiari Deployment Guide

## Recommended launcher

`./deploy/ostiari` is the supported adopter path. It provides one profile model,
waits for readiness, verifies deployed endpoints, and keeps generated state under
`.ostiari/deployments/`.

The canonical diagrams and request flows are in
[`docs/architecture.md`](../docs/architecture.md).

| Profile | Data | AgentCore | Placement and egress | Intended use |
|---|---:|---:|---|---|
| `local-demo` | Seeded | No | Docker Compose on one host | First run and evaluation |
| `local-empty` | Empty | No | Docker Compose on one host | Local integration work |
| `aws-demo` | Seeded | No | Public Fargate tasks, no NAT | Cost-aware AWS evaluation |
| `aws-empty` | Empty | No | Public Fargate tasks, no NAT | Clean AWS integration |
| `aws-agentcore-demo` | Seeded | Yes | Private app subnets, one NAT | AgentCore evaluation |
| `aws-agentcore-empty` | Empty | Yes | Private app subnets, one NAT | Clean AgentCore integration |
| `production` | Empty | No | Private Fargate tasks, two NAT gateways | Hardened production |
| `production-agentcore` | Empty | Yes | Private Fargate and AgentCore, two NAT gateways | Hardened production with AgentCore |

List these at any time:

```bash
./deploy/ostiari profiles
```

### Local

Docker Compose builds the gateway with embedded AxonLLM, control plane,
dashboard, and Valkey. The demo profile also starts functional demo tools and an
idempotent seed job.

```bash
./deploy/ostiari local up --profile local-demo
./deploy/ostiari local status --profile local-demo
./deploy/ostiari local logs --profile local-demo --follow
./deploy/ostiari local down --profile local-demo
```

For an empty database:

```bash
./deploy/ostiari local up --profile local-empty
```

Use `--gateway-port`, `--control-plane-port`, `--frontend-port`,
`--demo-tools-port`, and `--redis-port` when the defaults are occupied.

### AWS evaluation

Prerequisites are AWS CLI v2 credentials, Docker Buildx, Node.js 22+, and enough
quota for the selected profile. The launcher installs its pinned CDK dependencies
in `deploy/aws/.venv` and `deploy/aws/node_modules`, bootstraps CDK idempotently,
and limits public access to the caller's current `/32` by default.

```bash
aws login
./deploy/ostiari aws plan --profile aws-demo --name evaluation --region us-east-1
./deploy/ostiari aws deploy --profile aws-demo --name evaluation --region us-east-1
```

Change `aws-demo` to `aws-empty`, `aws-agentcore-demo`, or
`aws-agentcore-empty`. AgentCore profiles deploy the same operational Ostiari
gateway and control plane plus an IAM-authorized AgentCore HTTP runtime that
routes validation through the private gateway. The gateway is not replaced by
AgentCore because registration, heartbeat, config push, and durable reporting
are gateway lifecycle contracts.

`aws-demo` and `aws-empty` avoid NAT cost: their application tasks run in public
subnets with public IPs, while security groups restrict ingress to the ALB and
internal service relationships. AgentCore profiles resolve two zones from the
checked-in AgentCore support registry, map the zone IDs to account-specific AZ
names, and place application workloads in private subnets behind one NAT
gateway.

The AWS stack creates:

- a two-AZ VPC and private service discovery;
- ECS/Fargate control plane, gateway, and dashboard services;
- encrypted PostgreSQL and serverless Valkey;
- an internet-facing ALB restricted to the configured CIDRs;
- CloudWatch logs and deployment circuit breakers;
- optional demo tools and optional Bedrock AgentCore runtime.

Remove an evaluation stack with:

```bash
./deploy/ostiari aws destroy --profile aws-demo --name evaluation --yes
```

AgentCore runtime deletion can complete before AWS releases its managed
`agentic_ai` network interfaces. If those ENIs temporarily block subnet,
security-group, or VPC deletion, the launcher identifies them and asks you to
retry the same destroy command after AWS releases them.

### Production

Production is deliberately two-phase: prepare immutable artifacts and
configuration, then create and review a CloudFormation change set before
execution.

1. Publish architecture-correct, immutable ECR images from a clean release
   commit:

   ```bash
   ./deploy/ostiari aws publish-images \
     --name production \
     --region us-east-1
   ```

   Add `--include-agentcore` for `production-agentcore`. The command creates
   immutable, scan-on-push ECR repositories, uses an attestation-capable Buildx
   builder, publishes provenance/SBOM attestations for the architecture-specific
   images, and writes manifest-digest URIs to
   `.ostiari/deployments/production/images.json`.

2. Provision the external identity contracts: one OAuth client for gateway
   workload identity, an agent-token issuer/audience, and a separate agent
   client for AgentCore when enabled. Create an issued ACM certificate and put
   the required secret values in AWS Secrets Manager. Secret values are never
   stored in deployment JSON.

3. Start from `deploy/aws/examples/production.json` or
   `production-agentcore.json`. Replace every placeholder, use the image URIs
   produced in step 1, and pass the resulting file to all production commands.
   Set `allowed_cidrs` to the operator, VPN, or reverse-proxy ranges that should
   reach the ALB. Fernet encryption keys must be URL-safe base64 encodings of
   exactly 32 random bytes.

4. Run preflight, synthesize the plan, and prepare the change set:

   ```bash
   ./deploy/ostiari aws preflight \
     --profile production --name production --region us-east-1 \
     --config /path/to/production.json

   ./deploy/ostiari aws plan \
     --profile production --name production --region us-east-1 \
     --config /path/to/production.json

   ./deploy/ostiari aws deploy \
     --profile production --name production --region us-east-1 \
     --config /path/to/production.json
   ```

   `deploy` does not execute production changes. It saves the complete reviewed
   change-set response under `.ostiari/deployments/production/` and prints the
   exact `aws execute` command. Execution is blocked when CloudFormation reports
   resource replacement unless `--allow-replacements` is explicitly supplied.

Production adds multi-AZ PostgreSQL, two NAT gateways, autoscaling, ALB access
logs, WAF rate limiting, alarms, TLS-only ingress, retained state, deletion
protection, and stack termination protection. It refuses demo data, mutable
images, partial secret ARNs, non-HTTPS identity endpoints, or insecure runtime
posture.

### Operations

```bash
./deploy/ostiari aws status --name evaluation --region us-east-1
./deploy/ostiari aws preflight --profile aws-demo --name evaluation
```

The preflight catches known failed-stack states, checks VPC quota when a new VPC
is required, and checks Elastic IP quota before profiles create NAT gateways.
Production preflight also verifies every Secrets Manager ARN, ACM certificate,
and ECR image digest without retrieving secret values.

After an AWS deployment, the launcher prints the dashboard URL, the admin email,
and an exact Secrets Manager command for retrieving the generated password. It
never retrieves or writes that password itself.

## Still external by design

The launcher does not provision an identity provider or DNS registrar, approve
AWS service-quota increases, or automate destructive production teardown. It
also does not replace retained production rehearsals for restore, rollback,
alarm delivery, authenticated canaries, load/failure behavior, and live payment
caps. Official signed public images, private-only/existing-VPC installs,
multi-region disaster recovery, and air-gapped packaging remain separate release
or enterprise deployment tracks.

## Deployment Options

The following lower-level templates remain supported for teams that already
operate their own platform tooling.

### Docker Compose (Local Development)

Full local stack with gateway, control-plane backend + frontend, and Valkey:

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

These `:latest` names are local-development tags only. Publish production images
once, record their manifest digests, and reference those digests from Kubernetes,
Helm, or ECS.

> The frontend's `VITE_API_URL` is baked into the bundle at build time and is
> called from the user's **browser**, so set it to a URL the browser can reach
> (a published host/ingress address), not an in-cluster service name.

### Kubernetes - Sidecar Pattern

The checked-in manifests are production templates, not ready-to-apply defaults.
Replace every `REPLACE_*` value, publish digest-pinned images, and create the
referenced `ostiari-secrets` keys before applying them. The control plane runs
two replicas and requires `database-url` plus a TLS/authenticated `redis-url`;
The bundled Valkey service provides the Redis protocol used for live fan-out,
rate limits, singleton work, durable outboxes, and readiness:

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

Create a values file containing the immutable image digest, OIDC issuer and
audience, Redis secret key, and existing Secret name. Production rendering fails
when any of those controls is missing:

```bash
helm install ostiari deploy/helm/ostiari-gateway -f custom-values.yaml
```

### ECS Fargate

1. Create the ECS cluster, VPC, and ALB (or use existing).
2. Replace placeholders in `ecs/task-definition.json` and `ecs/service.json`.
3. Configure the ALB target group's health-check path as `/ready`. The
   container health check remains `/health`: Redis loss should remove a task
   from traffic without creating a restart loop.
4. Register and deploy:

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

> **Non-production only:** Lambda runs the gateway's request/response validation only. The
> register/heartbeat/config-push background loop does **not** run under Lambda
> (`lifespan="off"`), so a Lambda gateway won't stay registered or receive
> pushed config. It cannot satisfy the production lifecycle contract. Use ECS
> or Kubernetes for a governed deployment.

## Environment Variables

**Gateway:**

| Variable | Default | Description |
|----------|---------|-------------|
| `OSTIARI_GATEWAY_ID` | `sidecar-1` | Unique gateway instance identifier. Must match the gateway's control-plane record id or the control plane can't push it tools and policy. (The CLI flag is still `--sidecar-id`.) |
| `OSTIARI_CONTROL_PLANE_URL` | _(none)_ | Control plane backend URL (enables register/heartbeat) |
| `OSTIARI_PORT` | `8421` | Gateway listen port |
| `OSTIARI_ADVERTISE_HOST` | _(bind host)_ | Host the control plane pushes config back to. Set this to the gateway's network-reachable name (compose service, k8s Service DNS, ECS service). Without it, config pushes may not reach the gateway. |
| `OSTIARI_ENV` | _(unset = dev)_ | `production` (or `prod`) activates a fatal startup posture check. The gateway refuses missing machine credentials, OIDC issuer/audience, Redis, fail-closed control-plane handling, or simulated settlement. |
| `OSTIARI_TENANCY_MODE` / `OSTIARI_ORG_ID` | `multi` / `default` in dev | Each production gateway is assigned to one explicit organization with `single`; agent tokens must carry that organization. The shared control plane may run in `multi`. |
| `OSTIARI_FAIL_CLOSED_ON_CP_LOSS` | _(implied by `OSTIARI_ENV`)_ | Explicit override for the deny-by-default-on-registration-failure behavior. Set `true`/`false` to decide independently of `OSTIARI_ENV`. |
| `OSTIARI_SSRF_ALLOW` | _(none)_ | Comma-separated hosts/CIDRs exempt from the production private-IP block, for tools that legitimately live on internal addresses. Link-local and metadata addresses (169.254.169.254) are blocked in **every** environment and cannot be allowlisted. |
| `OSTIARI_HITL` | `off` | `on` enables human-in-the-loop for the *intervene* tier: a mid-band call returns **202** with an approval id instead of executing, and the caller re-submits with `X-Approval-Id` once a human approves. **Set this in production** — see below. |
| `OSTIARI_REQUIRE_AXON` | _(unset)_ | Apply the production fail-closed embedded-router contract outside production. The gateway image already contains pinned AxonLLM `v0.3.1`; `OSTIARI_ENV=production` makes it mandatory whenever the LLM module is active. Tool-only gateways may leave the module disabled. |
| `OSTIARI_CONFIG_ADMIN_KEY` | _(none)_ | Required in production and must be at least 32 characters. Protects gateway configuration reads and writes. |
| `OSTIARI_WORKLOAD_TOKEN_FILE` | _(none)_ | Path to a short-lived projected workload token. The file is reread for every control-plane request so rotation does not require a restart. Configure this **or** OAuth client credentials. |
| `OSTIARI_WORKLOAD_TOKEN_URL` / `OSTIARI_WORKLOAD_CLIENT_ID` | _(none)_ | Per-gateway OAuth 2.0 client-credentials endpoint and client id. Required for OAuth mode; the gateway refreshes access tokens before expiry. Production requires an HTTPS token URL. |
| `OSTIARI_WORKLOAD_CLIENT_SECRET` / `_FILE` | _(none)_ | Per-gateway OAuth client secret, injected directly or read from a mounted file. Configure exactly one. Do not reuse one client across gateway ids. |
| `OSTIARI_WORKLOAD_SCOPE` / `OSTIARI_WORKLOAD_TOKEN_AUDIENCE` | _(none)_ | Optional token-request scope and audience. The audience must match the control plane's `OSTIARI_WORKLOAD_OIDC_AUDIENCE`. |
| `OSTIARI_WORKLOAD_CLIENT_AUTH_METHOD` | `client_secret_basic` | OAuth token endpoint authentication: `client_secret_basic` or `client_secret_post`. |
| `OSTIARI_SERVICE_TOKEN` / `OSTIARI_INGEST_KEY` | _(none)_ | Legacy local-development compatibility only. Production startup rejects both fleet-wide shared credentials. |
| `OSTIARI_GATEWAY_AUTH` | `off` | Must be `required` in production. Authentication covers tool, validation, LLM shim, native invoke, model metadata, MCP, and A2A ingress. |
| `OSTIARI_OIDC_ISSUER` / `OSTIARI_OIDC_AUDIENCE` | _(none)_ | HTTPS token issuer and exact audience required for gateway agent authentication in production. Verified token claims determine the effective agent identity. |
| `OSTIARI_GATEWAY_RATE_LIMIT_RPM` | `0` | Must be a positive integer in production. Redis makes the per-caller window fleet-wide. |
| `OSTIARI_REQUIRE_REDIS` | _(false)_ | Must be true in production. Redis startup/runtime failure denies shared enforcement and durable event delivery, and makes `/ready` return 503. |
| `REDIS_ENDPOINT` | _(none)_ | Redis host for fleet-wide rate-limit / budget / wallet state and gateway-scoped trace, cost, payment, and budget-alert outboxes |
| `REDIS_PORT` | `6379` | Redis port |
| `OSTIARI_REDIS_URL` | _(none)_ | Full URL alternative to the two above (`redis://[:pass@]host:port/db`); checked first |
| `OSTIARI_REDIS_PREFIX` | `ostiari` | Key namespace, so several gateways or tenants can share one Redis |
| `OSTIARI_X402_MODE` | `simulated` | Dev supports `simulated`. Production permits only `off` or `live`; `off` rejects settlement rather than using a demo ledger. |
| `OSTIARI_MAX_TOOL_RESPONSE_BYTES` | `1048576` | Maximum decompressed bytes retained from one downstream tool response (server-capped at 16 MiB). |
| `OSTIARI_OUTBOUND_TIMEOUT_SECONDS` | `30` | Absolute wall-clock deadline for outbound tool requests (server-capped at 120 seconds). |
| `OSTIARI_MCP_MAX_RESPONSE_BYTES` / `OSTIARI_MCP_GATEWAY_TIMEOUT_SECONDS` | `1048576` / `30` | Equivalent limits for the standalone MCP bridge. |
| `ANTHROPIC_API_KEY` | _(none)_ | Anthropic API key for LLM routing |
| `OPENAI_API_KEY` | _(none)_ | OpenAI API key for LLM routing |

**Control plane:**

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | SQLite in `OSTIARI_DATA_DIR` | `sqlite+aiosqlite:///…` (dev) or `postgresql+asyncpg://user:pass@host:5432/ostiari` (prod) |
| `OSTIARI_DATA_DIR` | `control-plane/data` | Development-only SQLite directory and location checked for a legacy `state.json` import. Production uses PostgreSQL and requires no backend data volume. |
| `OSTIARI_NO_DEMO` | _(unset)_ | Set to `1` to start with an empty control plane (no seeded demo data) |
| `OSTIARI_TENANCY_MODE` / `OSTIARI_ORG_ID` | `single` / `default` | `single` fixes all requests to `OSTIARI_ORG_ID`; `multi` requires explicit tenant claims on user and workload tokens. The production Kubernetes template sets `multi` explicitly. |
| `OSTIARI_GATEWAY_CALLBACK_ALLOW` | _(none)_ | Required in production. Comma-separated gateway callback hostnames or CIDRs; registration and operator CRUD reject destinations outside it, and metadata/link-local addresses are always blocked. |
| `OSTIARI_CORS_ORIGINS` | _(all, no creds)_ | Comma-separated allowed origins (enables credentialed CORS) |
| `OSTIARI_REQUIRE_AUTH` | _(unset)_ | Set to require API authentication |
| `OSTIARI_ENV` | _(unset = dev)_ | `production` activates a fatal posture check requiring authentication, no demo seed, PostgreSQL, strong machine secrets, durable encryption, and explicit HTTPS origins. |
| `OSTIARI_ADMIN_PASSWORD` | _(dev seed)_ | **Required in production.** Without it the control plane refuses to seed an admin at all. |
| `OSTIARI_JWT_SECRET` | _(dev default)_ | **Required in production**, ≥32 chars. Startup fails otherwise. |
| `OSTIARI_WORKLOAD_OIDC_ISSUER` / `OSTIARI_WORKLOAD_OIDC_AUDIENCE` | _(none)_ | Dedicated workload-token issuer and exact audience. Both are required in production; the issuer must use HTTPS. |
| `OSTIARI_WORKLOAD_OIDC_JWKS_URL` | issuer discovery | Optional explicit JWKS endpoint for issuers whose discovery document does not expose the correct key URL. |
| `OSTIARI_WORKLOAD_GATEWAY_ID_CLAIM` | `gateway_id` | Optional claim name that binds a token directly to a gateway id. Standard OAuth client-credentials tokens may omit it; the verified issuer/subject pair is still bound immutably to one gateway on first registration. |
| `OSTIARI_SERVICE_TOKEN` / `OSTIARI_INGEST_KEY` | _(none)_ | Legacy local-development compatibility only. The production posture check rejects both values. |
| `OSTIARI_CONFIG_ADMIN_KEY` | _(none)_ | Credential the control plane sends on gateway `/config/*` calls. Set the same value on the control plane and gateways. |
| `OSTIARI_GATEWAY_AGENT_TOKEN` / `OSTIARI_GATEWAY_AGENT_ID` | _(none)_ | Dedicated agent credential used for control-plane initiated execution. Required in production; browser JWTs are never forwarded to gateways. |
| `OSTIARI_SANDBOX_GATEWAY_TOKEN` | _(caller bearer)_ | Optional dedicated bearer credential for Sandbox Code tool calls to protected gateways. Store it as a secret. |
| `OSTIARI_SANDBOX_GATEWAY_AGENT_ID` | `sandbox-code` | Gateway agent identity asserted with the dedicated Sandbox bearer token; it must match the token's identity claim. |
| `OSTIARI_ENCRYPTION_KEY` | _(ephemeral)_ | Encrypts stored provider API keys. Unset, a new key is minted per process — stored keys become unreadable after restart. |
| `OIDC_ISSUER` / `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | _(none)_ | Enable the dashboard's browser SSO authorization-code flow. |
| `OIDC_REDIRECT_URI` | `http://localhost:8400/api/auth/sso/callback` | Public backend callback URL registered verbatim with the identity provider. |
| `OSTIARI_FRONTEND_URL` | `http://localhost:9000` | Public dashboard origin used after the backend completes SSO. |
| `OSTIARI_AUTH_MODE=oidc` + `OSTIARI_OIDC_*` | _(local tokens)_ | Direct IdP bearer-token validation for API clients. This is separate from dashboard SSO. |
| `OSTIARI_PROXY_MAX_RESPONSE_BYTES` / `OSTIARI_PROXY_TIMEOUT_SECONDS` | `1048576` / `30` | Byte cap and absolute deadline for dashboard-to-gateway proxy responses. |

Every production lifecycle and event request uses a short-lived workload Bearer
token. On first registration the control plane binds the token's verified
issuer/subject pair to that gateway row. Every subsequent heartbeat, config,
approval, trace, cost, payment, quota-alert, and spend request must match that
binding and the gateway id carried in the path or body. If the token includes
the configured gateway-id claim, that claim must also match. JWKS/provider
outages fail closed with a retryable 503.

For dashboard SSO, leave `OSTIARI_AUTH_MODE` unset (`local`). The backend
exchanges the authorization code, provisions or updates the local user, and
issues an Ostiari JWT that the frontend validates before storing. Set
`OSTIARI_AUTH_MODE=oidc` only when callers present IdP access tokens directly.

## Secrets Management

For Kubernetes, create a secret:

```bash
kubectl create secret generic ostiari-secrets \
  --from-literal=database-url='postgresql+asyncpg://...' \
  --from-literal=jwt-secret='...' \
  --from-literal=admin-password='...' \
  --from-literal=encryption-key='...' \
  --from-literal=config-admin-key='...' \
  --from-literal=workload-client-secret='one-client-secret-per-gateway' \
  --from-literal=gateway-agent-token='...' \
  --from-literal=redis-url='rediss://...' \
  --from-literal=oidc-client-id='...' \
  --from-literal=oidc-client-secret='...'
```

Add the x402, Stripe, and provider keys only for the features you enable. For
ECS, store the equivalent values in AWS Secrets Manager and reference them in
the task definition. Never put secrets directly in Helm values, ConfigMaps, task
definition environment arrays, or image layers.

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
  container cannot rewrite its own code or install anything:
  - **gateway** → `/dev/shm`, the runtime-provided writable tmpfs where it
    renders pushed policies to a process-lifetime tempfile;
  - **frontend** → `/dev/shm`, the runtime-provided writable tmpfs where nginx
    keeps its pid file and all five temp dirs without a platform-specific mount;
  - **control-plane backend** → no writable mount in production; PostgreSQL owns
    all durable runtime and governance state.

  The control-plane image still defaults to `/data` with SQLite for local use.
  Production overrides `DATABASE_URL` with PostgreSQL and never calls the legacy
  JSON writer, so `readOnlyRootFilesystem` requires no application data mount.
- **Database and migrations:** SQLite is development-only. Production startup
  rejects it. Run `alembic upgrade head` as a release/init job before the API;
  the control-plane image now includes the migration files.
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
- **Production is fail-closed at startup.** `OSTIARI_ENV=production` refuses
  incomplete identity, machine-secret, Redis, database, CORS, encryption, and
  settlement configuration. Gateway and control-plane deployments must use the
  same explicit `OSTIARI_ORG_ID`; tokens for a missing or different tenant are
  rejected. There is no separate strict-mode switch.
- **Scaling & fleet-wide limits**: Enforcement state — the rate limiter,
  quota/budget counters, and payment wallets — is **in-process by default**, so
  a horizontally-scaled fleet enforces limits **per replica** (N instances ⇒ N×
  the effective `rate_limit_rpm`/`budget_limit_usd`; wallet balances diverge per
  pod). To make limits hold **fleet-wide**, point the gateway at Redis (below);
  the rate limiter, budget reservations, and wallet debits then run as atomic
  operations against shared Redis state, correct across replicas.
- **Redis (shared state)**: The deployment image installs the Redis extra. Set
  `REDIS_ENDPOINT`
  (+ optional `REDIS_PORT`, default 6379) or `OSTIARI_REDIS_URL`
  (`rediss://[:pass@]host:port/db` for production). Production startup refuses a
  missing or unreachable store. Runtime Redis failures deny rate/budget/payment
  mutations and make `/ready` return 503; `/health` remains liveness-only.
  `OSTIARI_REDIS_PREFIX` (default `ostiari`) namespaces keys so several
  deployments can share one Redis.
- **Readiness differs from liveness.** Route traffic only to `/ready`-healthy
  gateway tasks and `/api/ready`-healthy control-plane tasks. Use `/health` and
  `/api/health` only for process liveness.
- **Settlement mode is explicit.** Development may use `simulated`. Production
  accepts `off` or `live`; `off` uses a disabled backend that cannot debit the
  demo ledger if payment policy is accidentally enabled.
- **Control-plane replicas are coordinated.** PostgreSQL is authoritative for
  runtime configuration, approvals, traces, SSO state, and offline updates.
  Each runtime-state transaction advances a tenant/namespace revision; replicas
  poll only changed namespaces. Redis provides live trace fan-out, atomic
  session roots, singleton health-sweep leases, and fleet-wide rate limits.
  Production startup and `/api/ready` fail closed when Redis is unavailable.
- **Authenticating `/config`.** With `OSTIARI_CONFIG_ADMIN_KEY` set, callers
  present it as `X-Config-Admin-Key: <key>` or `Authorization: Bearer <key>`.
