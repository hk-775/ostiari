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

## For the Demo

In a demo environment:
- Gateway IS running (port 8421) — Push works immediately
- Traces ARE flowing — Live Traces shows real data
- Health IS green — heartbeat active

If the gateway isn't running:
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
