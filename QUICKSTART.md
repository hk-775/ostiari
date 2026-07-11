# Ostiari — Quick Start Guide

Choose your path:

| Path | Time | For |
|------|------|-----|
| [**1. Demo Mode**](#1-demo-mode) | 2 min | See everything working with pre-loaded data |
| [**2. Bootstrap Your Agents**](#2-bootstrap-your-agents) | 15 min | Clean install, connect your own agents step by step |
| [**3. Enterprise Production**](#3-enterprise-production) | 1 hour | Full production stack with auth, TLS, HA, monitoring |

---

## 1. Demo Mode

See the full platform running with mock data. No backend, no credentials, no AWS account needed.

```bash
git clone https://github.com/aws-samples/sample-ostiari.git
cd ostiari/control-plane/frontend
npm install
npm run dev
```

Open **http://localhost:9000** — that's it.

You'll see:
- Dashboard with live metrics
- 14 configured models with pricing
- Per-agent model access and budgets
- Policies, quotas, tools, MCP servers
- Architecture demo with narration
- Sandbox (chat, scenarios, A2A)

**Everything runs in demo mode with mock data — no gateway or backend required.**

---

## 2. Bootstrap Your Agents

Clean install. No demo data. You connect your own agents to Ostiari.

### Prerequisites

- Python 3.10+
- Node.js 18+

### Install

```bash
git clone https://github.com/aws-samples/sample-ostiari.git
cd ostiari

# Install core library + gateway
pip install -e .
pip install -e gateway/

# Install frontend
cd control-plane/frontend && npm install && cd ../..
```

### Start the stack

```bash
# Terminal 1: Gateway
python -m ostiari_gateway.main \
  --port 8421 \
  --sidecar-id my-gateway \
  --control-plane http://localhost:8400

# Terminal 2: Control Plane backend
cd control-plane/backend && python main.py

# Terminal 3: Control Plane frontend
cd control-plane/frontend && npm run dev
```

### Register your first tool

```bash
curl -X POST http://localhost:8421/config/tools \
  -H "Content-Type: application/json" \
  -d '{
    "tools": [{
      "name": "send_email",
      "endpoint": "http://your-service:8080/send",
      "method": "POST",
      "description": "Send an email"
    }]
  }'
```

### Point your agent at the gateway

```python
import requests

GATEWAY = "http://localhost:8421"

# One line change — replace direct tool URL with the gateway
resp = requests.post(f"{GATEWAY}/tool/send_email",
    json={"to": "user@example.com", "body": "Hello"},
    headers={"X-Agent-Id": "my-agent"})

if resp.status_code == 200:
    print(resp.json()["result"])      # Tool succeeded
elif resp.status_code == 403:
    print(resp.json()["reason"])      # Blocked by policy
```

### Add a policy

```bash
curl -X POST http://localhost:8421/config/policy \
  -H "Content-Type: application/json" \
  -d '{
    "block": ["*.delete", "*.drop", "*.destroy"],
    "allow": ["send_email", "db_query"],
    "rules": [
      {"type": "risk_adjust", "action": "send_email", "risk_adjust": 25}
    ],
    "thresholds": {
      "global": {"allow_max": 30, "intervene_max": 70}
    }
  }'
```

### Set per-agent access control

```bash
curl -X POST http://localhost:8421/config/agent-auth \
  -H "Content-Type: application/json" \
  -d '{
    "enabled": true,
    "agents": {
      "my-agent": {
        "allowed_tools": ["send_email", "db_query"],
        "allowed_models": ["claude-haiku-4-5", "gpt-4o-mini"],
        "allowed_providers": ["anthropic", "openai"],
        "budget_usd": 10.00
      }
    }
  }'
```

### Set quota limits

```bash
curl -X POST http://localhost:8421/config/quota \
  -H "Content-Type: application/json" \
  -d '{
    "rate_limit_rpm": 60,
    "budget_limit_usd": 100.0,
    "max_tokens_per_request": 4096
  }'
```

### Verify it works

```bash
# This should succeed (allowed)
curl -X POST http://localhost:8421/tool/send_email \
  -H "Content-Type: application/json" \
  -H "X-Agent-Id: my-agent" \
  -d '{"to": "user@example.com", "body": "test"}'

# This should be blocked (*.delete pattern)
curl -X POST http://localhost:8421/tool/db_delete \
  -H "Content-Type: application/json" \
  -H "X-Agent-Id: my-agent" \
  -d '{"table": "users"}'
# → 403 {"blocked": true, "reason": "..."}
```

### Add MCP servers (optional)

```bash
curl -X POST http://localhost:8421/config/mcp-servers \
  -H "Content-Type: application/json" \
  -d '{
    "name": "github",
    "transport": "sse",
    "url": "http://your-github-mcp:3000"
  }'
```

Your agent can now call `POST /tool/github.create_issue` — the gateway handles MCP protocol translation.

### Open the Control Plane

Visit **http://localhost:9000** to see your gateway, agent, and traces in the UI.

---

## 3. Enterprise Production

Full production deployment with security, high availability, and observability.

### Choose your deployment target

| Target | Command |
|--------|---------|
| Docker Compose | `cd deploy/docker && docker-compose up` |
| Kubernetes (shared) | `kubectl apply -f deploy/kubernetes/gateway-shared.yaml` |
| Kubernetes (sidecar) | `kubectl apply -f deploy/kubernetes/gateway-sidecar.yaml` |
| ECS Fargate | `aws ecs register-task-definition --cli-input-json file://deploy/ecs/task-definition.json` |
| Helm | `helm install ostiari deploy/helm/ostiari-gateway` |
| Lambda | `cd deploy/lambda && sam deploy --guided` |

### Production checklist

| Requirement | How |
|---|---|
| **Authentication** | Add OIDC/JWT auth in front of Control Plane (Cognito, Auth0, Okta) |
| **TLS** | Terminate at load balancer (ALB, nginx, Istio) or set `--ssl-keyfile` |
| **Database** | Replace SQLite with PostgreSQL: `DATABASE_URL=postgresql+asyncpg://...` |
| **Secrets** | Store LLM API keys in AWS Secrets Manager / Vault, reference via env vars |
| **High availability** | Run 3+ gateway replicas behind a load balancer (see Helm HPA config) |
| **Observability** | Set `OTEL_EXPORTER_OTLP_ENDPOINT` to export traces to Datadog/Splunk/X-Ray |
| **Network isolation** | K8s NetworkPolicy: agents can ONLY reach their gateway, not backend services directly |
| **Backup** | Control Plane DB backups (RDS automated) + gateway config in version control |
| **RBAC** | Role-based access on Control Plane: admin (full), operator (config), viewer (read-only) |

### Environment variables

```bash
# Gateway
OSTIARI_GATEWAY_ID=prod-gateway-1
OSTIARI_CONTROL_PLANE_URL=https://control-plane.internal:8400
OSTIARI_PORT=8421

# LLM credentials (from Secrets Manager)
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
AWS_REGION=us-east-1  # for Bedrock

# Observability
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
OTEL_SERVICE_NAME=ostiari-gateway

# Redis (for rate limiting at scale)
REDIS_ENDPOINT=redis.internal
REDIS_PORT=6379
```

### Production architecture

```
                    ┌─────────────────────────────────┐
                    │        Control Plane             │
                    │   (PostgreSQL + React UI)        │
                    │   Push config, collect traces    │
                    └──────────┬──────────────────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
     ┌────────▼──┐    ┌───────▼───┐    ┌───────▼───┐
     │ Gateway 1 │    │ Gateway 2 │    │ Gateway 3 │
     │ (Team A)  │    │ (Team B)  │    │ (Team C)  │
     └─────┬─────┘    └─────┬─────┘    └─────┬─────┘
           │                 │                │
     ┌─────▼─────┐    ┌─────▼─────┐    ┌─────▼─────┐
     │ Agent A1  │    │ Agent B1  │    │ Agent C1  │
     │ Agent A2  │    │ Agent B2  │    │ Agent C2  │
     └───────────┘    └───────────┘    └───────────┘
```

### Scaling guidance

| Component | How to scale |
|---|---|
| **Gateway** | Stateless — add replicas behind ALB. HPA on CPU/request count. |
| **Control Plane** | Stateless (with external DB) — 2-3 replicas. |
| **Database** | RDS Multi-AZ for Control Plane state. |
| **Redis** | ElastiCache cluster for shared rate limiting across gateway replicas. |

### Cost estimate (moderate traffic)

| Component | Monthly |
|---|---|
| 3× Gateway (Fargate 0.5vCPU/1GB) | ~$45 |
| Control Plane (Fargate 0.5vCPU/1GB) | ~$15 |
| RDS PostgreSQL (db.t3.micro) | ~$15 |
| ElastiCache Redis (cache.t3.micro) | ~$12 |
| ALB | ~$18 |
| **Total** | **~$105/month** |

---

## What's next

- **Landing page**: http://localhost:9000 — product overview
- **Architecture demo**: http://localhost:9000/architecture — animated walkthrough with narration
- **Sandbox**: http://localhost:9000/sandbox — test tool calls, A2A, and chat
- **API docs**: `curl http://localhost:8421/docs` (FastAPI auto-generated)
- **Deployment configs**: `deploy/` directory (Docker, K8s, ECS, Helm, Lambda)
