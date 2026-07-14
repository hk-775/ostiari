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

If a gateway restarts, it doesn't lose config. It pulls the latest from the Control Plane on startup.

### 2. Push is Best-Effort, Not Required

Clicking "Push" sends config to the gateway NOW. But if the gateway isn't reachable, the config is still saved in the Control Plane database.

When the gateway reconnects (via heartbeat), it automatically receives the latest config.

This means Push never truly "fails" — it either applies immediately or queues for later.

### 3. Gateway Lifecycle

```
Gateway starts
  → POST /api/gateways/register (announces itself to CP)
  → CP responds with full config bundle (policies, quotas, tools, agent-auth)
  → Gateway applies config and begins serving

Running
  → Every 30s: POST /api/gateways/{id}/heartbeat
  → CP responds with any pending config changes since last heartbeat
  → Gateway applies deltas

Gateway unhealthy
  → 3 missed heartbeats → CP marks gateway as "unhealthy" (red dot in UI)
  → Config changes queue until reconnect

Gateway reconnects
  → Full config sync (CP pushes entire current state)
  → Queued changes applied
  → Status returns to "healthy" (green dot)
```

### 4. Push Button Semantics in the UI

| Gateway Status | What Push Does |
|---|---|
| 🟢 Healthy | Config sent immediately → ✓ |
| 🔴 Unhealthy | Config saved + queued → "Will apply on reconnect" |
| ⚪ Unregistered | Config saved → "Gateway not yet registered" |

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
  → Registers → gets full config bundle (including the queued quota change)
  → Starts heartbeating → green dot again
```

---

## Fleet Status (Live Demo)

| Service | Port | Status |
|---------|------|--------|
| Control Plane Backend | 8400 | ✅ |
| Control Plane Frontend | 9000 | ✅ |
| CRM Gateway | 8421 | ✅ Registered, heartbeating, 3 tools loaded |
| Ops Gateway | 8422 | ✅ Registered, heartbeating, 2 tools loaded |
| DevOps Gateway | 8423 | ✅ Registered, heartbeating, 4 tools + policy loaded |
| Analytics Gateway | 8424 | ✅ Registered, heartbeating, 1 tool loaded |

Open http://localhost:9000 → Gateways page. You should see all 4 with green health dots, heartbeating every 30s. Push a policy change and it'll go to the correct gateway immediately (✓).

---

## For the Demo

In a demo environment:
- Gateways ARE running (ports 8421-8424) — Push works immediately
- Traces ARE flowing — Live Traces shows real data
- Health IS green — all heartbeats active

If a gateway isn't running:
- Push shows "Gateway offline — config saved, will sync on reconnect"
- Health shows red
- Traces stop (obviously)
- All config changes are still persisted and will apply when gateway restarts

---

## Configuration Flow (End to End)

```
Operator changes policy in UI
  → Control Plane saves to database
  → If gateway healthy: Push immediately (POST /config/policy)
  → If gateway offline: Queue for next heartbeat
  → Gateway receives config
  → Gateway applies immediately (hot-reload, no restart)
  → Next tool call uses new policy
  → Trace shows new policy decision
  → Operator sees result in Live Traces
```

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
