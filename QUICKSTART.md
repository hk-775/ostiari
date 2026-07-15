# Ostiari — Quick Start Guide

Choose your path:

| Path | Command | Time | For |
|------|---------|------|-----|
| [**1. Demo Mode**](#1-demo-mode-frontend-only) | `make demo` | 2 min | See the UI with mock data, no backend needed |
| [**2. Full Demo Stack**](#2-full-demo-stack) | `make demo-full` | 5 min | All components running with seeded demo data |
| [**3. Clean Install**](#3-clean-install) | `make clean-start` | 15 min | Fresh start, no demo data, connect your own agents |

---

## 1. Demo Mode (Frontend Only)

See the full UI with mock data. No backend, no gateways, no credentials needed.

```bash
make demo
```

Open **http://localhost:9000** — that's it.

You'll see:
- Dashboard with live metrics
- 14 configured models with pricing
- Per-agent model access and budgets
- Policies, quotas, tools, MCP servers
- Architecture demo with narration
- Sandbox (chat, scenarios, A2A)

Everything runs client-side with mock data.

---

## 2. Full Demo Stack

All components running with real API responses and seeded demo data. Five gateways, nine agents, an A2A demo agent, and the full control plane.

### Prerequisites

- Python 3.10+
- Node.js 18+

### Install

```bash
git clone https://github.com/aws-samples/sample-ostiari.git
cd ostiari

# Core library + gateway
pip install -e .
pip install -e gateway/

# Frontend
cd control-plane/frontend && npm install && cd ../..
```

### Start all components

```bash
make demo-full
```

This starts everything in the background:
- **Control Plane backend** on port 8400 (loads demo data from `control-plane/backend/data/state.json`)
- **Control Plane frontend** on http://localhost:9000
- **5 Gateways** on ports 8421–8425
- **A2A Demo Agent** on port 9200

### What's running

| Component | URL | Description |
|---|---|---|
| Control Plane UI | http://localhost:9000 | Dashboard, config, sandbox |
| Control Plane API | http://localhost:8400 | Backend API |
| Primary Gateway | http://localhost:8421 | `dev-gateway` |
| Ops Gateway | http://localhost:8422 | `ops-gateway` |
| DevOps Gateway | http://localhost:8423 | `devops-gateway` |
| Analytics Gateway | http://localhost:8424 | `analytics-gateway` |
| CRM Gateway | http://localhost:8425 | `crm-gateway` (3 tools pre-loaded) |
| A2A Demo Agent | http://localhost:9200 | DevOps Assistant (deploy, rollback, status) |

### Try it

```bash
# Verify gateways are healthy
curl http://localhost:8421/health
curl http://localhost:8422/health

# Call a tool through the CRM gateway (pre-configured with example-config.yaml)
curl -X POST http://localhost:8425/tool/send_email \
  -H "Content-Type: application/json" \
  -H "X-Agent-Id: crm-agent" \
  -d '{"to": "user@example.com", "subject": "test", "body": "hello"}'

# A2A: discover the demo agent
curl http://localhost:9200/.well-known/agent.json

# A2A: send a task
curl -X POST http://localhost:9200/a2a \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tasks/send","id":"1","params":{"message":{"parts":[{"type":"text","text":"Deploy auth-service to staging"}]}}}'
```

In the Control Plane UI:
1. **Sandbox > A2A tab** — enter `http://localhost:9200`, click Discover, send tasks
2. **Gateways** — see all 5 gateways registered and heartbeating
3. **Models** — 14 models with routing rules and pricing
4. **Agents** — 9 pre-configured agents across frameworks

### Demo data details

The backend loads seeded state from `control-plane/backend/data/state.json`:
- 8 quota configurations across gateways
- 14 model routing configs (Anthropic, OpenAI, Bedrock, Mistral)
- 5 experiments (A/B tests, canary deployments)

Agents are seeded in-memory (see `control-plane/backend/control_plane/routers/agents.py`):
- research-agent (OpenAI), ops-agent (Strands), claude-agent (Anthropic)
- bedrock-agent, agentcore-agent, crewai-agent, langgraph-agent
- planner-bot, smart-router-bot (gateway-invoke)

---

## 3. Clean Install

Fresh Ostiari with no demo data. You register your own gateways, agents, and tools.

### Prerequisites

- Python 3.10+
- Node.js 18+

### Install

```bash
git clone https://github.com/aws-samples/sample-ostiari.git
cd ostiari

pip install -e .
pip install -e gateway/
cd control-plane/frontend && npm install && cd ../..
```

### Start fresh

```bash
make clean-start
```

This wipes demo data and starts:
- **Control Plane backend** on port 8400 (empty state)
- **Control Plane frontend** on http://localhost:9000
- **One gateway** (`my-gateway`) on port 8421, connected to the control plane

### Register the gateway with the Control Plane

```bash
curl -X POST http://localhost:8400/api/gateways \
  -H "Content-Type: application/json" \
  -d '{
    "id": "my-gateway",
    "name": "My Gateway",
    "endpoint": "http://localhost:8421",
    "description": "Primary gateway"
  }'
```

The gateway auto-registers via its lifecycle heartbeat, but creating it in the Control Plane first gives it a name and makes it visible in the UI immediately.

### Register a tool

```bash
curl -X POST http://localhost:8421/config/tools \
  -H "Content-Type: application/json" \
  -d '{
    "tools": [{
      "name": "send_email",
      "endpoint": "http://your-service:8080/send",
      "method": "POST",
      "description": "Send an email to a recipient"
    }]
  }'
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

### Set quotas

```bash
curl -X POST http://localhost:8421/config/quota \
  -H "Content-Type: application/json" \
  -d '{
    "rate_limit_rpm": 60,
    "budget_limit_usd": 100.0,
    "max_tokens_per_request": 4096
  }'
```

### Point your agent at the gateway

```python
import requests

GATEWAY = "http://localhost:8421"

resp = requests.post(f"{GATEWAY}/tool/send_email",
    json={"to": "user@example.com", "body": "Hello"},
    headers={"X-Agent-Id": "my-agent"})

if resp.status_code == 200:
    print(resp.json()["result"])
elif resp.status_code == 403:
    print(resp.json()["reason"])  # Blocked by policy
```

### Add an MCP server (optional)

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

### Verify it works

```bash
# Should succeed (allowed tool)
curl -X POST http://localhost:8421/tool/send_email \
  -H "Content-Type: application/json" \
  -H "X-Agent-Id: my-agent" \
  -d '{"to": "user@example.com", "body": "test"}'

# Should be blocked (*.delete pattern)
curl -X POST http://localhost:8421/tool/db_delete \
  -H "Content-Type: application/json" \
  -H "X-Agent-Id: my-agent" \
  -d '{"table": "users"}'
# → 403 {"blocked": true, "reason": "..."}
```

### Open the Control Plane

Visit **http://localhost:9000** to manage your gateway, agents, and traces.

---

## Environment variables

```bash
# Gateway
OSTIARI_GATEWAY_ID=my-gateway
OSTIARI_CONTROL_PLANE_URL=http://localhost:8400
OSTIARI_PORT=8421

# LLM credentials (for model routing)
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
AWS_REGION=us-east-1  # for Bedrock

# Observability (optional)
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
OTEL_SERVICE_NAME=ostiari-gateway
```

---

## Production deployment

See the full [Production Guide](docs/production.md) for Docker, Kubernetes, ECS, Helm, and Lambda deployments.

| Target | Command |
|--------|---------|
| Docker Compose | `cd deploy/docker && docker-compose up` |
| Kubernetes (shared) | `kubectl apply -f deploy/kubernetes/gateway-shared.yaml` |
| Kubernetes (sidecar) | `kubectl apply -f deploy/kubernetes/gateway-sidecar.yaml` |
| ECS Fargate | `aws ecs register-task-definition --cli-input-json file://deploy/ecs/task-definition.json` |
| Helm | `helm install ostiari deploy/helm/ostiari-gateway` |
| Lambda | `cd deploy/lambda && sam deploy --guided` |
