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
| `OSTIARI_GATEWAY_ID` | `sidecar-1` | Unique gateway instance identifier |
| `OSTIARI_CONTROL_PLANE_URL` | _(none)_ | Control plane backend URL (enables register/heartbeat) |
| `OSTIARI_PORT` | `8421` | Gateway listen port |
| `OSTIARI_ADVERTISE_HOST` | _(bind host)_ | Host the control plane pushes config back to. Set this to the gateway's network-reachable name (compose service, k8s Service DNS, ECS service). Without it, config pushes may not reach the gateway. |
| `REDIS_ENDPOINT` | _(none)_ | Redis host for distributed state |
| `REDIS_PORT` | `6379` | Redis port |
| `ANTHROPIC_API_KEY` | _(none)_ | Anthropic API key for LLM routing |
| `OPENAI_API_KEY` | _(none)_ | OpenAI API key for LLM routing |

**Control plane:**

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | SQLite in `data/` | `sqlite+aiosqlite:///…` (dev) or `postgresql+asyncpg://user:pass@host:5432/ostiari` (prod) |
| `OSTIARI_NO_DEMO` | _(unset)_ | Set to `1` to start with an empty control plane (no seeded demo data) |
| `OSTIARI_CORS_ORIGINS` | _(all, no creds)_ | Comma-separated allowed origins (enables credentialed CORS) |
| `OSTIARI_REQUIRE_AUTH` | _(unset)_ | Set to require API authentication |

## Secrets Management

For Kubernetes, create a secret:

```bash
kubectl create secret generic ostiari-secrets \
  --from-literal=anthropic-api-key=sk-ant-... \
  --from-literal=openai-api-key=sk-...
```

For ECS, store secrets in AWS Secrets Manager and reference them in the task definition.

## Production Notes

- **Database**: The control plane uses SQLite for dev. For production, configure PostgreSQL via RDS.
- **TLS**: Terminate TLS at the load balancer or ingress controller, not at the gateway.
- **Scaling & per-replica limits**: Each gateway holds its enforcement state
  **in-process** — the rate limiter, quota/budget counters, and payment wallets
  are per-instance. Config (tools/policy/quotas) is pushed/pulled fleet-wide, but
  the *running counters* are not shared. Consequently, quantitative limits are
  enforced **per replica, not fleet-wide**: with N gateway instances an effective
  `budget_limit_usd`/`rate_limit_rpm` becomes N× the configured value, and a
  wallet balance is tracked independently per instance. Plan capacity per replica
  accordingly, and pin agents to a stable instance (or run a single instance)
  where an exact global cap matters.
- **Redis**: The `REDIS_ENDPOINT`/`REDIS_PORT` env vars are surfaced by the
  deploy manifests for a future shared-state backend, but the gateway does **not**
  yet use Redis — enforcement state is in-process regardless of whether Redis is
  reachable. Sharing rate-limit/quota/wallet state across replicas via Redis is
  tracked as follow-up work.
