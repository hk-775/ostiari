# Ostiari Deployment Guide

## Deployment Options

### Docker Compose (Local Development)

Full local stack with gateway, control plane, and Redis:

```bash
cd deploy/docker
docker compose up --build
```

Gateway: http://localhost:8421
Control Plane API: http://localhost:8400
Control Plane UI: http://localhost:9000

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

Deploy using AWS SAM:

```bash
cd deploy/lambda
pip install mangum ostiari-gateway -t .
sam build
sam deploy --guided
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OSTIARI_GATEWAY_ID` | `gateway-1` | Unique gateway instance identifier |
| `OSTIARI_CONTROL_PLANE_URL` | `http://control-plane:8400` | Control plane backend URL |
| `OSTIARI_PORT` | `8421` | Gateway listen port |
| `REDIS_ENDPOINT` | _(none)_ | Redis host for distributed state |
| `REDIS_PORT` | `6379` | Redis port |
| `ANTHROPIC_API_KEY` | _(none)_ | Anthropic API key for LLM routing |
| `OPENAI_API_KEY` | _(none)_ | OpenAI API key for LLM routing |

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
- **Redis**: Enable Redis for distributed rate limiting and session state across multiple gateway instances.
- **TLS**: Terminate TLS at the load balancer or ingress controller, not at the gateway.
- **Scaling**: The gateway is stateless (with Redis). Scale horizontally without concern.
