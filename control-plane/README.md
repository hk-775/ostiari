# Ostiari Control Plane

Centralized web console for managing a fleet of Ostiari **agent gateways** — the
proxies that enforce safety policies between AI agents and their tools.

---

## Architecture

```mermaid
graph TB
    subgraph "Control Plane (this project)"
        UI[React + Tailwind UI<br/>port 9000]
        API[FastAPI Backend<br/>port 8400]
        DB[(SQLite / Postgres)]
    end

    subgraph "Gateway Fleet"
        G1[Gateway A<br/>port 8421]
        G2[Gateway B<br/>port 8422]
        G3[Gateway C<br/>port 8424]
    end

    subgraph "AI Agents"
        A1[Agent A]
        A2[Agent B]
        A3[Agent C]
    end

    subgraph "Tools & Services"
        T1[Email Service]
        T2[Database]
        T3[GitHub MCP]
    end

    UI --> API
    API --> DB
    API -->|push config| G1
    API -->|push config| G2
    API -->|push config| G3
    G1 -->|report traces| API
    G2 -->|report usage| API
    A1 --> G1
    A2 --> G2
    A3 --> G3
    G1 --> T1
    G2 --> T2
    G3 --> T3
```

---

## Features

| Feature | Description |
|---------|-------------|
| **Agent Gateway Registry** | Register gateways (formerly "sidecars"), monitor health via a background check loop, push configuration to one or all. Per-gateway enforcement mode: `enforce` or `shadow` |
| **Agent Registry** | View all agents across your fleet — 9 seeded agents across 8 frameworks (OpenAI, Anthropic, Strands, Bedrock, AgentCore, CrewAI, LangGraph, plus `gateway-invoke` for the two bots that call the gateway directly). Shows framework badges, tools, model, and gateway assignment |
| **Tool Management** | CRUD tools per gateway with search/filter, plus OpenAPI spec import. The demo registers 23 tools across the four gateways |
| **Policy Management** | Create/edit/push safety policies (allow/block/risk-adjust). The demo creates `block-destructive` (crm-agent) and `ops-guard` (ops-agent) |
| **MCP Server Management** | Configure MCP servers (embedded, remote HTTP, or stdio subprocess) with tool auto-discovery. The demo seeds two *real* stdio servers (draw.io + filesystem, via `npx`) |
| **Model Configuration** | Central registry of LLM models — 18 pre-seeded from AxonLLM. CRUD with inline routing strategy editing. Shows providers, pricing, capabilities (tools/vision), and category (reasoning/general/speed) |
| **Quotas (Runtime Enforced)** | Per-gateway and per-agent rate limits, budget caps, model/provider allowlists, and `max_tokens` caps. Agent quotas persist in the control plane, push as a complete gateway bundle, restore measured spend after restart, and use actual usage records for dashboard spend/RPM |
| **Approvals (HITL)** | Human-in-the-loop queue for calls that score *intervene*. The gateway answers 202 with an approval id; a human decides here; the caller re-submits with `X-Approval-Id` |
| **Sandbox** | Four-tab testing environment: Chat (invoke an LLM via the gateway), Scenarios (one-click allow/block demos), Code (write and run agent code), A2A (discover + send tasks). Uses the gateway proxy to eliminate CORS |
| **Cost Dashboard** | Track LLM spend broken down by model, gateway, agent, and day |
| **Metering** | Per-agent token/cost rollups with CSV/JSON export |
| **Payments (x402)** | Per-agent USDC wallets with balance, per-call and daily limits, a payment ledger, and per-tool pricing. External money movement is simulated behind a clean seam |
| **Token Broker** | Retry-safe pool drawdown and customer charging, depleted-provider routing controls, margin reporting, and invoice reconciliation |
| **A/B Experiments** | Percentage-based traffic splitting between models with side-by-side results comparison. 3 seeded in the demo |
| **Live Trace Viewer** | Real-time WebSocket feed of tool calls across all gateways, with session/plan/step grouping, tool parameters, and a model badge per trace |
| **Shadow Report** | What a gateway in `shadow` mode *would* have blocked — try before you enforce |
| **Protocol Governance (A2A)** | Register peer agents per gateway, govern agent-to-agent delegation, and view a delegation report with trust scoring |
| **Discovery** | Scan for unregistered agents and onboard them |
| **Compliance** | EU AI Act–oriented framework reports |
| **ROI / Savings** | "Damage prevented" dashboard driven by a configurable cost model |
| **Audit Log** | Tamper-evident record of who changed what config, when (filterable by resource, action, actor; `/api/audit/verify` checks the chain) |
| **Users & SSO** | Local accounts with roles (admin / operator / viewer) plus an OIDC SSO flow, including IdP claim→role mapping |
| **Gateway Proxy** | `/api/proxy/gateway/{gateway_id}/{path}` forwards UI requests to gateways, eliminating CORS and enabling Sandbox in production |
| **Data Persistence** | SQLite + JSON state, both under `control-plane/data/` (override with `OSTIARI_DATA_DIR`). `state.json` is written on graceful shutdown and restored on startup, and covers exactly six stores: quotas, experiments, models, providers, the ROI cost model, and the token-broker config. **Agents, approvals, and traces are in neither the DB nor `state.json`** — the demo re-seeds them each start, so a non-demo start comes up without them. Wallets, tools, policies, MCP servers, usage records, and audit logs are SQLite tables and persist normally |

### UI Design

The control plane uses a warm light theme (stone-50 background) with white cards,
Inter font, and violet primary buttons. Each page features colored glowing borders
that match its sidebar icon color.

### Sidebar Navigation Structure

The sidebar is organized into labeled sections with colored left border accents
(see `frontend/src/components/Layout.tsx`):

| Section | Color | Pages |
|---------|-------|-------|
| **Observe** | Emerald | Dashboard, Live Traces, Shadow Report, Approvals, Costs, Metering, Audit Log, Compliance, ROI / Savings |
| **Control** | Rose | Models (per agent), Policies (per tool), Quotas (per gateway), Quotas (per agent) |
| **Monetize** | Emerald | Payments (x402), Token Broker |
| **Configure** | Sky | Discovery, Agent Gateways, Agents, Tools, MCP Servers, Protocol (A2A) |
| **Test** | Fuchsia | Sandbox, A/B Tests, Architecture |
| **Admin** | Violet | LLM Providers, Users |

**Admin** is admin-only; a `viewer` additionally loses the write sections.
`/efficiency` and `/` (the landing page) are routed in `App.tsx` but not linked
from the sidebar — reach them by URL.

> **Role enforcement is mostly a frontend affordance.** Hiding a section stops the
> nav, not the API. Only three surfaces check the role server-side: providers
> (`require_role("admin")`), user management (admin), and the audit log
> (admin-or-operator). Everything else — policies, quotas, tools, gateways, mode —
> accepts a write from any authenticated principal, and from *any* caller at all
> unless `OSTIARI_REQUIRE_AUTH` is set. Treat the roles as UI organization until
> those checks exist.

---

## Quick Start

### Backend

```bash
cd backend
pip install -e .
python main.py
# API available at http://localhost:8400
```

### Frontend

```bash
cd frontend
npm install
npm run dev
# UI available at http://localhost:9000
```

### Verify

```bash
curl http://localhost:8400/api/health
# {"status": "ok", "service": "control-plane"}
```

**By default no route requires a token.** The `AuthMiddleware` gate is a no-op
unless `OSTIARI_REQUIRE_AUTH` is truthy — which is what keeps the demo working
with zero config, and also means a dev instance on a shared network is wide open.
Set it in anything but a laptop demo.

With it on, every `/api/*` route needs a Bearer token except `/api/health`,
`/api/auth/login`, `/api/auth/register`, `/api/auth/sso/*`, `/api/traces/ingest`
(guarded by its own `X-Ingest-Key`), and the OpenAPI docs. Sign in through the UI
or `POST /api/auth/login`. See [`../auth/README.md`](../auth/README.md) for the
OIDC path.

---

## API Reference

Authoritative source: `http://localhost:8400/docs` (generated from the app).

### Gateways

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/gateways` | GET | List all registered gateways |
| `/api/gateways` | POST | Register a new gateway |
| `/api/gateways/{gateway_id}` | GET | Get gateway details |
| `/api/gateways/{gateway_id}` | PATCH | Update a gateway |
| `/api/gateways/{gateway_id}` | DELETE | Remove a gateway |
| `/api/gateways/{gateway_id}/push` | POST | Push the full config bundle, rebuilt from stored state. **Prefer this** — it's the Gateways page's ↑ button and can't clear anything |
| `/api/gateways/{gateway_id}/push-config` | POST | Forward an arbitrary caller-supplied body to the gateway's `POST /config`. **Not persisted**; tools/policy/base fields use replacement semantics while explicitly present runtime gates apply live. Prefer dedicated routes for individual controls |
| `/api/gateways/{gateway_id}/config-bundle` | GET | Fetch the config a gateway would receive |
| `/api/gateways/{gateway_id}/register` | POST | Self-registration (called by the gateway on boot) |
| `/api/gateways/{gateway_id}/heartbeat` | POST | Liveness ping (called by the gateway) |
| `/api/gateways/{gateway_id}/health` | GET | Check gateway health |
| `/api/gateways/{gateway_id}/mode` | PUT | Set enforcement mode (`enforce` \| `shadow`), persist it, and push it live. Registration and reconnect bundles include the stored mode, so it is restored after a gateway restart |
| `/api/gateways/push-all` | POST | Push config to all gateways |

### Tools

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/tools` | GET | List all tools across gateways |
| `/api/tools/{gateway_id}` | POST | Add a tool to a gateway |
| `/api/tools/{gateway_id}/import-openapi` | POST | Import tools from an OpenAPI spec |
| `/api/tools/{tool_id}` | DELETE | Remove a tool |

### Policies

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/policies` | GET | List all policies |
| `/api/policies` | POST | Create a new policy |
| `/api/policies/{policy_id}` | GET | Get a policy |
| `/api/policies/{policy_id}` | PATCH | Update a policy |
| `/api/policies/{policy_id}` | DELETE | Remove a policy |
| `/api/policies/{policy_id}/push` | POST | Push the effective policy set to its assigned gateway, or every org gateway for a global policy |

### MCP Servers

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/mcp-servers` | GET | List MCP servers (optional `?gateway_id=` filter) |
| `/api/mcp-servers/{gateway_id}` | POST | Add an MCP server to a gateway |
| `/api/mcp-servers/{mcp_id}` | GET | Get MCP server details |
| `/api/mcp-servers/{mcp_id}` | DELETE | Remove an MCP server |
| `/api/mcp-servers/{mcp_id}/discover` | POST | Discover the server's tools |
| `/api/mcp-servers/{mcp_id}/tools` | GET | List discovered tools |

### Models

Models are keyed by **name**, not a numeric id.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/models` | GET | List all registered models (18 pre-seeded from AxonLLM) |
| `/api/models` | POST | Register a new model (custom or fine-tuned) |
| `/api/models/{name}` | GET | Get model details (pricing, capabilities, routing strategy) |
| `/api/models/{name}` | PUT | Update a model's configuration |
| `/api/models/{name}` | DELETE | Remove a model from the registry |
| `/api/models/push` | POST | Validate and push the tenant model/provider catalog to each reachable gateway with the LLM module active |

### Quotas

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/quotas` | GET | List quotas; optional `scope=gateway|agent|...` filter. Spend and trailing-minute RPM are aggregated from usage records |
| `/api/quotas` | POST | Create a gateway or agent quota (rate limit, budget, model/provider allowlists, `max_tokens`, alert threshold) |
| `/api/quotas/{quota_id}` | PUT | Update a quota's limits (partial — omitted fields are untouched, an explicit `null` clears one) |
| `/api/quotas/{quota_id}` | DELETE | Remove a quota |
| `/api/quotas/{quota_id}/push` | POST | **Push quota to the gateway** — agent scope rebuilds the complete agent quota map for that gateway |
| `/api/quotas/agents/push?gateway_id=...` | POST | Push all persisted agent quotas for a gateway (also removes deleted limits) |
| `/api/quotas/alerts` | POST | Record a budget threshold crossing (80/90/100%) — called by the gateway |
| `/api/quotas/alerts` | GET | Budget alerts from this org's gateways, newest first |
| `/api/quotas/alerts` | DELETE | Acknowledge (clear) this org's alerts; returns the count cleared |

> Budget alerts are held in memory, capped at 200 per org — an alert is a
> notification, not a ledger, and the spend behind it is already in
> `usage_records`. They are saved to `state.json` on shutdown and restored on
> startup, like quotas themselves: the cap bounds the store, but a control-plane
> bounce should not erase the record that a gateway crossed 100% of its budget.
> Ingest is a service-key machine path when production auth is enabled, and the
> org comes from the reporting gateway's row rather than the payload. The
> **Quotas** page shows them, newest first, with an Acknowledge-all button.
>
> **Editing a quota does not enforce the change** — push it afterwards, same as a
> newly created one.

> **Important:** Creating or editing through the API saves the quota; call
> `/api/quotas/{id}/push` to activate it. The Agent Quotas page combines these as
> **Save & Push**. Gateway quotas are sent to `/config/quota`. Agent quotas are
> layered over the gateway's existing tool grants and sent together to
> `/config/agent-auth`, so changing one agent cannot erase another or widen its
> tool access. The same bundle is stored on the gateway record and is returned
> during registration/reconnect. Tool authorization and quota enforcement have
> separate enable switches, so adding a quota does not turn on tool restrictions.

### Approvals (HITL)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/approvals` | GET | List pending approvals |
| `/api/approvals` | POST | Create an approval request (called by the gateway) |
| `/api/approvals/all` | GET | List pending *and* decided approvals |
| `/api/approvals/{approval_id}` | GET | Get approval details (action, params, risk signals) |
| `/api/approvals/{approval_id}/decision` | POST | Approve or deny |

### Cost Tracking

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/costs/summary` | GET | Aggregated costs by model/gateway/agent/day (`?period_days=7&gateway_id=`) |
| `/api/costs/records` | GET | List individual usage records (`?gateway_id=&model=&limit=100`) |
| `/api/costs/record` | POST | Record a single usage event (called by gateways) |
| `/api/costs/record/batch` | POST | Record a batch of usage events |

### Metering

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/metering/summary` | GET | Per-agent token and cost rollups |
| `/api/metering/export` | GET | Export usage records (CSV / JSON) |

### Payments (x402)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/payments/wallets` | GET | List agent wallets (balance, limits) |
| `/api/payments/wallets` | POST | Create a wallet |
| `/api/payments/wallets/{agent_id}` | PATCH | Update limits |
| `/api/payments/wallets/{agent_id}/fund` | POST | Add USDC to a wallet |
| `/api/payments/pricing` | GET/POST | Per-tool price list |
| `/api/payments/ledger` | GET | Payment history |
| `/api/payments/summary` | GET | Spend rollup |
| `/api/payments/ingest` | POST | Record a payment (called by the gateway) |
| `/api/payments/push` | POST | Push wallets + pricing to gateways |

### Token Broker

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/token-broker/config` | GET/POST | Bulk discount and markup settings |
| `/api/token-broker/config/reset` | POST | Restore defaults |
| `/api/token-broker/report` | GET | Margin report (retail vs. wholesale) |
| `/api/token-broker/pilot/pools` | GET | Pool inventory and gateway-enforced low-water status |
| `/api/token-broker/pilot/pools/fund` | POST | Buy tokens into a pool |
| `/api/token-broker/pilot/collector` | GET | Collector state |
| `/api/token-broker/pilot/reconcile` | POST | Reconcile a provider invoice |
| `/api/token-broker/pilot/reconciliations` | GET | Reconciliation history |

Gateway cost batches carry a stable `event_id` and the provider that actually
served each request. The control plane uses `(gateway_id, event_id)` to apply the
usage row, pool debit, and customer charge once. A billing failure returns `503`
after persisting the usage and debit; the gateway retains and retries that exact
batch with the same event IDs. Pool state is returned with ingestion responses
and heartbeats so gateways reroute away from, or block, depleted providers.

### A/B Experiments

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/experiments` | GET | List all experiments |
| `/api/experiments` | POST | Create a new experiment |
| `/api/experiments/{name}` | DELETE | Delete an experiment |
| `/api/experiments/{name}/toggle` | PATCH | Enable/disable an experiment |
| `/api/experiments/{name}/push` | POST | Re-push this experiment's gateway set — for a gateway that was down when it was created (502 if still unreachable) |
| `/api/experiments/{name}/results` | GET | Compare model A vs model B stats (`?period_days=7`) |

### Traces

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/traces/ingest` | POST | Receive a trace event from a gateway. Requires a matching `X-Ingest-Key` header when `OSTIARI_INGEST_KEY` is set (constant-time compare). Open when unset in dev; **401 when unset under `OSTIARI_ENV=production`**. The gateway reporter sends the configured key. |
| `/api/traces/recent` | GET | Get recent traces (`?limit=50`) |
| `/api/traces/spans` | GET | Traces grouped into session/plan/step spans |
| `/api/traces/shadow-report` | GET | What shadow-mode gateways would have blocked |
| `/api/traces/delegation-report` | GET | Agent-to-agent delegation summary |
| `/ws/traces` | WebSocket | Live stream of trace events (for the UI) |

### Protocol Governance (A2A) & Trust

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/a2a-agents` | GET | List registered peer agents |
| `/api/a2a-agents/{gateway_id}` | POST | Register a peer agent on a gateway |
| `/api/a2a-agents/{agent_id}` | DELETE | Remove a peer agent |
| `/api/agent-routing` | GET/POST | Per-gateway agent routing rules |
| `/api/agent-routing/{gateway_id}` | GET | Rules for one gateway |
| `/api/agent-routing/{gateway_id}/{agent_id}` | DELETE | Remove a rule |
| `/api/routing-controls/{gateway_id}` | GET | Read stored task-classification and budget-period controls |
| `/api/routing-controls/{gateway_id}/task-classification` | PUT | Persist and push keyword categories and target models |
| `/api/routing-controls/{gateway_id}/budget-reset` | PUT | Persist and push the UTC budget-reset schedule |
| `/api/routing-controls/{gateway_id}/reset-spend` | POST | Start a new gateway and per-agent budget period immediately |
| `/api/trust/scores` | GET | Trust scores per agent |
| `/api/trust/apply` | POST | Apply trust-based gating |
| `/api/trust/disable` | POST | Turn trust gating off |

### Discovery, Compliance, ROI

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/discovery/agents` | GET | Agents found on the network but not registered |
| `/api/discovery/onboard` | POST | Register a discovered agent |
| `/api/compliance/frameworks` | GET | Supported compliance frameworks |
| `/api/compliance/report` | GET | Generate a compliance report |
| `/api/roi/report` | GET | "Damage prevented" / savings report |
| `/api/roi/cost-model` | GET/POST | Configure the incident cost model |
| `/api/roi/cost-model/reset` | POST | Restore defaults |

### Audit Log

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/audit` | GET | List audit entries (`?resource_type=&actor=&action=&limit=100`) |
| `/api/audit/verify` | GET | Verify the audit hash chain |

### Agents

Agents are keyed by **name**, and there is no `PUT` — re-`POST` to replace.

The registry is in-memory and **not** in `state.json`: the demo agents are
re-seeded on each start by `demo_seed.seed_demo_agents()` (skipped under
`OSTIARI_NO_DEMO=1`), and an agent you register yourself is lost on restart.
`state.json` persists quotas, experiments, models, providers, the ROI cost model,
and the token-broker config — that list, and nothing else.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents` | GET | List all registered agents (framework, model, tools, gateway assignment) |
| `/api/agents/{name}` | GET | Get agent details |
| `/api/agents` | POST | Register or replace an agent |
| `/api/agents/{name}` | DELETE | Remove an agent |

### Auth, Users, Providers

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/auth/login` | POST | Exchange credentials for a bearer token |
| `/api/auth/register` | POST | Create a user |
| `/api/auth/me` | GET | Current user + role |
| `/api/auth/users` | GET | List users (admin) |
| `/api/auth/users/{user_id}` | DELETE | Remove a user (admin) |
| `/api/auth/sso/config` | GET | SSO discovery config |
| `/api/auth/sso/login` | GET | Begin the OIDC flow |
| `/api/auth/sso/callback` | GET | OIDC redirect target |
| `/api/providers` | GET/POST | LLM provider credentials |
| `/api/providers/{name}` | PUT/DELETE | Update or remove a provider |
| `/api/providers/{name}/key` | GET | Masked key |
| `/api/providers/{name}/test` | POST | Send a test completion |
| `/api/providers/{name}/health` | GET | Provider reachability |

Browser SSO requires `OIDC_ISSUER`, `OIDC_CLIENT_ID`, and
`OIDC_CLIENT_SECRET`. Register `OIDC_REDIRECT_URI` with the IdP; its local
default is `http://localhost:8400/api/auth/sso/callback`. Set
`OSTIARI_FRONTEND_URL` to the browser-reachable dashboard origin (local default
`http://localhost:9000`). The callback redirects to `/auth/sso-callback` with
the issued token in the URL fragment; the frontend removes it from history,
validates `/api/auth/me`, then stores the session.

This browser flow issues a local Ostiari JWT, so leave `OSTIARI_AUTH_MODE`
unset. `OSTIARI_AUTH_MODE=oidc` plus `OSTIARI_OIDC_*` is the separate mode for
API clients that send IdP bearer tokens directly.

### Gateway Proxy

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/proxy/gateway/{gateway_id}/{path}` | GET, POST | Forward request to a gateway (eliminates CORS, enables Sandbox) |

> The proxy looks up the gateway's registered endpoint and forwards the full
> request. The browser never talks to gateways directly.

### Health

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Control plane health check (unauthenticated) |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | React 19, Tailwind CSS 3, Vite 6, TanStack Query 5 |
| Backend | Python 3.11+, FastAPI, SQLAlchemy (async), Pydantic |
| Database | SQLite (dev) / PostgreSQL (prod) |
| Real-time | WebSockets (trace streaming) |
| Gateway Communication | HTTP push (`POST /config/*`) |
| LLM Cost Estimation | Built-in pricing table (Claude, GPT-4o, Nova, Gemini, etc.) |

---

## Project Structure

```
control-plane/
├── backend/
│   ├── main.py                     # Entry point
│   ├── control_plane/
│   │   ├── app.py                  # FastAPI app + lifespan (load state, seed demo)
│   │   ├── database.py             # Async engine / session
│   │   ├── env.py                  # data_dir() — one writable dir for db + state
│   │   ├── persistence.py          # state.json save/load on shutdown/startup
│   │   ├── demo_seed.py            # Idempotent demo seeding
│   │   ├── auth/                   # Local auth, roles, OIDC SSO
│   │   ├── routers/
│   │   │   ├── gateways.py         # Gateway registry, push, mode
│   │   │   ├── agents.py           # Agent registry (9 agents, 8 frameworks)
│   │   │   ├── agent_routing.py    # Per-gateway agent routing rules
│   │   │   ├── a2a_agents.py       # A2A peer registry
│   │   │   ├── trust.py            # Delegation trust scoring
│   │   │   ├── tools.py            # Tool CRUD + OpenAPI import
│   │   │   ├── policies.py
│   │   │   ├── mcp_servers.py
│   │   │   ├── model_config.py     # Model registry (18 pre-seeded)
│   │   │   ├── quotas.py           # Quota CRUD + push to gateway
│   │   │   ├── approvals.py        # HITL queue
│   │   │   ├── costs.py
│   │   │   ├── metering.py
│   │   │   ├── payments.py         # x402 wallets, pricing, ledger
│   │   │   ├── token_broker.py     # Margin, pools, reconciliation
│   │   │   ├── broker_pilot.py
│   │   │   ├── experiments.py
│   │   │   ├── traces.py           # Ingest, spans, shadow report, /ws/traces
│   │   │   ├── discovery.py
│   │   │   ├── compliance.py
│   │   │   ├── roi.py
│   │   │   ├── providers.py
│   │   │   ├── proxy.py            # Gateway proxy (eliminates CORS)
│   │   │   └── audit.py
│   │   ├── models/                 # DB models + Pydantic schemas + org scoping
│   │   └── services/               # Business logic (push, audit, health check)
│   ├── tests/
│   └── pyproject.toml
├── data/                           # SQLite db + state.json (gitignored)
├── frontend/
│   ├── src/
│   │   ├── components/Layout.tsx   # Sidebar nav sections
│   │   ├── pages/                  # One per sidebar entry (Dashboard, Sandbox, …)
│   │   └── ...
│   └── package.json
└── docs/
    └── getting-started.md          # Step-by-step setup guide
```

---

## Runtime Enforcement (Not Just Informational)

A key architectural distinction: quotas and budgets configured here are
**runtime-enforced on the gateway**, not just informational dashboards.

```mermaid
graph LR
    subgraph "Control Plane (configuration)"
        Q[Quota: $50/day budget<br/>30 req/min rate limit]
    end

    subgraph "Gateway (enforcement)"
        E[Enforcer: actively blocks<br/>requests that exceed limits]
    end

    Q -->|"POST /api/quotas/{id}/push"| E
```

When you push a quota, the gateway:
1. Loads the gateway quota or complete per-agent quota map into its enforcers
2. Checks every LLM request against rate, projected budget, model/provider, and token limits
3. Rejects agent rate/budget failures with 429 on the API shims (the `/invoke` response carries a blocked result)
4. Silently caps `max_tokens` without rejecting (agent gets shorter responses, not errors)
5. Reports actual usage from `/invoke`, `/v1/messages`, and `/v1/chat/completions` so control-plane spend survives gateway restarts

The gateway calculates cost locally using per-model pricing tables (no round-trip
to the control plane needed). Pre-request budget projection estimates cost BEFORE
calling the LLM, preventing budget overshoot.

---

## Demo Fleet Summary

`make demo-full` (from the repo root) brings up this fleet. Everything below is
seeded by `control_plane/demo_seed.py` and the `gateway/register_demo_*.py` /
`register_fleet_tools.py` scripts — no checked-in database.

| Resource | Count | Details |
|----------|-------|---------|
| Gateways | 4 | `crm-agent`:8421, `ops-agent`:8422, `devops-agent`:8424, `analytics-agent`:8425 |
| Agents | 9 | Across 8 frameworks (OpenAI, Anthropic, Strands, Bedrock, AgentCore, CrewAI, LangGraph, gateway-invoke) |
| Tools | 23 | 12 on crm-agent, 4 on ops-agent, 4 on devops-agent, 3 on analytics-agent — all pointed at the demo tools server on :9300 |
| Policies | 3 created | `block-destructive` (crm-agent), `ops-guard` (ops-agent), `devops-guard` (devops-agent). One per gateway that registers a destructive tool; `analytics-agent` gets none because it registers none |
| Quotas | 4 | One per gateway; `current_spend` summed from real usage records so the budget bars match the Costs page |
| MCP Servers | 2 | Real stdio servers via `npx`: draw.io and filesystem (sandboxed to `/tmp/ostiari-mcp-sandbox`) |
| Models | 18 | Pre-seeded from AxonLLM; 4 appear in metered usage |
| Wallets | 8 | Varied balances — one nearly drained so payments actually block |
| Approvals | 8 | 4 pending, 4 already decided |
| Experiments | 3 | `haiku-vs-sonnet`, `gpt4o-vs-o3`, `cost-routing-test` |
| Broker Pools | 2 | One healthy, one deliberately under its low-water mark |
| Usage Records | 647 | Across 8 agents and 4 models, for Costs / Metering / ROI |
| Live Traces | Seeded | Plus live streaming from real Sandbox/gateway calls |

Set `OSTIARI_NO_DEMO=1` (what `make clean-start` does) to skip the seeders above.
Two things it doesn't cover:

- **The 18 models come back regardless.** `seed_models()` runs at import time at
  the bottom of `routers/model_config.py`, outside the `OSTIARI_NO_DEMO` gate. By
  design — the catalog is a routing table with pricing, not sample data.
- **`state.json` is restored before the gate is checked**, so quotas,
  experiments, models, providers, the ROI cost model, and the token-broker config
  from an earlier demo run reappear. `make clean-start` deletes
  `control-plane/backend/data/state.json`, the path from before `env.data_dir()`
  centralized it; the live file is `control-plane/data/state.json`. Remove that
  one for a genuinely empty control plane.

---

## Agent Gateway (Rename from "Sidecar")

The UI displays "Agent Gateways" instead of "Sidecars." The concept: an Agent
Gateway can be deployed in three topologies — not just as a K8s sidecar.

```mermaid
graph LR
    subgraph "Mode 1: Sidecar"
        A1[Agent] --- G1[Gateway<br/>per-pod]
    end

    subgraph "Mode 2: Shared"
        A2[Agent A] --> G2[Shared Gateway]
        A3[Agent B] --> G2
    end

    subgraph "Mode 3: NAT"
        A4[All Agents] --> G3[Global Gateway]
    end
```

| Topology | Description | Manifest |
|------|-------------|----------|
| **Sidecar** | Per-pod in K8s, co-located with the agent | `deploy/kubernetes/gateway-sidecar.yaml` |
| **Shared gateway** | One gateway serving multiple agents (per-agent auth) | `deploy/kubernetes/gateway-shared.yaml` |
| **Global NAT gateway** | Network-level proxy all agents route through | — |

This is a deployment topology, chosen by how you run the gateway. It is distinct
from the gateway's **enforcement mode** (`enforce` \| `shadow`), which is the
`mode` field on a gateway record and is set via `PUT /api/gateways/{id}/mode`.

> The sidecar→gateway rename is complete: routes are `/api/gateways/*` and
> `/api/proxy/gateway/*`. There is no `/api/sidecars` alias — it returns 404.
> Filter query params are `?gateway_id=`, not `?sidecar_id=`.

---

## Per-Agent Tool Authorization

Least privilege enforcement when multiple agents share a gateway:

- Each agent gets explicit tool grants (exact names or wildcards like `github.*`)
- Unregistered agents denied by default
- Wildcard `*` for full access
- Configured on the gateway (`/config/agent-auth`), surfaced in the UI as
  **Quotas (per agent)**

---

## Full Cost Enforcement (Gateway-Local)

The gateway calculates cost locally and enforces budgets without round-tripping
to the control plane:

- **Pre-request budget projection** — blocks before calling the LLM if cost would exceed budget
- **Local pricing table** — per-model pricing for all supported models
- **Budget alerts** at 80% / 90% / 100% thresholds
- **Silent `max_tokens` cap** — reduces output length without erroring

---

## Related

- [Gateway](../gateway/) — the runtime proxy that sits between agents and tools
- [Features and Flows](../docs/features-and-flows.md) — complete capability map
  and end-to-end runtime flows
- [Getting Started Guide](docs/getting-started.md) — full walkthrough from zero to running fleet
- [Control Plane Guide](../docs/control-plane-guide.md) — novice-friendly tour with diagrams and pitfalls
