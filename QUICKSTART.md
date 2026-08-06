# Ostiari — Quick Start Guide

For the complete capability map and runtime flows, see
[`docs/features-and-flows.md`](docs/features-and-flows.md).

Choose your path:

| Path | Command | Time | For |
|------|---------|------|-----|
| [**1. Frontend Only**](#1-frontend-only) | `make demo` | 2 min | Landing page + build check; no backend, so no dashboard |
| [**2. Full Demo Stack**](#2-full-demo-stack) | `make demo-full` | 5 min | All components running with seeded demo data |
| [**3. Clean Install**](#3-clean-install) | `make clean-start` | 15 min | Fresh start, no demo data, connect your own agents |

---

## 1. Frontend Only

Starts the Vite dev server and nothing else.

```bash
make demo
```

Open **http://localhost:9000** for the landing page.

**This is not a working dashboard.** There is no client-side mock data layer:
every page except the landing page is behind `RequireAuth`, sign-in POSTs to
`/api/auth/login` on the backend, and each page fetches from the API. With no
backend on :8400 the login fails and every route redirects back to `/login`.

Use this to check the frontend builds and to work on the landing page. For a
populated dashboard you want [Full Demo Stack](#2-full-demo-stack) — it's one
command and needs no credentials either.

---

## 2. Full Demo Stack

All components running with real API responses and seeded demo data. Four gateways, nine agents, an A2A demo agent, and the full control plane.

### Prerequisites

- Python 3.11+ for the full platform (the core library and gateway support 3.10+)
- Node.js 18+

### Install

```bash
git clone https://github.com/hk-775/ostiari.git
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
- **Control Plane backend** on port 8400 (seeds demo data on first start; restores saved state from `control-plane/data/state.json` on later starts)
- **Control Plane frontend** on http://localhost:9000
- **4 Gateways** on ports 8421, 8422, 8424, 8425
- **A2A Demo Agent** on port 9200
- **Demo Tools server** on port 9300 (canned backends for web_search/db_query/github.*/drawio.*)

Each gateway starts with the **same ID as its control-plane record** (`crm-agent`, `ops-agent`, `devops-agent`, `analytics-agent`). That's what lets the control plane push each gateway its seeded tools and policy on registration — start them with any other ID and they come up with no tools, and Sandbox calls won't resolve or produce traces.

The `crm-agent` gateway also loads `llm-gateway-config.yaml` (enables the LLM module + credentials for the Sandbox **Chat** tab), and `register_demo_tools.py` points its tools at the demo tools server so tool calls return real data and the block policy actually blocks destructive actions.

### What's running

| Component | URL | Gateway ID | Tools |
|---|---|---|---|
| Control Plane UI | http://localhost:9000 | — | — |
| Control Plane API | http://localhost:8400 | — | — |
| CRM Gateway | http://localhost:8421 | `crm-agent` | web_search, db_query, send_email, github.*, drawio.* (12) — plus the real draw.io + filesystem MCP servers |
| Ops Gateway | http://localhost:8422 | `ops-agent` | db_query, send_email, slack.post, db_delete (4) |
| DevOps Gateway | http://localhost:8424 | `devops-agent` | github.search_code, github.create_issue, web_search, github.delete_repo (4) — `devops-guard` blocks the last one |
| Analytics Gateway | http://localhost:8425 | `analytics-agent` | db_query, web_search, drawio.create_diagram (3) |
| A2A Demo Agent | http://localhost:9200 | — | deploy, rollback, status |
| Demo Tools | http://localhost:9300 | — | canned tool backends |

All four **Sandbox** tabs work against this stack: Chat (LLM + tool calls), Scenarios (allow/block guard demo), Code (tool call), and A2A (discover + send task, routed through the gateway).

### Try it

```bash
# Verify gateways are healthy and have their tools
curl http://localhost:8421/tools
curl http://localhost:8422/tools

# Call a tool through the CRM gateway
curl -X POST http://localhost:8421/tool/send_email \
  -H "Content-Type: application/json" \
  -H "X-Agent-Id: research-agent" \
  -d '{"to": "user@example.com", "subject": "test", "body": "hello"}'

# A2A: discover the demo agent
curl http://localhost:9200/.well-known/agent.json

# A2A: send a task
curl -X POST http://localhost:9200/a2a \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tasks/send","id":"1","params":{"message":{"parts":[{"type":"text","text":"Deploy auth-service to staging"}]}}}'
```

In the Control Plane UI:
1. **Sandbox** — run a scenario or tool call; it routes through the CRM gateway and appears in Live Traces
2. **Sandbox > A2A tab** — enter `http://localhost:9200`, click Discover, send tasks
3. **Live Traces** — pre-seeded with demo traces on startup; new Sandbox/gateway calls stream in live
4. **Gateways** — see all 4 gateways registered and heartbeating
5. **Models** — 18 models with routing rules and pricing
6. **Agents** — 9 pre-configured agents across frameworks

### Demo data details

Nothing is checked in — the backend seeds on first start (`control_plane/demo_seed.py`),
then saves to `control-plane/data/state.json` on shutdown and restores it next time:
- 4 gateway quotas, one per gateway, with spend summed from real usage records
- 18 model routing configs (Anthropic, OpenAI, Bedrock, Vertex, Mistral, xAI, …)
- 3 experiments (`haiku-vs-sonnet`, `gpt4o-vs-o3`, `cost-routing-test`)
- 8 approvals (4 pending, 4 decided), 8 payment wallets, 2 broker pools
- 647 usage records across 8 agents, feeding Costs / Metering / ROI

Set `OSTIARI_NO_DEMO=1` to skip the seeders above (that's what `make clean-start`
does). The 18 models are skipped too, as of this release — `seed_models()` runs at
the bottom of `control_plane/routers/model_config.py` and is now gated on the same
flag.

It used to run unconditionally, on the argument that a model catalog is a routing
table rather than demo content. That argument isn't wrong, but it lost to a simpler
one: `clean-start` promises an empty control plane, and a page showing 18 models you
didn't add is confusing in a way that outweighs the convenience. If you want the
catalog on a clean install, either register the models you actually use via
`POST /api/models` or call `seed_models()` yourself.

Tools, policies, MCP servers, and A2A peers are registered by the
`gateway/register_demo_*.py` scripts the Makefile runs, and the control plane
pushes them to each gateway on registration. The SQLite DB
(`control-plane/data/control_plane.db`) is created on first run, not shipped.

Agents are seeded in-memory (see `control-plane/backend/control_plane/routers/agents.py`):
- research-agent (OpenAI), ops-agent (Strands), claude-agent (Anthropic)
- bedrock-agent, agentcore-agent, crewai-agent, langgraph-agent
- planner-bot, smart-router-bot (gateway-invoke)

Live Traces are seeded on startup (see `seed_traces()` in `control-plane/backend/control_plane/routers/traces.py`) so the view isn't empty. Real Sandbox/gateway calls take precedence and stream in live.

---

## 3. Clean Install

Fresh Ostiari with no demo data. You register your own gateways, agents, and tools.

### Prerequisites

- Python 3.11+ for the full platform (the core library and gateway support 3.10+)
- Node.js 18+

### Install

```bash
git clone https://github.com/hk-775/ostiari.git
cd ostiari

pip install -e .
pip install -e gateway/
cd control-plane/frontend && npm install && cd ../..
```

### Start fresh

```bash
make clean-start
```

This starts:
- **Control Plane backend** on port 8400 (`OSTIARI_NO_DEMO=1`, so no seeders run)
- **Control Plane frontend** on http://localhost:9000
- **One gateway** (`my-gateway`) on port 8421, connected to the control plane

`clean-start` removes the SQLite database and `control-plane/data/state.json` — the
path `data_dir()` resolves to (see `control-plane/backend/control_plane/env.py`). It
also still removes the pre-`data_dir()` path, `control-plane/backend/data/state.json`,
so an older checkout doesn't leave a file behind that a future refactor might start
reading again.

This is worth knowing because the recipe used to delete *only* the old path, while
the lifespan restored the live one **before** the `OSTIARI_NO_DEMO` check — so
previously-seeded quotas, experiments, and providers came back on a "clean" start.

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
    "block": ["*delete*", "*drop*", "*destroy*"],
    "allow": ["send_email", "db_query"],
    "rules": [
      {"type": "risk_adjust", "action": "send_email", "risk_adjust": 25}
    ],
    "thresholds": {
      "global": {"allow_max": 30, "intervene_max": 70}
    }
  }'
```

> **Use `*delete*`, not `*.delete`.** Patterns are `fnmatch` globs, so `*.delete`
> requires a literal dot — it matches `github.delete` but **not** `db_delete`. The
> bare-underscore names are the ones you most want blocked, so a `*.delete` block
> list reads as protective and lets `db_delete` straight through. Test every block
> pattern against your real action names (the Sandbox is for exactly this).

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
    "mode": "remote",
    "url": "http://your-github-mcp:3000"
  }'
```

The field is **`mode`**, and the only accepted values are `remote`, `stdio`, and
`embedded` (the default). `MCPServerConfig` has no `transport` field — an unknown
key is ignored, so passing `"transport": "sse"` silently leaves the server in
`embedded` mode, where it looks for a Python package named `github` and connects to
nothing.

Your agent can now call `POST /tool/github.create_issue` — the gateway handles MCP protocol translation.

### Verify it works

```bash
# Should succeed (allowed tool)
curl -X POST http://localhost:8421/tool/send_email \
  -H "Content-Type: application/json" \
  -H "X-Agent-Id: my-agent" \
  -d '{"to": "user@example.com", "body": "test"}'

# Should be blocked (*delete* pattern)
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

See [`deploy/README.md`](deploy/README.md) for Docker, Kubernetes, ECS, Helm, and Lambda deployments.

| Target | Command |
|--------|---------|
| Docker Compose | `cd deploy/docker && docker-compose up` |
| Kubernetes (shared) | `kubectl apply -f deploy/kubernetes/gateway-shared.yaml` |
| Kubernetes (sidecar) | `kubectl apply -f deploy/kubernetes/gateway-sidecar.yaml` |
| ECS Fargate | `aws ecs register-task-definition --cli-input-json file://deploy/ecs/task-definition.json` |
| Helm | `helm install ostiari deploy/helm/ostiari-gateway` |
| Lambda | `cd deploy/lambda && sam deploy --guided` |
