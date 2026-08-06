# Ostiari — Startup & Deployment Guide

This guide covers **three** ways to stand Ostiari up, end to end:

| Part | Deployment | Who it's for | Time |
|------|-----------|--------------|------|
| [**Part 1**](#part-1--local-install-no-demo-data) | **Local, no demo data** | Evaluators wiring their own agents on a laptop | ~15 min |
| [**Part 2**](#part-2--local-install-full-demo-data) | **Local, full demo data + all gateways** | A guided tour of every feature with seeded data | ~5 min |
| [**Part 3**](#part-3--enterprise-service) | **Enterprise service** (Docker / Kubernetes / Helm / ECS) | Platform teams running Ostiari as shared infrastructure | ~30–60 min |

Parts 1 and 3 include **per-feature configuration** walkthroughs with diagrams. Part 2 is intentionally turnkey — one command brings everything up.

> **Related docs:** [`QUICKSTART.md`](QUICKSTART.md) is the condensed cheat-sheet;
> [`deploy/README.md`](deploy/README.md) is the deployment reference;
> [`docs/control-plane-guide.md`](docs/control-plane-guide.md) covers the UI in
> depth; and [`docs/features-and-flows.md`](docs/features-and-flows.md) is the
> canonical inventory of implemented features and their end-to-end flows.

---

## What Ostiari is (60-second orientation)

Ostiari is a **runtime governance layer for AI agents**. Every tool call, model call, and agent-to-agent message an agent makes passes through a **gateway** that validates it against policy before it executes. A central **control plane** manages the fleet of gateways: you define policy, quotas, budgets, and tool catalogs once, and the control plane pushes them to every gateway.

```
                          ┌─────────────────────────────┐
                          │      CONTROL PLANE          │
                          │  (policy, quotas, catalog,  │
                          │   traces, dashboard UI)     │
                          └───────────┬─────────────────┘
                register / heartbeat  │  push config
                config-bundle / push  │  (tools, policy, quotas…)
              ┌───────────────────────┼───────────────────────┐
              ▼                       ▼                       ▼
      ┌───────────────┐       ┌───────────────┐       ┌───────────────┐
      │   GATEWAY A   │       │   GATEWAY B   │       │   GATEWAY C   │
      │  validate +   │       │  validate +   │       │  validate +   │
      │  proxy calls  │       │  proxy calls  │       │  proxy calls  │
      └───────┬───────┘       └───────┬───────┘       └───────┬───────┘
              ▲                       ▲                       ▲
        tool / LLM / A2A calls  (X-Agent-Id header)
              │                       │                       │
         ┌────┴────┐             ┌────┴────┐             ┌────┴────┐
         │ Agent 1 │             │ Agent 2 │             │ Agent 3 │
         └─────────┘             └─────────┘             └─────────┘
```

**Four components:**

| Component | Path | Default port | Role |
|-----------|------|--------------|------|
| Core library (`ostiari`) | `src/ostiari` | — | The guard engine; can be used standalone without a gateway |
| Gateway | `gateway/ostiari_gateway` | 8421 | Validates + proxies each agent call; registers with the control plane |
| Control-plane backend | `control-plane/backend` | 8400 | FastAPI; fleet management, policy, traces, persistence |
| Control-plane frontend | `control-plane/frontend` | 9000 | React/Vite dashboard UI |

**Prerequisites (Parts 1 & 2):** Python 3.11+ for the full platform (the core
library and gateway support 3.10+), plus Node.js 18+. No AWS account or cloud
credentials are required for local use — all AWS integrations (Bedrock, S3) are
optional and import-guarded.

---

# Part 1 — Local Install (no demo data)

A clean Ostiari with an **empty control plane**: no seeded agents, no demo tools, no traces. You register your own gateway, tools, policy, and agents. This is the honest starting point for evaluating Ostiari against your own workloads.

## 1.1 Install

```bash
git clone https://github.com/hk-775/ostiari.git
cd ostiari

# Core library + gateway (editable installs)
pip install -e .
pip install -e gateway/

# Frontend
cd control-plane/frontend && npm install && cd ../..
```

## 1.2 Start with no demo data

```bash
make clean-start
```

`clean-start` brings up three processes with the seeders off:

```
  ┌──────────────────────────┐        ┌──────────────────────────┐
  │  control-plane backend   │        │  control-plane frontend  │
  │  :8400  (OSTIARI_NO_DEMO)│◀──────▶│  :9000  (dashboard UI)   │
  │  empty registry, empty DB│        └──────────────────────────┘
  └────────────┬─────────────┘
      register / heartbeat / push
               │
  ┌────────────▼─────────────┐
  │        gateway           │
  │  :8421  id=my-gateway    │   ← starts with 0 tools, 0 policy
  └──────────────────────────┘
```

The `OSTIARI_NO_DEMO=1` flag is what skips the demo seeders (agents, traces, experiments, pricing, usage records, approvals, quotas, wallets).

> **What `clean-start` clears.** The SQLite database, both `state.json` paths (the
> live `control-plane/data/state.json` that `data_dir()` resolves to, and the
> pre-`data_dir()` `control-plane/backend/data/state.json`), and — via
> `OSTIARI_NO_DEMO=1` — every seeder including the 18 model routing configs.
>
> Two things previously survived it. The `rm` targeted only the *old* `state.json`
> path while the lifespan called `load_state()` **before** the `OSTIARI_NO_DEMO`
> check, so quotas, experiments, and providers from an earlier demo run came back.
> And `seed_models()` ran at import time, ungated — defensible on the grounds that a
> model catalog is a routing table rather than demo content, but it meant the Models
> page was populated on an install that had promised to be empty.
>
> On a clean install, register the models you use via `POST /api/models`, or call
> `seed_models()` to get the built-in catalog back.

**Verify it's empty:**

```bash
curl http://localhost:8400/api/agents          # → []
curl http://localhost:8400/api/gateways        # → [ my-gateway (auto-registered) ]
curl http://localhost:8421/tools               # → { "tools": [], "mcp_tools": [] }
curl http://localhost:8400/api/quotas          # → []
curl http://localhost:8400/api/models          # → []
```

Open **http://localhost:9000** — every page renders, but the dashboard, agents, traces, and metering views are empty until you send real traffic.

## 1.3 Configure each feature

All gateway configuration can be applied two ways:

- **Directly on the gateway** — `POST http://localhost:8421/config/<feature>` (takes effect immediately, hot-reloaded).
- **Via the control plane** — configure in the UI or `POST http://localhost:8400/api/...`, then the control plane **pushes** it to the gateway. This is the fleet-wide path and survives gateway restarts (the gateway pulls its full config bundle on registration).

The examples below use the direct gateway path for immediacy; the control-plane equivalents are noted.

```
     Direct path (single gateway)          Fleet path (control plane)
  ┌──────────┐  POST /config/tools     ┌──────────────┐  push   ┌──────────┐
  │  you /   │ ───────────────────────▶│   gateway    │◀────────│ control  │
  │  script  │                         │    :8421     │ config  │  plane   │
  └──────────┘                         └──────────────┘ bundle  └──────────┘
                                                          ▲ on register/heartbeat
```

### 1.3.1 Tools

A **tool** is an HTTP endpoint the gateway will validate and proxy to. Agents call `POST /tool/{name}`; the gateway checks policy, then forwards to the tool's real endpoint.

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

**Fleet path:** `POST http://localhost:8400/api/tools/{gateway_id}` then `POST /api/gateways/{gateway_id}/push`. Or import a whole OpenAPI spec: `POST /api/tools/{gateway_id}/import-openapi` (see [`docs/openapi-import.md`](docs/openapi-import.md)).

### 1.3.2 Policy

Policy decides **allow / intervene / block** for each call. It combines allow/block glob patterns, per-action risk adjustments, and score thresholds.

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

```
   incoming call
        │
        ▼
  ┌───────────────┐   matches block glob?  ──yes──▶  403 BLOCK
  │  policy eval  │
  └──────┬────────┘   compute risk score (0–100)
         │                 ≤ allow_max      ──▶  ALLOW  (proxy to tool)
         ▼                 ≤ intervene_max  ──▶  INTERVENE (approval)
   score thresholds        >  intervene_max ──▶  BLOCK
```

> **Patterns are `fnmatch` globs, so use `*delete*`, not `*.delete`.** A dotted
> pattern requires a literal dot: `*.delete` matches `github.delete` but **not**
> `db_delete`. The bare-underscore names are usually the ones you most want
> blocked, so a `*.delete` block list reads as protective while letting
> `db_delete` straight through. Test every pattern against your real action names
> — the Sandbox exists for this.

**Policy YAML** (for the core library / gateway startup config) is documented in [`README.md`](README.md#policy-yaml); note the top level accepts only `allow`, `block`, `rules`, `thresholds`, and a rule is keyed by `type`, not `decision`. **Fleet path:** manage in the **Policies** page or `POST /api/policies/{id}/push`.

`POST /api/policies/{id}/push` targets the gateway's *partial* `/config/policy`
endpoint, which changes policy and nothing else. The **Policies page's Push
button does not use it** — it calls `POST /api/gateways/{id}/push-config`, which
forwards to the gateway's `POST /config`, a whole-document replace that clears
the tool registry and resets enforcement mode. Prefer the Gateways page's Push
(`POST /api/gateways/{id}/push`), which rebuilds the whole bundle from stored
state. See
[`docs/Ostiari-Configure-Orchestrate-Lifecycle.md`](docs/Ostiari-Configure-Orchestrate-Lifecycle.md) §4.

### 1.3.3 Per-agent access control (agent-auth)

Restrict which tools, models, and providers a given agent may use, and set a per-agent budget. The agent identity comes from the `X-Agent-Id` request header.

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

### 1.3.4 Quotas

Rate, budget, and token ceilings for the gateway.

```bash
curl -X POST http://localhost:8421/config/quota \
  -H "Content-Type: application/json" \
  -d '{
    "rate_limit_rpm": 60,
    "budget_limit_usd": 100.0,
    "max_tokens_per_request": 4096
  }'
```

**Fleet path:** `POST /api/quotas/{id}/push` — it forwards to the gateway's
`/config/quota` and is the one that actually enforces. Quotas are persisted to
`state.json` and restored across control-plane restarts.

> **The Quotas page's Push button is not that route.** It calls
> `POST /api/gateways/{id}/push-config` with `{"quota": …}`, and the gateway's
> `POST /config` applies only tools and policy — the quota is stored and echoed
> back by `GET /config` but never handed to the quota enforcer. Use
> `POST /api/quotas/{id}/push` (gateway-scoped quotas only), or the Gateways
> page's Push, which sends the whole bundle from stored state.

### 1.3.5 LLM gateway (model routing)

Enable the LLM module so agents can call models through the gateway (with budgets, routing, and A/B experiments applied). Because the `/invoke` endpoint is registered when the module activates, the module and its credentials are set at **gateway startup** via a config YAML — not the runtime API. Supply credentials via environment variables; never hard-code keys.

Create `my-gateway-config.yaml`:

```yaml
sidecar_id: my-gateway
control_plane_url: http://localhost:8400

modules:
  core: true
  llm_gateway: true

llm:
  default_model: claude-sonnet-4-6
  fallback_chain:
    - gpt-4o
    - claude-haiku-4-5-20251001
  max_tokens: 1024
  credentials:
    anthropic: ${ANTHROPIC_API_KEY}   # resolved from the environment at startup
    openai: ${OPENAI_API_KEY}
    # For Bedrock: AWS_REGION + the standard AWS credential chain (no key here)
```

Start the gateway with it:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # do not commit or echo keys
export OPENAI_API_KEY=sk-...
python -m ostiari_gateway.main --port 8421 --sidecar-id my-gateway \
  --control-plane http://localhost:8400 --config my-gateway-config.yaml
```

At **runtime**, per-model routing rules can be adjusted with `POST /config/llm` (`{"routing_rules": [...]}`). Model routing, per-agent model access, and A/B experiments are managed in the **Models**, **Agents**, and **Experiments** pages. See [`docs/agent-llm-routing.md`](docs/agent-llm-routing.md).

### 1.3.6 MCP servers

Attach a Model Context Protocol server; the gateway discovers its tools and exposes them as `POST /tool/{prefix}.{tool}`.

```bash
# Remote MCP server (mode is "remote" | "stdio" | "embedded")
curl -X POST http://localhost:8421/config/mcp-servers \
  -H "Content-Type: application/json" \
  -d '{"name": "github", "mode": "remote", "url": "http://your-github-mcp:3000", "prefix": "github"}'

# Local (stdio) MCP server
curl -X POST http://localhost:8421/config/mcp-servers \
  -H "Content-Type: application/json" \
  -d '{"name": "filesystem", "mode": "stdio", "prefix": "fs",
       "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp/sandbox"]}'
```

### 1.3.7 Agent-to-agent (A2A) governance

Register a peer A2A agent so this gateway can discover its skills and route tasks to it under policy. The gateway fetches the agent card at `url` and exposes its skills as an `a2a.<name>` tool.

```bash
curl -X POST http://localhost:8421/config/a2a-agents \
  -H "Content-Type: application/json" \
  -d '{"url": "http://peer-agent:9200", "name": "devops_assistant"}'
```

See [`docs/gateway-architecture.md`](docs/gateway-architecture.md) and the **Protocol Governance** page.

### 1.3.8 Payments (x402 micropayments)

Optional. Set per-agent wallets and paywalled-tool pricing so tool calls settle micropayments.

```bash
curl -X POST http://localhost:8421/config/payments \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "simulated",
    "default": 0.0,
    "overrides": {"premium_search": 0.005},
    "wallets": [
      {"agent_id": "my-agent", "balance_usdc": 5.00, "daily_limit_usdc": 1.00}
    ]
  }'
```

`mode` is `off` | `simulated` | `passthrough`; `overrides` maps tool name → USDC price; `wallets` is a list of per-agent wallets.

### 1.3.9 Enforcement mode (enforce vs shadow)

`shadow` mode evaluates everything and records what *would* have happened, but never blocks and never executes real side effects — ideal for a safe rollout.

```bash
curl -X POST http://localhost:8421/config/mode \
  -H "Content-Type: application/json" -d '{"mode": "shadow"}'
```

Review outcomes on the **Shadow Report** page, then switch to `enforce`.

> **Mode survives a gateway restart.** `PUT /api/gateways/{id}/mode` persists the
> mode in the control plane and pushes it live, the config bundle carries `mode`,
> and the gateway applies it on reconnect — before tools or policy, so a gateway
> you left in shadow doesn't spend even one request enforcing. A mode value the
> gateway doesn't recognize is ignored rather than defaulted, since the dangerous
> direction is toward *enforcing* a gateway you deliberately put in shadow.
>
> Note that the Gateways page shows the **stored** mode, not the gateway's live
> state. To check what a specific gateway is actually doing, ask it:
> `curl localhost:8421/config/mode`.

## 1.4 Point your agent at the gateway

```python
import requests

GATEWAY = "http://localhost:8421"

resp = requests.post(
    f"{GATEWAY}/tool/send_email",
    json={"to": "user@example.com", "body": "Hello"},
    headers={"X-Agent-Id": "my-agent"},
)

if resp.status_code == 200:
    print(resp.json()["result"])       # allowed → proxied result
elif resp.status_code == 403:
    print(resp.json()["reason"])       # blocked by policy
```

**Verify the guard works:**

```bash
# Allowed
curl -X POST http://localhost:8421/tool/send_email \
  -H "X-Agent-Id: my-agent" -H "Content-Type: application/json" \
  -d '{"to": "user@example.com", "body": "test"}'

# Blocked by the *delete* pattern → 403
curl -X POST http://localhost:8421/tool/db_delete \
  -H "X-Agent-Id: my-agent" -H "Content-Type: application/json" \
  -d '{"table": "users"}'
```

Both calls now appear in **Live Traces** in the UI.

## 1.5 Troubleshooting (Part 1)

| Symptom | Cause | Fix |
|---------|-------|-----|
| Quotas/experiments/providers appear despite `clean-start` | Fixed — the recipe used to `rm` only the pre-`data_dir()` `state.json` path, so the live one was restored | Update, then re-run. On an older checkout: `rm -f control-plane/data/state.json` |
| Models page empty on a clean install | `seed_models()` is now gated on `OSTIARI_NO_DEMO`, so a clean install starts with no catalog | Expected. Register what you use via `POST /api/models`, or call `seed_models()` for the built-in 18 |
| Gateway shows in UI but config push fails | Gateway endpoint missing its port | Fixed — gateways advertise `host:port` on register. Confirm `curl /api/gateways` shows `http://…:8421`, not `http://…` |
| `POST /tool/x` returns 404 | Tool not registered on that gateway | Register via `/config/tools` or push from the control plane |
| Tool call 403 unexpectedly | agent-auth enabled but agent not listed | Add the agent to `/config/agent-auth`, or disable agent-auth |

---

# Part 2 — Local Install (full demo data)

Everything running with **seeded demo data** and **all gateways up**: the fastest way to see every feature populated. One command.

## 2.1 Install (same as Part 1)

```bash
git clone https://github.com/hk-775/ostiari.git
cd ostiari
pip install -e .
pip install -e gateway/
cd control-plane/frontend && npm install && cd ../..
```

## 2.2 Start the full demo stack

```bash
make demo-full
```

This brings up the complete topology and seeds demo data:

```
  ┌──────────────────────────┐        ┌──────────────────────────┐
  │  control-plane backend   │        │  control-plane frontend  │
  │  :8400  (demo seeded)    │◀──────▶│  :9000  (dashboard)      │
  │  9 agents, traces, quotas│        └──────────────────────────┘
  └────────────┬─────────────┘
     register / heartbeat / push (tools, policy, MCP, A2A, payments)
    ┌───────────┼───────────┬───────────────┬───────────────┐
    ▼           ▼           ▼               ▼               │
┌────────┐  ┌────────┐  ┌─────────┐   ┌──────────┐          │
│crm-agent│ │ops-agent│ │devops-   │  │analytics-│          │
│  :8421  │ │  :8422  │ │agent:8424│  │agent:8425│          │
│ +LLM +  │ │ ops     │ │ MCP      │  │ MCP      │          │
│ MCP +   │ │ tools   │ │ tools    │  │ tools    │          │
│ payments│ └────────┘  └─────────┘   └──────────┘          │
└────┬───┘                                                   │
     │ tools point at ─────────────────┐                     │
     ▼                                  ▼                     ▼
┌──────────────┐                 ┌──────────────┐   registers A2A agent
│ demo tools   │                 │  A2A demo    │◀──────────────┘
│ server :9300 │                 │ server :9200 │
└──────────────┘                 └──────────────┘
```

## 2.3 What you get

| Component | URL | Gateway ID | Highlights |
|-----------|-----|-----------|------------|
| Control Plane UI | http://localhost:9000 | — | Every page populated |
| Control Plane API | http://localhost:8400 | — | 9 agents, seeded traces, quotas, experiments |
| CRM Gateway | http://localhost:8421 | `crm-agent` | LLM chat, 12 HTTP tools + the draw.io and filesystem MCP servers, payments, `block-destructive` policy |
| Ops Gateway | http://localhost:8422 | `ops-agent` | 4 ops tools + `ops-guard` policy |
| DevOps Gateway | http://localhost:8424 | `devops-agent` | 4 DevOps tools + `devops-guard` policy |
| Analytics Gateway | http://localhost:8425 | `analytics-agent` | 3 analytics tools, no policy (no destructive tools) |
| A2A Demo Agent | http://localhost:9200 | — | deploy / rollback / status skills |
| Demo Tools server | http://localhost:9300 | — | Canned tool backends |

The registration scripts (`register_demo_tools`, `register_fleet_tools`, `register_demo_mcp`, `register_demo_a2a`, `register_demo_payments`, `register_demo_providers`) run automatically and push tools, policy, MCP servers, an A2A agent, payment wallets, and provider credentials to the gateways.

> **Three policies get created, not two.** `register_fleet_tools.py` used to set
> `"policy": None` for `devops-agent` on the assumption that a `devops-strict`
> policy already blocked `github.delete_repo` — nothing in the repo ever created
> one, so a fresh demo shipped that destructive tool ungoverned. It now creates a
> `devops-guard` policy blocking `*delete*`, `*.drop`, `*.destroy`, `db_delete`
> and allowing the three read/write tools. `analytics-agent` is still policy-free,
> and legitimately so — it registers no destructive tools.

## 2.4 Verify the demo is wired

```bash
# /tools returns {"tools": [...HTTP...], "mcp_tools": [...MCP...]}
curl http://localhost:8421/tools | python -c 'import sys,json;d=json.load(sys.stdin);print(len(d["tools"])+len(d["mcp_tools"]),"tools (HTTP+MCP)")'
curl http://localhost:8422/tools | python -c 'import sys,json;d=json.load(sys.stdin);print(len(d["tools"]),"HTTP tools")'   # 4
curl http://localhost:8400/api/payments/wallets                       # 8 wallets
curl http://localhost:9200/.well-known/agent.json                     # A2A agent card
```

## 2.5 Take the tour (in the UI)

1. **Sandbox → Chat** — talk to the LLM through the CRM gateway; watch tool calls resolve.
2. **Sandbox → Scenarios** — run allow/block guard demos.
3. **Sandbox → A2A** — enter `http://localhost:9200`, Discover, send a task ("Deploy auth-service to staging").
4. **Live Traces** — pre-seeded, plus your Sandbox calls stream in live.
5. **Gateways** — all four registered and heartbeating (endpoints show correct ports).
6. **Approvals (HITL)** — four pending calls awaiting a decision, plus a decision
   history. The pending four *are* the intervene-tier calls in Live Traces, so both
   views describe the same events. Approve or deny one and watch it move.
7. **Quotas** — one quota per gateway with live budget bars, rate limits, model
   allowlists, and **Push to gateway**. Spend is summed from the same usage records
   the Costs page reads, so the two agree.
8. **Payments** — 8 wallets, a drained agent that blocks, settled micropayments.
9. **Metering / Costs / ROI** — populated from seeded usage records.

## 2.6 Reset

To go from full-demo back to a clean slate, stop the processes, delete the state
file the Makefile misses, then run `make clean-start` (Part 1):

```bash
rm -f control-plane/data/state.json
make clean-start
```

Without that `rm`, the demo's quotas, experiments, models, and providers are
restored on the next boot — see the caveat in §1.2.

---

# Part 3 — Enterprise Service

Run Ostiari as shared infrastructure. All enterprise targets drive the gateway through **environment variables** (no CLI flags needed) and are built from three container images.

## 3.1 The images

| Image | Dockerfile | Serves |
|-------|-----------|--------|
| `ostiari-gateway` | `deploy/docker/Dockerfile.gateway` | The gateway (port 8421) |
| `ostiari-control-plane` | `deploy/docker/Dockerfile.control-plane` | Backend API (port 8400) |
| `ostiari-frontend` | `deploy/docker/Dockerfile.frontend` | Dashboard via nginx (port 9000) |

All build contexts are the **repo root**, because the gateway and control-plane images install the local `ostiari` core package (not published to PyPI):

```bash
docker build -f deploy/docker/Dockerfile.gateway       -t ostiari-gateway:latest .
docker build -f deploy/docker/Dockerfile.control-plane -t ostiari-control-plane:latest .
docker build -f deploy/docker/Dockerfile.frontend \
  --build-arg VITE_API_URL=https://ostiari.example.com:8400 -t ostiari-frontend:latest .
```

> **Frontend gotcha:** `VITE_API_URL` is baked into the JS bundle at build time and called from the **user's browser**. Set it to a URL the browser can reach (public host / ingress), *not* an in-cluster service name.

## 3.2 Configuration surface (all targets)

**Gateway** (env vars — read by the CLI via `envvar=`):

| Variable | Purpose |
|----------|---------|
| `OSTIARI_GATEWAY_ID` | Unique gateway identifier |
| `OSTIARI_CONTROL_PLANE_URL` | Control plane URL (enables register/heartbeat) |
| `OSTIARI_PORT` | Listen port (default 8421) |
| `OSTIARI_ADVERTISE_HOST` | **Critical.** Host the control plane pushes config back to. Set to the gateway's network-reachable name (compose service / k8s Service DNS / ECS service). Without it, config pushes may not reach the gateway. |
| `OSTIARI_CONFIG_ADMIN_KEY` | **Fail-open unless set.** Gates the `/config/*` administration endpoints; unset, anything that can reach the gateway can rewrite its policy |
| `OSTIARI_GATEWAY_AUTH=required` | **Off by default**, meaning `X-Agent-Id` is trusted as-is. When required, a tool call needs a Bearer token whose identity matches the header (401 missing/invalid, 403 mismatch) |
| `OSTIARI_HITL=on` | Enables the human-approval gate for the *intervene* tier (202 + `X-Approval-Id` resubmit). Off in a fail-closed production deployment means the middle tier becomes a refusal |
| `OSTIARI_REQUIRE_AXON=1` | Refuse to start without AxonLLM instead of warning. The right setting in production — see [`docs/axon-router.md`](docs/axon-router.md) |
| `REDIS_ENDPOINT` / `OSTIARI_REDIS_URL` | Share rate-limit, budget, and wallet counters fleet-wide. Without it they are per-replica (see §3.8) |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | LLM credentials (mount from a secret) |

**Control plane** (env vars):

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | `sqlite+aiosqlite:///…` (dev) or `postgresql+asyncpg://user:pass@host:5432/ostiari` (prod RDS) |
| `OSTIARI_DATA_DIR` | Writable dir for the SQLite DB and `state.json` (default `control-plane/data`). Both resolve through `env.data_dir()` — point it at the mounted volume |
| `OSTIARI_NO_DEMO` | `1` to skip the demo seeders (recommended for production) |
| `OSTIARI_ENV=production` | The single production signal. Turns three defaults fail-closed: admin password, JWT secret, and trace-ingest key |
| `OSTIARI_REQUIRE_AUTH` | **Off by default.** The coarse gate: when truthy, every `/api/*` route needs a Bearer token. Without it the whole API is unauthenticated even with OIDC configured |
| `OSTIARI_ADMIN_EMAIL` / `OSTIARI_ADMIN_PASSWORD` | Initial admin. The app **refuses to seed** `admin/admin` when `OSTIARI_ENV=production` |
| `OSTIARI_JWT_SECRET` | Signing secret for issued tokens. In production the dev default is refused and the value must be **≥ 32 characters** |
| `OSTIARI_INGEST_KEY` | Shared secret for `POST /api/traces/ingest` (`X-Ingest-Key`). **Required in production** — unset, ingest 401s |
| `OSTIARI_CORS_ORIGINS` | Comma-separated allowed origins (enables credentialed CORS) |
| `OSTIARI_AUTH_MODE=oidc` + `OSTIARI_OIDC_*` | Validate IdP JWTs instead of local tokens — see [`auth/README.md`](auth/README.md) |

> **`OSTIARI_ENV=production` and `OSTIARI_REQUIRE_AUTH` are separate knobs, and
> you need both.** Production mode hardens the admin password, JWT secret, and
> ingest key, but it does *not* make authentication mandatory — that's
> `OSTIARI_REQUIRE_AUTH` alone. Set production without it and every route stays
> reachable with no token.
>
> Two of these make the app **refuse to start** rather than warn:
> `OSTIARI_ADMIN_PASSWORD` and a ≥32-char `OSTIARI_JWT_SECRET`. That's deliberate
> — a governance control plane that silently boots on `admin/admin` is worse than
> one that fails loudly.
>
> `OSTIARI_INGEST_KEY` has two known gaps: the gateway never sends
> `X-Ingest-Key`, and only `/api/traces/ingest` checks it. See
> [`deploy/README.md`](deploy/README.md#ostiari_ingest_key--two-gaps) before
> setting it.

### Why `OSTIARI_ADVERTISE_HOST` matters

The control plane pushes config to `{gateway.endpoint}/config`. The gateway advertises this endpoint when it registers. If it advertised only the source host, the **port would be lost** and pushes would fail. The advertise host must be the name other services use to reach the gateway:

```
  ┌───────────────┐   register {callback_url: http://<ADVERTISE_HOST>:8421}
  │    gateway    │ ─────────────────────────────────────────────▶ ┌──────────────┐
  │    :8421      │                                                 │control plane │
  │               │ ◀───────────────────────────────────────────── │              │
  └───────────────┘   push config to http://<ADVERTISE_HOST>:8421/config└──────────┘
```

- **docker-compose** → the service name (`gateway`)
- **Kubernetes** → the gateway's Service DNS name
- **ECS** → the service's Cloud Map / service-discovery name

## 3.3 Option A — Docker Compose

Full stack (backend + frontend + one gateway + Redis) on one host.

```bash
cd deploy/docker
docker compose up --build
```

```
  docker network: docker_default
  ┌──────────────────┐   :8400   ┌──────────────────┐
  │ control-plane-   │◀─────────▶│ control-plane-   │ :9000
  │ backend (SQLite  │           │ frontend (nginx) │
  │ on named volume) │           └──────────────────┘
  └───────┬──────────┘
   push │  ▲ register/heartbeat  (OSTIARI_ADVERTISE_HOST=gateway)
        ▼  │
  ┌──────────────────┐        ┌──────────┐
  │     gateway      │        │  redis   │
  │     :8421        │        │  :6379   │
  └──────────────────┘        └──────────┘
```

- Backend health: `GET /api/health`. Gateway health: `GET /health`.
- Data persists in the `control-plane-data` named volume (SQLite). For production, set `DATABASE_URL` to Postgres.

**Verify:**

```bash
curl http://localhost:8400/api/health
curl http://localhost:8400/api/gateways   # gateway-1 → http://gateway:8421 healthy
curl http://localhost:9000/               # dashboard (200)
# Prove CP→gateway push works:
curl -X POST http://localhost:8400/api/tools/gateway-1 \
  -H 'Content-Type: application/json' \
  -d '{"name":"echo","endpoint":"http://example.com/echo","method":"POST"}'
curl -X POST http://localhost:8400/api/gateways/gateway-1/push
curl http://localhost:8421/tools          # echo tool present
```

## 3.4 Option B — Kubernetes

Two patterns:

**Sidecar** — one gateway per agent pod, sharing the pod network:

```bash
kubectl apply -f deploy/kubernetes/gateway-sidecar.yaml
kubectl apply -f deploy/kubernetes/control-plane.yaml
```

```
  ┌───────────────── Pod: agent-with-ostiari ─────────────────┐
  │  ┌────────────┐         localhost:8421      ┌───────────┐  │
  │  │   agent    │ ──────────────────────────▶ │  gateway  │  │
  │  │  :8080     │                             │  sidecar  │  │
  │  └────────────┘                             │  :8421    │  │
  │                                             └─────┬─────┘  │
  └───────────────────────────────────────────────────┼───────┘
      Service agent-with-ostiari:8421 ◀── control plane pushes here
```

**Shared** — a horizontally-scaled gateway fleet behind one Service:

```bash
kubectl apply -f deploy/kubernetes/gateway-shared.yaml
kubectl apply -f deploy/kubernetes/control-plane.yaml
```

Config push targets the Service DNS (reaches one replica); every replica also **pulls** its config on each heartbeat, so the fleet self-syncs. An HPA scales 3→20 pods on CPU/memory.

> **Note:** config self-syncs, and *enforcement counters* (rate limit, quota/budget, wallets) are per-replica in-process **unless** you point the gateway at Redis (`REDIS_ENDPOINT`/`OSTIARI_REDIS_URL`) — then they're shared fleet-wide via atomic Redis ops. Without Redis, a limit configured on the fleet is enforced per pod (N pods ⇒ N× effective). See the [Production checklist](#38-production-checklist).

Secrets:

```bash
kubectl create secret generic ostiari-secrets \
  --from-literal=anthropic-api-key=... \
  --from-literal=openai-api-key=...
```

The control-plane Deployment uses a `DATABASE_URL` env var and `/api/health` probes; the frontend runs as the separate `ostiari-frontend` image. For production, replace the SQLite PVC with RDS (set `DATABASE_URL` to your Postgres DSN).

## 3.5 Option C — Helm

```bash
helm install ostiari deploy/helm/ostiari-gateway \
  --set gateway.controlPlaneUrl=http://your-control-plane:8400 \
  --set gateway.advertiseHost=ostiari-gateway \
  --set redis.endpoint=your-redis-host
```

Key values (`deploy/helm/ostiari-gateway/values.yaml`): `image.repository/tag`, `gateway.{id,port,controlPlaneUrl,advertiseHost}`, `redis.{enabled,endpoint,port}`, `autoscaling.*`, `secrets.existingSecret`. The chart renders valid manifests whether or not Redis is enabled.

```bash
helm lint deploy/helm/ostiari-gateway
helm template ostiari deploy/helm/ostiari-gateway   # dry-run render
```

## 3.6 Option D — ECS Fargate

```bash
# 1. Push images to ECR; fill in ACCOUNT_ID / REGION placeholders.
# 2. Store secrets in AWS Secrets Manager (ostiari/anthropic-api-key, …).
aws ecs register-task-definition --cli-input-json file://deploy/ecs/task-definition.json
aws ecs create-service          --cli-input-json file://deploy/ecs/service.json
```

The task definition wires `OSTIARI_GATEWAY_ID` / `OSTIARI_CONTROL_PLANE_URL` / `OSTIARI_PORT` / `OSTIARI_ADVERTISE_HOST` as env vars and pulls API keys + Redis endpoint from Secrets Manager. Health check: `curl -f http://localhost:8421/health`.

## 3.7 Option E — AWS Lambda (limited)

Suitable for **stateless, pull-based validation only**. The heartbeat/config-push background loop does **not** run under Lambda, so a Lambda gateway won't stay registered or receive pushed config. Prefer ECS/Kubernetes for a fully-governed gateway.

```bash
cd deploy/lambda
pip install mangum -t .
pip install ../.. -t .          # ostiari core
pip install ../../gateway -t .  # ostiari-gateway
sam build && sam deploy --guided
```

## 3.8 Production checklist

- [ ] `DATABASE_URL` → PostgreSQL (RDS), not SQLite.
- [ ] `OSTIARI_NO_DEMO=1` on the control plane.
- [ ] `OSTIARI_ENV=production` + `OSTIARI_ADMIN_PASSWORD` set (the app refuses to seed `admin/admin` in production).
- [ ] `OSTIARI_REQUIRE_AUTH=1` and an `OSTIARI_JWT_SECRET` of **≥ 32 characters**
      (production refuses the dev default and anything shorter).
- [ ] `OSTIARI_INGEST_KEY` set — required in production, or every trace ingest
      401s. Read the two gaps in [`deploy/README.md`](deploy/README.md) first.
- [ ] `OSTIARI_CONFIG_ADMIN_KEY` set on every gateway. Unset, the `/config/*`
      endpoints are open and anything on the network can rewrite the policy the
      gateway enforces.
- [ ] `OSTIARI_GATEWAY_AUTH=required` if agents must prove identity — otherwise
      `X-Agent-Id` is a self-asserted header and per-agent limits are advisory.
- [ ] `OSTIARI_HITL=on` wherever `OSTIARI_ENV=production` is set. Production is
      fail-closed, so HITL off turns every scored *intervene* into a 403 with an
      empty Approvals queue. Validate thresholds in `shadow` first.
- [ ] `OSTIARI_REQUIRE_AXON=1` on gateways that route LLM traffic — without
      AxonLLM, routing governance and token cost tracking silently stop applying.
- [ ] After every gateway restart, Push from the Gateways page (or
      `POST /api/gateways/push-all`) — enforcement mode is not restored from the
      config bundle, so a shadow gateway comes back up enforcing.
- [ ] `OSTIARI_CORS_ORIGINS` set to your dashboard origin(s).
- [ ] `OSTIARI_ADVERTISE_HOST` set on every gateway to a name the control plane can reach.
- [ ] API keys sourced from Secrets Manager / k8s Secrets, never baked into images.
- [ ] TLS terminated at the ALB / ingress (not the gateway).
- [ ] For a multi-replica fleet, set `REDIS_ENDPOINT` (or `OSTIARI_REDIS_URL`)
      so rate-limit / budget / wallet limits hold **fleet-wide** — otherwise
      enforcement state is in-process and limits apply per replica (N instances
      ⇒ N× the effective `budget_limit_usd`/`rate_limit_rpm`). Install the extra:
      `pip install "ostiari-gateway[redis]"`. See [`deploy/README.md`](deploy/README.md)
      Production Notes.
- [ ] (Optional) `OTEL_EXPORTER_OTLP_ENDPOINT` for trace export — see [`docs/otlp-export.md`](docs/otlp-export.md).

## 3.9 Troubleshooting (Part 3)

| Symptom | Cause | Fix |
|---------|-------|-----|
| Gateway image build fails on `COPY ostiari_sidecar/` | Old stale Dockerfile | Use `deploy/docker/Dockerfile.gateway` (built from repo root) |
| Backend container crashloops: `No module named 'ostiari'` | Core package not installed in image | `Dockerfile.control-plane` installs core first; rebuild |
| Config push returns 502 / never reaches gateway | `OSTIARI_ADVERTISE_HOST` unset or wrong | Set it to the gateway's network-reachable name |
| Dashboard loads but all API calls fail (CORS / connection) | `VITE_API_URL` points at an in-cluster name the browser can't resolve | Rebuild the frontend image with a browser-reachable `VITE_API_URL` |
| Backend health check fails | Probing `/health` instead of `/api/health` | Control-plane health path is `/api/health` |
| Helm render invalid YAML with `redis.enabled=false` | Old chart emitted secrets under a missing `env:` | Fixed — `env:` is always present |

---

## Appendix — Feature → configuration map

| Feature | Gateway endpoint | Control-plane page | UI-managed |
|---------|-----------------|--------------------|:----------:|
| Tools | `/config/tools` | Tools | ✓ |
| Policy | `/config/policy` | Policies | ✓ |
| Per-agent access | `/config/agent-auth` | Agents / Agent Quotas | ✓ |
| Cross-agent rules | `/config/cross-agent` | — | |
| Quotas | `/config/quota` | Quotas | ✓ |
| Budget reset | `/config/budget-reset` | — | |
| LLM routing | `/config/llm`, `/config/routing-overrides`, `/config/agent-routing` | Models / Providers | ✓ |
| A/B experiments | — | Experiments | ✓ |
| MCP servers | `/config/mcp-servers` | MCP Servers | ✓ |
| A2A agents | `/config/a2a-agents` | Protocol Governance | ✓ |
| Payments (x402) | `/config/payments` | Payments | ✓ |
| Enforcement mode | `/config/mode` | Shadow Report | ✓ |
| Approvals | — | Approvals | ✓ |
| Traces | — | Live Traces | ✓ |
| Metering / Costs / ROI | — | Metering / Costs / ROI | ✓ |
| Compliance / Audit | — | Compliance / Audit Log | ✓ |
| Discovery | — | Discovery | ✓ |
| Token broker | — | Token Broker | ✓ |

Every row above is a **partial** endpoint that changes one concern. The gateway
also has a bare `POST /config`, which is a whole-document replace applying only
tools + policy — that is what both UI Push buttons go through, and why they clear
the tool registry and reset enforcement mode. Detail:
[`docs/Ostiari-Configure-Orchestrate-Lifecycle.md`](docs/Ostiari-Configure-Orchestrate-Lifecycle.md) §4
and [`docs/gateway-architecture.md`](docs/gateway-architecture.md#the-config-partial-push-trap).

`/config/agent-routing` is registered by the **llm_gateway module**, not the core
server, so it 404s on a gateway started without `modules.llm_gateway: true`.

For deeper reference: [`docs/control-plane-guide.md`](docs/control-plane-guide.md), [`docs/gateway-architecture.md`](docs/gateway-architecture.md), [`deploy/README.md`](deploy/README.md), [`auth/README.md`](auth/README.md).
