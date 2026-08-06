# Ostiari — Configuration, Orchestration & Lifecycle Management

## The Fundamental Design Principle

The Control Plane is a management UI. It doesn't *own* the runtime components — it pushes config to gateways and receives traces from them. If a gateway isn't running, "Push" has nowhere to go.

This document explains how Ostiari manages the lifecycle of the components it governs.

---

## How Production Control Planes Handle This

| Approach | How it works | Example |
|---|---|---|
| **Desired state model** | CP stores the *desired* config. Gateways pull on startup. Push is optional (accelerates propagation). | Kubernetes: you apply a manifest → etcd stores it → kubelet pulls it |
| **Push + reconciliation** | CP pushes immediately AND stores. If gateway was down, it reconciles on reconnect. | Istio: Pilot pushes to Envoy, but also serves via xDS on connect |
| **Heartbeat-driven** | Gateways heartbeat to CP. If missed → mark unhealthy. On reconnect → CP pushes latest config. | Kong: CP stores config, gateways poll for changes |

---

## How Ostiari Works

### 1. Control Plane is the Source of Truth

All config lives in the Control Plane — policies, quotas, models, agent-auth, provider keys. Gateways are *consumers* of this config, not owners.

If a gateway restarts, it doesn't lose config: registration returns the full bundle
and the gateway applies it — tools, policy, MCP servers, payments, quotas,
agent-auth, A/B experiments, and the enforcement mode. Anything left in the
pending-push queue arrives beside the bundle as `config_updates` and is applied
after it (§2, Caveat 2). What is *not* recoverable is a `push-config` body that was
never persisted; clicking Push on the Gateways page re-sends stored state.

### 2. Push is Best-Effort, Not Required

Clicking "Push" sends config to the gateway NOW. But if the gateway isn't reachable, the config is still saved in the Control Plane database.

When the gateway reconnects (via heartbeat), it automatically receives the latest config.

Push therefore never reports a hard failure — it either applies immediately or
queues. Two caveats on the queue, though, and the second one is a real bug.

**Caveat 1 — the queue doesn't survive a control-plane restart.** `config_queue`
in `control_plane/routers/gateways.py` is a plain in-memory dict. The durable part
is the stored config itself, so a gateway that registers later still gets the
current state in its bundle; what's lost is the delta that was waiting. Restarting
the control plane while a gateway is down is the case to watch.

**Caveat 2 — the queue is a sibling key, not part of the bundle.** `gateway_register`
drains `config_queue` into a top-level `config_updates` on the response, *beside*
`config`, and `lifecycle.register()` applies the bundle first and then each queued
update in order — so a queued change wins over the stored config it was meant to
change. It cannot be nested inside `config`, because the gateway applies that as a
single document and an unrecognized key inside it is silently dropped. (It used to be
`bundle["queued_updates"]`, which is exactly what happened: the pop cleared the queue
and the payload went nowhere, so no later heartbeat could recover it either.)

The name matches the heartbeat path deliberately — `_heartbeat_loop` reads
`config_updates` and applies each entry the same way, so both reconnect paths behave
identically.

What was at risk was narrow but real: the register bundle carries the current stored
config anyway, so a queued policy edit that was also persisted arrived via
`bundle["policy"]`. Only a delta that was *never* stored was lost — which is exactly
what `POST /{id}/push-config` sends, since it forwards an arbitrary operator-supplied
body without persisting it. That's the Policies and Quotas page Push buttons (see §4).

### 3. Gateway Lifecycle

```
Gateway starts
  → POST /api/gateways/{id}/register (announces itself, advertising callback_url)
  → CP auto-creates the record if it doesn't exist, marks healthy
  → CP responds with full config bundle (tools, policy, mode, quotas,
    agent_auth, mcp_servers, and a2a_agents when any are stored)
  → Gateway applies config, reconnects MCP servers + A2A peers, begins serving

Running
  → Every 30s: POST /api/gateways/{id}/heartbeat
  → CP marks healthy and returns queued deltas under "config_updates"
  → Gateway applies each one

Gateway unhealthy
  → last heartbeat older than 90s → CP marks it "unhealthy" (red dot in UI).
    The sweep runs every 15s, so the transition lands 90–105s after the last beat.
  → Config changes queue until reconnect

Gateway reconnects
  → Heartbeat from a non-healthy record → CP sends the full bundle plus the queue
  → Status returns to "healthy" (green dot)
```

Both reconnect paths deliver the queue the same way — as a top-level
`config_updates` list applied entry by entry after the full bundle, so a queued
change wins over the stored config. See §2, Caveat 2.

### 4. Push Button Semantics in the UI

`POST /api/gateways/{id}/push-config`:

| Gateway Status | What Push Does | Response |
|---|---|---|
| 🟢 Healthy | Forwarded to the gateway's `/config` immediately | `{"status": "applied"}` |
| 🟢 Healthy but now unreachable | Marked unhealthy, config queued | `{"status": "queued", "reason": "became_unreachable"}` |
| 🔴 Unhealthy | Queued for the next heartbeat | `{"status": "queued", "reason": "gateway_offline"}` |
| ⚪ No control-plane record | Rejected — there's nothing to push *to* | `404 Gateway not found` |

The last row is the one to know: a gateway must exist as a control-plane record
before config can be pushed to it. Gateways create their own record on first
registration, so in practice this only bites when you push to an id that has
never come up.

**Two different Push buttons, two different routes.** They are not
interchangeable:

| UI | Route | Body | Persisted? |
|---|---|---|---|
| Gateways page ↑ icon | `POST /api/gateways/{id}/push` | built from the DB by `push_service._build_config` | yes — it *is* the stored state |
| Policies page **Push** | `POST /api/gateways/{id}/push-config` | `{"policy": …}` supplied by the browser | **no** |
| Quotas page **Push** | `POST /api/gateways/{id}/push-config` | `{"quota": …}` supplied by the browser | **no**, and not enforced either |

`push-config` forwards whatever body it's given to the gateway's `POST /config`,
which is a **whole-document replace** that applies only tools + policy. So the
Policies page's Push clears the gateway's tool registry and resets its enforcement
mode to `enforce`, and the Quotas page's Push does nothing at all. Full detail and
reproductions: [gateway-architecture.md → The /config partial-push
trap](gateway-architecture.md#the-config-partial-push-trap).

Prefer the **Gateways page** Push (or `POST /api/gateways/push-all`): it rebuilds
the bundle from stored state, so it can't clear anything or leave the gateway
holding config the control plane doesn't know about.

**Enforcement mode survives a restart.** `PUT /api/gateways/{id}/mode` persists the
mode in the gateway record and pushes it live, `_build_config` always sends `mode`
explicitly, and `_apply_bundle` applies it — **first**, before tools or policy, so a
gateway the operator left in shadow doesn't spend even one request enforcing while
the rest of the bundle lands:

```
PUT /api/gateways/probe-gw/mode {"mode":"shadow"}
  → CP record: shadow      → GET :8479/config/mode: {"mode":"shadow"}   ✓ live push

# restart the gateway
  → CP record: shadow      → GET :8479/config/mode: {"mode":"shadow"}   ✓ restored
```

An unrecognized mode value is **ignored rather than defaulted**. A typo must not flip
a gateway that was deliberately observing into enforcement — the dangerous direction
here is toward *enforcing*, since that starts blocking traffic you meant only to
watch.

This used to drift: `_apply_bundle` never read the key, so a restarted gateway came
up `enforce` regardless, while the Gateways page kept rendering `Shadow` (it shows the
stored record, not the gateway's live state — so verify with the gateway's
`GET /config/mode` if you need the truth).

---

## What the Control Plane Manages

| Concern | CP's Role | How |
|---|---|---|
| **Policies** | Define and push | Stores rules, pushes to gateways on change |
| **Quotas** | Set limits | Per-gateway and per-agent limits, enforced at runtime by gateway |
| **Model access** | Control who uses what | Per-agent model/provider restrictions |
| **Budgets** | Track and enforce | Per-agent spend tracking, alerts, auto-reset |
| **Providers** | Configure keys | Store encrypted API keys, test connectivity |
| **Traces** | Collect and display | Gateways fire-and-forget traces to CP |
| **Costs** | Aggregate and alert | Per-model, per-agent cost attribution |
| **Health** | Monitor | Heartbeat-based health tracking |

---

## What the Control Plane Does NOT Manage

| Concern | Who Owns It | Why |
|---|---|---|
| **Starting/stopping gateways** | Orchestrator (K8s, ECS, systemd) | CP manages config, not deployment |
| **Deploying agents** | Agent teams | CP controls what agents can do, not where they run |
| **Infrastructure provisioning** | Terraform, CDK, CloudFormation | CP sits on top of infra, doesn't create it |
| **Network routing** | Service mesh, DNS, load balancers | CP pushes config over the network, doesn't control the network |
| **Secret rotation** | Secrets Manager, Vault | CP can read secrets and push them, but doesn't own rotation logic |

The CP's job is: **tell gateways what the rules are, and show operators what's happening.**

Not: start processes, provision resources, or manage infrastructure.

---

## Analogy

Think of it like the AWS Console:
- You can configure a security group (the rules)
- You can see EC2 instance health (observability)
- You cannot SSH into an instance from the console (that's your terminal)
- You don't start the EC2 service itself (that's AWS infrastructure)

Ostiari Control Plane:
- You configure policies, quotas, models (the rules)
- You see traces, costs, health (observability)
- You don't start the gateway process (that's your orchestrator)
- You don't deploy agents (that's your CI/CD)

---

## Lifecycle in Action (Example)

```
CP starts
  ← Gateway 1 registers (POST /api/gateways/crm-agent/register)
  ← Gateway 2 registers (POST /api/gateways/devops-agent/register)
  ← Gateway 3 registers (POST /api/gateways/ops-agent/register)
  ← Gateway 4 registers (POST /api/gateways/analytics-agent/register)

Running:
  ← Gateway 1 heartbeats every 30s
  ← Gateway 2 heartbeats every 30s
  ← Gateway 3 heartbeats every 30s

Operator pushes policy change to Gateway 2:
  → CP checks: Gateway 2 healthy? Yes → forward immediately ✓

Gateway 3 goes down:
  → CP marks unhealthy after 90s (red dot)
  → Operator pushes quota change to Gateway 3
  → CP queues it

Gateway 3 restarts:
  → Registers → gets full config bundle (tools, policy, mode, quotas,
    agent_auth, ab_experiments, …) plus the drained queue as config_updates
  → Applies the bundle, then each queued change on top (§2, Caveat 2)
  → Starts heartbeating → green dot again
```

---

## Fleet Status (Live Demo)

What `make demo-full` brings up:

| Service | Port | Gateway id | Tools |
|---------|------|---|---|
| Control Plane Backend | 8400 | — | — |
| Control Plane Frontend | 9000 | — | — |
| CRM Gateway | 8421 | `crm-agent` | 12 tools + `block-destructive` policy, plus the draw.io and filesystem MCP servers |
| Ops Gateway | 8422 | `ops-agent` | 4 tools + `ops-guard` policy |
| DevOps Gateway | 8424 | `devops-agent` | 4 tools, **no policy** — see below |
| Analytics Gateway | 8425 | `analytics-agent` | 3 tools, no policy (no destructive tools) |

Two policies get created, not three. `register_fleet_tools.py` skips the
devops-agent policy on the assumption that a `devops-strict` policy already
exists to block `github.delete_repo` — but nothing in the repo creates one, so on
a fresh demo that tool is unguarded. It's a demo-seeding gap rather than an
enforcement bug (add a policy on the Policies page to see the block fire), but
don't read the demo fleet as showing every destructive tool covered.

Open http://localhost:9000 → Gateways page. You should see all 4 with green health dots, heartbeating every 30s. Push a policy change and it'll go to the correct gateway immediately (✓).

Each gateway must start with the **same id as its control-plane record** — that's
what lets the control plane push it tools and policy on registration.

---

## For the Demo

In a demo environment:
- Gateways ARE running (ports 8421, 8422, 8424, 8425) — Push works immediately
- Traces ARE flowing — Live Traces shows real data
- Health IS green — all heartbeats active

If a gateway isn't running:
- Push reports `queued` (`reason: gateway_offline` or `became_unreachable`)
- Health shows red after 90s
- Traces stop (obviously)
- Config changes written through the *stored* state (Policies/Quotas/Tools CRUD,
  mode) are persisted and arrive in the next register bundle. A `push-config` body
  is not stored, so it survives only via the pending-push queue — and only if the
  control plane doesn't restart first. See §2, Caveats 1 and 2.

---

## Configuration Flow (End to End)

```
Operator changes policy in UI
  → Control Plane saves to database
  → If gateway healthy: Push immediately
      · Policies page Push → POST /config       (whole-document replace)
      · Gateways page Push → POST /config       (rebuilt from stored state)
      · POST /api/policies/{id}/push → POST /config/policy  (partial, safe)
  → If gateway offline: queue (delivered on the next heartbeat, not on register)
  → Gateway applies immediately (hot-reload, no restart)
  → Next tool call uses new policy
  → Trace shows new policy decision
  → Operator sees result in Live Traces
```

Only `POST /api/policies/{id}/push` hits the gateway's *partial* `/config/policy`
endpoint — the one that changes policy and nothing else. Neither Push button in the
UI uses it.

Total propagation time (healthy gateway): **< 1 second**
Total propagation time (reconnecting gateway): **≤ heartbeat interval (30s)**

---

## Gateway Lifecycle Management — Separation of Concerns

### The Rule: CP manages config. Orchestrator manages lifecycle.

| Concern | Owner | Tool |
|---|---|---|
| **Start/stop gateways** | Platform team via orchestrator | K8s Deployment, ECS Service, systemd |
| **Scale gateways** | HPA / auto-scaling | K8s HPA, ECS target tracking, KEDA |
| **Restart on crash** | Orchestrator | K8s restartPolicy, ECS task restart |
| **Deploy new version** | CI/CD pipeline | ArgoCD, Flux, CodePipeline |
| **Config (policies, quotas, models)** | Ostiari Control Plane | Push via UI or API |
| **Health visibility** | Ostiari Control Plane | Heartbeat → green/red dots |
| **Drain before shutdown** | Gateway itself | Graceful shutdown hook |

### Why NOT from the CP

1. **Blast radius** — if the CP has a bug and accidentally stops all gateways, every agent goes dark. Separation of concerns prevents this.
2. **Auth scope** — a policy operator shouldn't have "kill gateway" power. K8s RBAC handles infra access separately.
3. **Reliability** — if the CP goes down, gateways keep running with cached config. If the CP could stop gateways, a CP outage = total outage.
4. **Existing tooling** — K8s, ECS, Terraform already solve lifecycle perfectly. Rebuilding it in the CP adds complexity with no value.

### What the CP SHOULD show (observability, not control)

- 🟢 Gateway healthy (heartbeating)
- 🔴 Gateway unhealthy (missed heartbeats)
- 📊 Gateway metrics (requests/sec, latency, error rate)
- ⚠️ "Gateway X hasn't heartbeated in 5 min" alert
- 📝 "Last config push: 2 min ago, applied successfully"

### What the CP should NOT have

- ❌ "Stop Gateway" button
- ❌ "Restart Gateway" button
- ❌ "Scale to N replicas" slider
- ❌ "Deploy version X" action

### The Pattern (how enterprises do it)

```
Developer commits policy change
  → CP API receives it
  → CP stores in DB
  → CP pushes to healthy gateways (< 1s)

Platform team deploys new gateway version
  → CI/CD builds new image
  → K8s rolling update (one pod at a time)
  → New pod starts → registers with CP → pulls config
  → Old pod drains → graceful shutdown
  → Zero downtime

Gateway crashes
  → K8s detects (liveness probe fails)
  → K8s restarts pod
  → New instance registers → pulls config → healthy
  → CP showed red dot for ~30s, then green again

Scale event (traffic spike)
  → HPA triggers (CPU > 70%)
  → New pods start → register → pull config
  → CP shows 5 gateways instead of 3
  → No operator action needed
```

### Analogy

The CP is like a thermostat — it sets the desired temperature (config) and shows the current temperature (health). It does NOT turn the furnace on/off (that's the HVAC system / orchestrator).
