# Ostiari Control Plane — A Complete Guide

*A knowledge document for operators, evaluators, and newcomers. Explains what the
control plane is, every option in it, how requests flow, and how to run it well.*

---

## 1. Start here: what is this, in plain terms?

Imagine your company has a bunch of AI agents doing real work — one answers
customer emails, one deploys code, one queries the analytics warehouse. Each
agent can *call tools*: send an email, run a SQL query, delete a repo, pay for
an API. That power is the whole point of agents, and also the whole risk. An
agent that can send email can leak data; one that can run SQL can drop a table.

**Ostiari sits between every agent and the tools it calls, and decides — per
call — whether to allow it, ask a human first, or block it.** Think of it as a
security checkpoint at an airport: every passenger (tool call) walks through the
same scanner (policy + risk scoring) before boarding (execution).

There are two halves:

- **The Gateway** (the "sidecar") — a small proxy that runs *next to* each agent.
  Every tool call the agent makes goes through its gateway, which enforces the
  rules and then either forwards the call or stops it.
- **The Control Plane** — the central brain and dashboard. You configure rules
  here once, and it *pushes* them to all the gateways. It also *collects* what
  every gateway saw (traces) so you can watch the whole fleet from one screen.

> **Analogy:** The gateways are the security guards standing at each door. The
> control plane is the security office — where you write the rulebook, watch the
> camera feeds, and see reports. Guards enforce; the office decides and observes.

```
                        ┌───────────────────────────────┐
                        │        CONTROL PLANE           │
                        │   (the brain + dashboard)       │
                        │                                 │
                        │  • You set policies here        │
                        │  • It shows all activity        │
                        └───────────────────────────────┘
                          ▲   │ push config      ▲
              report      │   │ (policies,       │ report
              traces      │   ▼  quotas, prices) │ traces
                ┌─────────┴──┐  ┌────────────┐  ┌┴───────────┐
                │  Gateway   │  │  Gateway   │  │  Gateway   │   ... one per agent
                │ crm-agent  │  │ ops-agent  │  │devops-agent│
                └─────┬──────┘  └─────┬──────┘  └─────┬──────┘
                      │               │               │
                 ┌────▼───┐      ┌────▼───┐      ┌────▼───┐
                 │ Agent  │      │ Agent  │      │ Agent  │
                 └────────┘      └────────┘      └────────┘
                      │               │               │
                 tool calls      tool calls      tool calls
                 (email, SQL, deploy, pay, MCP tools, other agents…)
```

**Why split it this way?** Because enforcement must be *local and fast* (the
gateway is in the request path, milliseconds matter), but configuration and
observability must be *central* (you don't want to log into 200 machines to
change one rule). The control plane is the single pane of glass; the gateways
are the muscle.

---

## 2. The one diagram that explains everything: the request flow

Every tool call an agent makes runs through the **gateway gate chain**. This is
the heart of the product — read it once and the rest of the doc clicks into
place. The control plane is what *configures* each of these gates.

```
   Agent wants to call a tool  (e.g. "db_delete", "send_email", "a2a.payments")
                │
                ▼
   ┌────────────────────────────────────────────────────────────────┐
   │                     GATEWAY GATE CHAIN                            │
   │                                                                  │
   │  1. DELEGATION   Is this agent allowed to call THAT agent?       │  ← Protocol Governance
   │        │          (only for agent→agent "a2a." calls)            │
   │        ▼                                                         │
   │  2. AUTH         Is this agent allowed to use THIS tool at all?  │  ← per-agent authorization
   │        │                                                         │
   │        ▼                                                         │
   │  3. QUOTA        Has this gateway/agent hit its rate/budget cap? │  ← Quotas
   │        │                                                         │
   │        ▼                                                         │
   │  4. RISK         Score 0-100. allow / intervene / block?         │  ← Policies + risk engine
   │        │                                                         │
   │        ▼                                                         │
   │  5. APPROVAL     Scored *intervene*? Pause for a human (202).    │  ← Approvals (HITL)
   │        │          Re-submit with X-Approval-Id once approved.    │
   │        ▼                                                         │
   │  6. PAYMENT      Does this call cost money? Can the wallet pay?  │  ← Payments (x402)
   │        │                                                         │
   │        ▼                                                         │
   │  7. EXECUTE      Forward to the real tool (HTTP / MCP / agent).  │
   │        │          Meter usage; draw down token pool.             │  ← Metering, Token Broker
   │        ▼                                                         │
   │  8. TRACE        Record everything → report to control plane.    │  ← Live Traces, Audit, ROI
   └────────────────────────────────────────────────────────────────┘
                │
                ▼
   Result returns to the agent (or "blocked" / "needs approval" / a shadow mock)
```

LLM calls (not tool calls) run a parallel chain on the same gateway — agent
authorization, injection/PII detection, quota, then routing through the embedded
AxonLLM engine. See [gateway-architecture.md](gateway-architecture.md) and
[axon-router.md](axon-router.md).

Three cross-cutting ideas that ride on top of this chain:

- **Shadow mode.** A gateway can run in *shadow* instead of *enforce*. In shadow,
  every gate still evaluates and records what it *would* have done — but nothing
  is ever blocked and no real side effect runs. It's "try before you enforce."
- **Human-in-the-loop is opt-in.** Gate 5 only engages when the gateway runs with
  `OSTIARI_HITL=on`. With it off the tier is advisory in dev — the score is
  recorded and the call proceeds — but in production it is *refused*, because
  production is fail-closed and an intervene nobody can resolve is not an allow.
  Worth knowing before you conclude that "intervene" means "someone is checking."
  The queue humans act in is the Approvals page (§7.4), which explains the
  dev/production split in full.
- **Everything is configured centrally.** You never edit a gateway directly. You
  change a policy / quota / price in the control plane and click **Push**; the
  control plane sends the new config to the gateway(s).

Keep this diagram in mind. Every control-plane page below is either **setting up
one of these gates** (Configure / Control / Monetize) or **watching what the
gates did** (Observe / Test).

---

## 3. The control plane, section by section

The dashboard's left nav has six sections. **The nav is ordered for daily use —
what you look at most is on top:**

```
  OBSERVE ─▶ CONTROL ─▶ MONETIZE ─▶ CONFIGURE ─▶ TEST ─▶ ADMIN
  (watch)     (rules)   (charging)   (register)   (verify)  (admin-only)
```

- **Observe** — Dashboard, Live Traces, Shadow Report, **Approvals**, Costs,
  Metering, Audit Log, Compliance, ROI.
- **Control** — Models, Policies, Quotas, Agent Quotas.
- **Monetize** — Payments, Token Broker.
- **Configure** — **Discovery**, Agent Gateways, Agents, Tools, MCP Servers,
  Protocol (A2A).
- **Test** — Sandbox, Experiments, Architecture.
- **Admin** — LLM Providers, Users. *(visible to admins only)*

**But you'd set it up in almost the opposite order.** This guide follows the
lifecycle, not the nav:

```
  CONFIGURE ─▶ CONTROL ─▶ MONETIZE ─▶ (agents run) ─▶ OBSERVE ─▶ TEST/tune
  (register)   (rules)    (charging)                  (watch)    (verify)
```

So §4 is Configure, §5 is Control, §6 is Monetize, §7 is Observe, §8 is Test, and
§9 is Admin — read top-to-bottom to stand a deployment up, and use the nav order
once it's running.

Two things worth knowing about the nav specifically:

- **Protocol (A2A)** lives under *Configure* in the nav, but it's genuinely a
  governance rule, so this guide covers it with the other rules in §5.4.
- One page is routed but **absent from the nav**: **Token Efficiency**
  (`/efficiency`), reachable only by typing the URL. See §7.9.

---

## 4. CONFIGURE — tell Ostiari what exists

Before Ostiari can govern anything, it needs to know your world: which gateways
are out there, which agents run behind them, what tools they can call.

### 4.1 Discovery (`/discovery`) — find the agents you didn't register

**What it is:** the first page in the Configure section, and the answer to "what
am I missing?" Ostiari correlates the agent identities it can *observe* — gateway
traffic and configured cloud signals — against the agents you actually
*registered*, and sorts every one into four buckets:

| Status | Meaning |
|---|---|
| **Shadow — ungoverned** | seen in traffic, never registered. Nothing governs it. |
| **Registered, off gateway** | registered and assigned, but still observed only outside that gateway. |
| **Governed** | registered and observed through its assigned Ostiari gateway. |
| **Registered, unseen** | registered but no traffic. Stale? Decommissioned? |

**Novice framing:** the airport analogy again — this is the sweep that finds
people who got onto the concourse without passing a checkpoint. You cannot write
a policy for an agent you don't know about, so this page comes *before* the
registration pages, not after.

Each shadow row shows the evidence and an **Onboard** action. The operator picks
a tenant-owned gateway; Ostiari persists the agent, adds a least-privilege
agent-auth entry, and pushes the complete authorization/quota bundle. If the
gateway is offline, the policy remains stored for reconnect.

Onboarding cannot rewrite an external workload's SDK endpoint or network path.
The row stays **Registered, off gateway** until traces prove that the identity
is using its assigned gateway. This avoids reporting a registry write as active
governance.

Production AWS discovery is opt-in with `OSTIARI_DISCOVERY_AWS=1` and is bound
to exactly one tenant by `OSTIARI_DISCOVERY_AWS_ORG`. The available collectors
query configured CloudTrail Lake event data stores, inventory Bedrock Agents,
and read an explicit agent-id tag through the Resource Groups Tagging API.
Collector failures appear on the page without suppressing healthy sources.
See `control-plane/README.md` for environment variables and IAM permissions.

**Best practice:** treat a non-zero shadow count as a work item, not a metric.
Onboard or shut down each one. Then check "Registered, unseen" — a registered
agent with no traffic is either dead config or an agent talking to a provider
directly, around its gateway.

**What to avoid:** reading this page cross-tenant. Shadow AI is computed as
*seen minus known*, so an unscoped read reports another tenant's agents as your
shadow AI. This was a real bug — see §10a.

### 4.2 Agent Gateways (`/gateways`)

**What it is:** the list of every gateway (sidecar) registered with the control
plane. Each row is one deployed proxy — its ID, endpoint, health (last
heartbeat), tool count, and **enforcement mode (enforce / shadow)**.

**Novice framing:** this is your roster of security guards. Each guard stands in
front of one agent. This page tells you which guards are on duty (healthy), how
many rules they're carrying, and whether they're actively stopping people
(enforce) or just taking notes (shadow).

**The key option — enforce vs. shadow (per gateway):**

```
   ENFORCE mode                        SHADOW mode
   ───────────                         ───────────
   blocked call → 403, tool never runs blocked call → recorded as
   real side effects happen             "would block", tool NOT run,
   for allowed calls                    synthetic response returned
                                        → zero real side effects
```

**Best practice:** new gateways start in **shadow**. Watch the Shadow Report for
a week. Only flip to enforce once you've confirmed it won't block legitimate
work. This is the single most important operational habit.

**What to avoid:** flipping a brand-new gateway straight to enforce in
production. You will block a real workflow on day one and erode trust in the
tool. Shadow first, always.

### 4.3 Agents (`/agents`)

**What it is:** the AI agents themselves — name, the framework they're built on
(OpenAI, Anthropic, LangGraph, CrewAI, Strands, AutoGen, … or "Other"), and
which gateway governs them.

**Novice note — why does the framework matter?** Different agent frameworks
describe a "tool call" differently (OpenAI function-calling vs. Anthropic tool
use vs. a LangGraph node). Ostiari's *adapters* normalize all of them into one
shape so the gate chain doesn't care which framework you use. Registering the
framework tells the gateway which adapter to apply.

**Best practice:** register every agent, even ones you think are low-risk.
Unregistered agents are how "shadow AI" sneaks into an org — you can't govern
what you don't know exists. Discovery (§4.1) is how you find the ones you missed.

### 4.4 Tools (`/tools`)

**What it is:** the tools each gateway can proxy — name, HTTP endpoint, method.
When an agent calls `db_query`, the gateway looks up that tool here to know where
to forward it.

**What to avoid:** pointing tools at endpoints the gateway can't actually reach.
A tool that lists but 502s on call looks "configured" but does nothing — worse
than not having it, because it *looks* healthy. (This was a real bug we fixed:
demo gateways had tools pointing at dead endpoints.)

### 4.5 MCP Servers (`/mcp-servers`)

**What it is:** MCP (Model Context Protocol) servers connected to a gateway. MCP
is an open standard for exposing tools to agents. A gateway can connect to MCP
servers in three modes:

- **stdio** — run the MCP server as a local subprocess (e.g. `npx drawio-mcp-server`).
- **remote** — connect over HTTP/SSE to a running MCP server.
- **embedded** — import a Python MCP server in-process (fastest, no network hop).

Once connected, the gateway **discovers** the server's tools and exposes them as
`<prefix>.<tool>` (e.g. `fs.read_file`) — and every one of those calls runs
through the same gate chain as any other tool.

**Novice framing:** MCP is like a USB standard for agent tools. Instead of
hand-wiring each tool, you plug in an MCP server and the gateway auto-discovers
everything it offers — then governs it identically.

```
   Gateway ──connect──▶ MCP server (draw.io) ──discovers──▶ 28 tools
                                                            │
   agent calls "drawio.add-rectangle" ──▶ gate chain ──▶ MCP server executes
```

**Best practice:** prefer stdio/embedded for tools you control; use remote only
for genuinely external services, and set `allowed_tools` / `blocked_tools` to
whitelist exactly what agents may use — MCP servers often expose more than you
want.

---

## 5. CONTROL — set the rules (the heart of governance)

This is where you decide what agents may and may not do. Everything here becomes
one of the gates in the request-flow diagram.

### 5.1 Policies (per tool) (`/policies`)

**What it is:** the rules that decide allow / block / risk-score for tool calls.
A policy is a small document with:

- `allow` — patterns always permitted (e.g. `db_query`, `web_search`).
- `block` — patterns always denied (e.g. `*delete*`, `*.drop`, `db_delete`).
- `risk_adjust` — nudge a tool's risk score up/down (e.g. `send_email: +25`).
- `thresholds` — where allow becomes intervene becomes block on the 0-100 scale.

Policies can be **global** (apply to all gateways) or **gateway-scoped**
(override for one gateway). Gateway-scoped wins.

**Novice framing — the risk score:** every call gets a number from 0 (harmless)
to 100 (dangerous). Two thresholds cut that line into three zones:

```
   0 ─────────────── allow_max ────────── intervene_max ─────────── 100
   │     ALLOW        │      INTERVENE        │        BLOCK          │
   │  (just do it)    │  (ask a human first)  │   (never, refuse)     │
```

**Critical gotcha — pattern matching:** patterns are glob-style (`fnmatch`).
`*.delete` matches `github.delete` but **NOT** `db_delete` (no dot!). If you want
to block `db_delete`, your pattern must be `*delete*` or `db_delete` explicitly.
This exact mistake let an ungoverned delete through in an early demo. **Always
test your block patterns against the real action names.**

**Best practice:**
- Keep an explicit `allow` list for the safe, high-volume tools so they never
  get caught by a broad block pattern.
- Use `risk_adjust` for "grey area" tools (email, file write) rather than a hard
  block — let them go to *intervene* (human approval) instead of failing. In
  production this only means "human approval" if HITL is on; otherwise the
  fail-closed default turns the same score into a refusal (§7.4).
- Write patterns against actual action names and verify in the Sandbox.

**What to avoid:**
- A second active policy with an empty `block: []` — when policies merge, an
  empty list can clobber a good one. (Real bug.) Don't ship empty block lists.
- Over-blocking. If every other call needs human approval, people route around
  the tool. Tune thresholds so *intervene* is rare and meaningful.

### 5.2 Models (per agent) (`/models`)

**What it is:** which LLM models each agent is allowed to use. Lets you say
"the support agent may use claude-haiku and gpt-4o-mini, but not opus" — a cost
and capability guardrail distinct from tool governance.

**Best practice:** restrict expensive models (opus, gpt-4o) to agents that
demonstrably need them; default everyone else to cheaper models. This pairs with
Metering and the Token Broker to control spend.

### 5.3 Quotas (per gateway) and Quotas (per agent) (`/quotas`, `/agent-quotas`)

**What it is:** rate limits and budget caps. Two granularities:

- **Per gateway** — "this whole gateway may do 1000 calls/min and spend $500
  before the next explicit reset."
- **Per agent** — "the payments-agent specifically may spend $50 before its
  accounting is reset."

When a quota is hit, the gate returns **429 (too many requests)** and the call is
refused — the same circuit-breaker mechanism that trips on failures.

**Novice framing:** a quota is a spending/traffic limit, like a prepaid phone
plan. Run out of minutes → calls stop. This protects you from a runaway agent
looping and burning $10k overnight.

**Best practice:** choose a per-agent budget for the reset process you actually
operate, and set a per-gateway cap as a backstop. Agent warning thresholds are
configurable; gateway alerts remain fixed at 80% / 90% / 100%.

**What to avoid:** setting quotas so tight that normal bursty traffic trips them.
A quota that cries wolf gets ignored (or raised until it's useless).

**Four operational details:**

1. **Agent quotas are control-plane records now.** **Save & Push** persists the
   record, rebuilds every agent limit for the selected gateway, and sends that
   complete map to `/config/agent-auth`. Existing tool grants are preserved.
   Gateway quotas still use their row's Push action and `/config/quota`.
2. **Budget periods are gateway-scoped.** The Models page stores a manual,
   daily, weekly, or monthly UTC schedule on the selected gateway. Its scheduler
   resets gateway and agent counters together, persists the reset epoch, and
   catches up after a boundary missed while offline. **Reset Now** starts a new
   period immediately.
3. **Threshold behavior differs by scope.** Gateway quotas use fixed
   80/90/100% thresholds. Each agent quota has a configurable warning threshold
   (plus 100%). Both are reported to `/api/quotas/alerts`; agent alerts retain
   `agent_id`.
4. **Quota definitions are durable SQL records with a bounded hot cache.**
   Tenant-scoped integer IDs are allocated atomically, so concurrent control-plane
   requests cannot collide. Displayed spend and trailing-minute RPM are recomputed
   from `usage_records`; all three LLM entry points report usage.

### 5.4 Protocol Governance — agent↔agent (`/protocol-governance`)

**What it is:** control over **one agent delegating a task to another agent**
(A2A — agent-to-agent). As agent systems grow, agents call *other agents*, not
just tools. This page governs those edges.

**Novice framing — why govern this separately?** Tool governance asks "may this
agent send an email?" Protocol governance asks "may the *research* agent hand a
task to the *payments* agent at all?" Without it, a low-trust agent could launder
its privileges by delegating to a high-privilege one. It's org-chart control for
agents.

This page has three parts:

1. **Connected A2A agents** — the real remote agents this gateway can delegate to
   (discovered via their "agent cards"), with their skills.
2. **Delegation matrix** — a grid: rows = callers, columns = callees, each cell
   allow / deny / default. Click to cycle. Plus a per-agent **trust score**.
3. **Blocked delegations feed** — which delegations governance stopped, and why.
4. **Behavior-derived trust** — trust scores *computed from each agent's actual
   risk/block history*, shown alongside the configured scores.

```
   research-agent ──wants to delegate──▶ payments-agent
                          │
                          ▼
        ┌─────────────────────────────────────────┐
        │ Delegation gate checks:                   │
        │  • Edge: is research→payments allowed?    │  (matrix)
        │  • Trust: is payments' score ≥ min_trust? │  (trust)
        │  • Depth: is the chain too deep? (A→B→C…)  │  (chain-depth guard)
        └─────────────────────────────────────────┘
                          │
              allow ──────┴────── block (403 + recorded)
```

**Trust scoring is shadow-first.** Behavior-derived trust is *computed and shown*
but **not enforced** until you explicitly opt in. You review what would change,
then enable it. This is deliberate: auto-enforcing a computed score could
silently break a working delegation.

**Best practice:** start with `default_allow` off and an explicit allow-matrix
for the delegations you expect — deny-by-default is safer than allow-by-default
for cross-agent. Set a sensible `min_trust` and a `max_chain_depth` (4 is a good
default) to stop runaway delegation loops. Review behavior-derived trust in
shadow before enabling.

**What to avoid:** allowing `*` (any agent may delegate to any agent). That
defeats the purpose — one compromised agent then reaches everything.

### 5.5 PII redaction & prompt-injection detection — gateway config only

**What it is:** two content controls that sit on the **LLM** path (not the tool
path) — redacting personal data out of a prompt before it leaves the process, and
scoring the prompt for prompt-injection attempts. They run on Ostiari's own
detection engine, `ostiari.detect`: 12 PII types, 7 injection categories, and
Unicode/zero-width obfuscation handling, with no external dependency.

**Why it's in this section but has no page:** these are governance rules, so they
belong beside policies and quotas conceptually — but **there is no control-plane
UI for them.** You set them in the gateway's YAML under `llm:`, and the control
plane neither displays nor pushes them:

```yaml
llm:
  pii_redaction: true
  pii_redact_types: [email, ssn, credit_card]   # omit for all types
  pii_reversible: true                          # false = unrecoverable
  injection_detection: true
  injection_threshold: 0.7                      # lower is stricter
  injection_mode: block                         # or "flag" to observe only
```

Both default to **off**. Full type tables and scoring in
[detection-engine.md](detection-engine.md).

**Two behaviors worth knowing before you turn `pii_redaction` on.** It does not do
the same thing on every entry point:

- On **`/invoke`**, where Ostiari owns the loop, messages really are redacted in
  place and the redacted set is what goes upstream (`pii_reversible: true` keeps a
  map so the response is restored on the way back).
- On the **`/v1/messages`, `/v1/chat/completions`, and `/v1/responses` shims**
  it acts as a
  *detector*: the proxies treat `pii_redacted` the same as `blocked` and return
  **403**. That's deliberate — those clients drive their own tool loops off the
  exact text they sent, and silently swapping in redacted content would
  desynchronize their conversation state. So on a shim, `pii_redaction: true`
  means "refuse prompts containing PII", not "clean them up."

Also note the `injection_mode: flag` escape hatch applies to injection only. There
is no flag mode for PII — enabling it on a shim blocks from the first match.

**Best practice:** start with `injection_mode: flag`. It scores and reports in the
response metadata without blocking — the same observe-before-enforce discipline as
shadow mode, applied to content instead of actions. Read what it flags on real
traffic, then switch to `block`.

**What to avoid — the failure mode that makes this worth its own warning:** these
controls are **fail-closed by design**. An enabled control that is unavailable or
that raises **blocks the request**. That is the correct posture, and it is also how
this feature was once completely broken: both detectors imported private AxonLLM
internals from a separate checkout, so a missing checkout made either switch
block **every** request. They now come from Ostiari's hard dependency, while the
separately bundled AxonLLM router handles routing. Keep the property in mind:
a broken detector means no traffic, not unchecked traffic.

---

## 6. MONETIZE — charge for usage (the revenue layer)

These two features turn governance into a business model. Both are built with an
**honesty principle:** the *measured* facts (counts, tokens) are real; the
*dollar assumptions* (prices, discounts) are operator-editable, so the numbers
are defensible, not magic.

### 6.1 Payments — x402 (`/payments`)

**What it is:** pay-per-tool-call using **x402**, an open standard where a tool
that costs money replies with HTTP **402 Payment Required**, and the caller pays
(in USDC, a stable digital dollar) before the call proceeds. Each agent has a
**wallet** with a balance and spending limits.

**Novice framing:** x402 is a toll booth for tool calls. A paid tool says "that'll
be half a cent." Ostiari pays it from the agent's wallet (if funded and within
limits) and lets the call through; if the wallet's empty, the call is blocked.
It's how an agent can pay for things autonomously *within limits you set*.

Three modes (per gateway):

- **off** — never charge (default).
- **metered** — Ostiari prices the call from a policy and charges before running.
- **passthrough** — the *tool* demands payment (returns 402); Ostiari pays from
  the wallet and retries. This is native x402.

```
   agent calls a paid tool
        │
        ▼
   tool returns 402 "pay $0.005"  ──▶  wallet has funds & within limits?
                                            │              │
                                     yes ───┘              └─── no
                                       │                        │
                              pay + retry → 200            blocked → 402
                              balance drops                tool never runs
```

Wallets **auto-pause** when a daily limit is hit (circuit-breaker style), and the
dashboard shows balances, a transaction ledger, and fees captured.

**Simulated vs. real (important for evaluators):** the demo runs a
**SimulatedSettler** — real wallet logic, real gate, real ledger, but no
blockchain. For live downstream payments, install `ostiari-gateway[payments]`,
set `OSTIARI_X402_MODE=live`, inject `OSTIARI_X402_PRIVATE_KEY` from a secret
manager, and use `passthrough`. The tool must speak x402 v2 using
`PAYMENT-REQUIRED` / `PAYMENT-RESPONSE`, the `exact` scheme, and an EVM network.
The official SDK signs the retry, while Ostiari pins the amount, payee, network,
scheme, and token contract and withholds tool output until settlement is
confirmed. Base mainnet and Base Sepolia USDC are allowed by default; additional
6-decimal USDC contracts require an explicit `OSTIARI_X402_ALLOWED_ASSETS`
mapping.

The Ostiari per-agent wallet is a policy allowance around the gateway payer, not
a private key store. An unconfirmed live attempt is shown separately from a
settled or blocked charge; its consumed allowance remains visible so a missing
response cannot silently restore spend. Payment reports retain a stable event
ID across retries, so the ledger and wallet mirror update once.

Live `metered` mode is deliberately rejected: Ostiari is the seller in that
flow, which requires a caller-supplied payment signature. Use simulated
`metered` pricing when monetizing governed calls, or live `passthrough` when
paying an external x402 resource. **What no code can create: the actual funded
wallet and provider relationships** — those are deployment and business setup.

**Best practice:** give each agent a per-call *and* daily limit. Start balances
small. Use passthrough for genuinely paywalled external tools; use metered when
*you* are monetizing governed calls.

**What to avoid:** provisioning wallets without limits. An agent with an
unlimited wallet and a loop is an unbounded bill. Always cap.

### 6.2 Token Broker (`/token-broker`)

**What it is:** buy LLM tokens in bulk at a negotiated discount, route customer
traffic through that pool, and charge a markup that's *still below* what the
customer would pay the provider directly. Everyone wins: customer saves vs. list,
you keep the spread.

**Novice framing — the three prices:**

```
   retail    = what the customer would pay the provider directly   $1.00
   our_cost  = retail × (1 − bulk_discount)     25% off  →          $0.75
   charged   = our_cost × (1 + markup)          +12%     →          $0.84
   ───────────────────────────────────────────────────────────────────
   customer saves  = retail − charged  = $0.16   (16% below list)
   our margin      = charged − our_cost = $0.09
```

The invariant the UI enforces: as long as `(1−discount)×(1+markup) < 1`, the
customer saves *and* you profit. The page warns if your markup eats the discount.

**The pilot layer (for a real deployment):**

- **Token pools** — purchased inventory per provider, drawn down as calls consume
  tokens, with a low-balance state that gateways use to reroute or halt calls
  before another request reaches a depleted provider.
- **Retry-safe charging** — every gateway usage event has a stable ID. Usage,
  pool drawdown, and the customer charge are applied once even when a gateway
  retries after a timeout or billing failure.
- **Stripe Billing** — `OSTIARI_BROKER_BILLING=live` emits one Meter Event per
  usage event. Values are integer micro-USD and the stable gateway identity is
  also the Stripe idempotency identity. Configure `STRIPE_API_KEY`,
  `STRIPE_METER_EVENT_NAME`, and either `STRIPE_CUSTOMER_ID` or a JSON
  `STRIPE_CUSTOMER_MAP`; the Stripe meter must treat 1,000,000 units as $1.00.
- **Reconciliation** — compare *our computed* consumption cost against the
  *provider's actual invoice* each period, and flag the drift. This is the part
  that keeps a broker from silently losing money.

```
   You buy 10M tokens (anthropic) → pool: 10M remaining
        │
   agents make claude calls → pool draws down → 8M remaining
        │
   month end: our computed cost vs provider invoice → drift?  ← catch leaks here
```

**Best practice:** set a low-balance threshold well above zero so you top up
before the pool depletes and routing halts. Preserve the gateway-generated event
ID on every delivery retry; changing it creates a new billable event. Reconcile
every billing period — drift over a few percent means your token-counting or the
provider's billing disagree, and you want to know *before* it compounds.

**What to avoid:** treating the discount as real without a signed volume
agreement behind it. The dashboard computes margin from *assumed* terms; the
money only appears if the bulk contract actually exists.

---

## 7. OBSERVE — watch the whole fleet

Everything above configures the gates. This section shows what the gates did.

### 7.1 Dashboard (`/dashboard`)
Fleet overview — gateway health, recent activity, headline numbers. Your home
screen.

### 7.2 Live Traces (`/traces`)
A real-time stream of every tool call across all gateways: which agent, which
tool, allow/intervene/block, risk score, duration. Streams over WebSocket.
**Novice framing:** the security camera feed. Grouped multi-step "sessions" let
you follow one agent's plan end to end.

### 7.3 Shadow Report (`/shadow-report`)
The "try before you enforce" summary: for a gateway in shadow mode, what enforce
mode *would* have blocked — counts, block rate, and the offending actions.
**This is what you read before flipping a gateway to enforce.**

### 7.4 Approvals (`/approvals`) — where a human answers the *intervene* tier

**What it is:** the review queue. Every other page in this guide describes rules
being applied automatically; this is the one place a person is in the loop. When a
call scores into the **intervene** band (§5.1), the gateway pauses it and files a
pending approval here. The reviewer sees the agent, the action, the risk score,
the *why* (the decision explanation), and the call's **raw parameters** — the
actual SQL, the actual recipient list — then clicks **Approve** or **Deny**. A
second panel, *Recent decisions*, shows the last 20 with who decided each.

**Novice framing:** the airport analogy's secondary-screening desk. Most
passengers walk through; a few get pulled aside, and a human looks in the bag and
decides. Without this page the *intervene* tier is just a label on a chart — this
is where it becomes an actual decision.

**The mechanics (gate 5 in §2), because they surprise people:**

```
   agent calls a tool → risk score lands in "intervene"
        │
        ├─ OSTIARI_HITL off (the default) ─▶ dev:  recorded as intervene, CALL PROCEEDS
        │                                    prod: 403, call refused
        │
        └─ OSTIARI_HITL on   (dev and production alike)
                │
                ▼
           gateway returns HTTP 202  { "pending_approval": true, "approval_id": …,
                                       "reason": …, "decision": {…} }
           the tool has NOT run
                │
           a human approves/denies on this page
                │
           caller re-submits the SAME request with header  X-Approval-Id: <id>
                │
                ├─ approved ─▶ tool executes
                └─ denied   ─▶ 403, tool never runs
```

Four consequences of that shape:

1. **HITL is opt-in.** The gateway must run with `OSTIARI_HITL=on` (`1`/`true`/`yes`
   also work). Off — the default — an intervene score is scored, explained, and
   traced, but nothing pauses and this queue stays empty. If you expected approvals
   and see none, check this first.
2. **The gateway does not block a thread waiting.** It answers **202** immediately
   and the *caller* is responsible for retrying with `X-Approval-Id` once the
   approval clears. An agent framework that treats any non-2xx-body as a failure
   will look like it "lost" the call — the 202 body carries the id and the reason
   text explaining the resubmit.
3. **Approval is per-call, not a standing grant.** The id authorizes that one
   request. The next identical call scores again and pauses again.
4. **The gate reads the *raw* tier.** The Guard can internally collapse an
   intervene into allow or block (fail-open, a policy callback, or a fail-closed
   raise), but the tier as *scored* is preserved and that's what HITL acts on. So a
   call can appear as `allow` in one view and still land in this queue —
   deliberately, because "ask a human" shouldn't be silently downgraded by a
   fail-open path.

**What HITL off means differs by environment,** and this is the one place the
fail-open default is load-bearing. Dev is fail-open: an unresolved intervene
becomes an *allow* and the call proceeds. Production is fail-closed (§9 / `OSTIARI_ENV`):
the same call is **refused with 403**. Same score, same policy, opposite outcome —
so a threshold tuned in dev, where the intervene band is effectively "log it,"
becomes a wall of refusals on the day you set `OSTIARI_ENV=production`. Either turn
HITL on so the band has somewhere to go, or move those actions out of the intervene
band before you promote.

With HITL **on**, production and the approvals queue compose: a fail-closed
intervene is deferred to this queue rather than refused, so the 202 →
`X-Approval-Id` loop above is the production path too. Three properties hold, and
are worth re-checking after any change to the enforcement path — they're the line
between an escalation and a bypass:

- A **genuine** block still 403s and creates **no** approval. A policy `block:` or a
  score over the block threshold is a decision, not a question; it has no business
  in a review queue.
- With HITL **off** in production, a fail-closed intervene stays a 403. There is
  nobody to defer to, so nothing is deferred.
- **Shadow mode** never queues. It doesn't enforce, and its short-circuit runs ahead
  of this gate: an intervene there is an observation to record, not a call to pause.

Before the fix that made this work, production silently deleted the middle tier:
the fail-closed collapse raised before the approval gate was reachable, so every
scored-intervene call 403'd and this page stayed empty no matter what
`OSTIARI_HITL` said. If you are running an older gateway, that's the symptom.

**Best practice:** turn HITL on only once your thresholds put *few* calls in the
intervene band (§5.1's "over-blocking" warning applies double here — a queue
nobody can keep up with gets rubber-stamped, which is worse than not having it).
Make sure whoever staffs the queue can actually judge the parameters they're
shown; if the reviewer can't read the SQL, the review is theater.

**What to avoid:** exposing this queue across tenants. It holds raw tool
parameters — the most sensitive payload in the system — plus the power to approve
them. A flat, un-scoped store put one tenant's SQL in every other tenant's queue
and let anyone decide it. Fixed, and covered in §10a; worth re-checking after any
change to approval storage.

### 7.5 Costs (`/costs`) and Metering (`/metering`)
- **Costs** — dollar spend by model/agent/gateway (LLM token costs).
- **Metering** — *governed-call counts* per agent/gateway/tool, with tiering
  (free / pro / enterprise). This is the billing lens: "Team X made 47,000
  governed calls this month." Metering counts; Costs prices.

### 7.6 Audit Log (`/audit`)
An immutable record of *administrative* actions (who changed which policy, when).
Distinct from traces (which record agent behavior). This is your compliance and
"who did that?" trail.

### 7.7 Compliance (`/compliance`)
Auto-generated regulator-shaped reports (EU AI Act first) mapping Ostiari's
evidence — audit logs, traces, policies, human-oversight interventions — to
specific requirements, scored green/yellow/red. One command → auditor-ready PDF.

### 7.8 ROI / Savings (`/roi`)
"We blocked N unsafe actions worth ~$X in prevented damage." **Honesty model:**
the *counts and risk scores are measured*; the *dollar value per blocked action
is your editable assumption*, risk-weighted so a barely-over-threshold block
counts less than a max-risk one. Edit the cost model and the number recomputes —
because a defensible ROI figure is one the CIO's own assumptions produced.

**Best practice for ROI:** set the incident costs to *your* org's real risk
figures before showing anyone. A borrowed default ("$500k per DB delete") invites
"where'd that come from?"; your own number survives the question.

### 7.9 Token Efficiency (`/efficiency`) — routed, but not in the nav

**What it is:** token usage, cost optimization, and prompt-quality insights. An
**Overall Score** plus Avg Tokens/Request, Cost/Request, and Models Used, broken
down into Token Efficiency / Cost Efficiency / Routing Diversity bars. It answers
"are we spending tokens well?", where Costs (§7.5) answers "what did we spend?"

**Read this before you use it:** the page is routed in the app but is **not listed
in `NAV_SECTIONS`** — there is no link to it. You reach it by typing
`/efficiency` in the address bar. So it is real and it works, but nobody will
discover it on their own, and its numbers aren't part of anyone's routine. Either
add it to the nav or treat it as a diagnostic you go to deliberately; don't assume
a teammate has seen it.

---

## 8. TEST — verify before you trust

### 8.1 Sandbox (`/sandbox`)
Fire tool calls at a gateway by hand and watch the decision. Pre-built scenarios
exercise the guard (a blocked destructive call), a multi-step plan, and MCP/A2A
calls. **Use this to test policy patterns before enforcing them.**

### 8.2 A/B Tests (`/experiments`)
Route a percentage of traffic to a challenger model and compare. Cost/quality
experimentation (e.g. "send 30% to haiku, keep 70% on sonnet, compare").

### 8.3 Architecture (`/architecture`)
A visual of the deployed topology — gateways, agents, connections. Good for
onboarding and for the "how does this fit together?" conversation.

---

## 9. ADMIN

### 9.1 LLM Providers (`/providers`)
The model providers AxonLLM (the embedded routing engine) can reach —
Anthropic, OpenAI, Google, xAI, Together, Groq, Fireworks, Bedrock, etc. — and
their credentials/health. **Novice note:** "LLM Providers" (model vendors) are
different from the agent *frameworks* on the Agents page. Providers supply the
brains (models); frameworks are how the agent is built.

**xAI (Grok) and Together** are first-class here: both speak the OpenAI wire format,
so connectivity is one shared `/v1/chat/completions` probe rather than a near-copy per
vendor. Their base URLs and probe models mirror AxonLLM's adapters deliberately — a
divergence would let this page "pass" a key the router can't actually route with.
Seeded models: `grok-3`, `grok-3-mini`, `llama-3.3-70b`, `deepseek-r1-together`.

Provider metadata and encrypted credentials are tenant-scoped SQL records.
`gateway/register_demo_providers.py` seeds or updates those records from the
same environment file the demo gateways load. Production startup requires a
stable `OSTIARI_ENCRYPTION_KEY`; API responses expose presence flags, never the
decrypted value.

### 9.2 Users (`/users`)

**What it is:** control-plane accounts and their roles. Three roles:

| Role | Intent |
|---|---|
| **admin** | everything, including this page and LLM Providers |
| **operator** — labelled **Editor** in the UI | change config; no user/provider admin |
| **viewer** | read-only |

`rbac.ROLES` spells out the intended permission sets (`admin` = operator's list
plus `users:*`; `viewer` = the `:read` half). Note that `check_permission` is
**only called from tests** — nothing in the request path consults it, so the table
above is design intent, not enforcement. What's actually enforced is below.

**Naming caveat, because it will confuse you:** the canonical value is `operator`.
The backend's `_VALID_ROLES` is `admin`/`operator`/`viewer`, `rbac.ROLES` has
entries for exactly those three, and the SSO group mapper accepts `operators`,
`operator_group`, or `editor` and normalizes them all to `operator`. The **UI**
labels that same role **Editor** — the dropdown on this page and the sidebar badge
both say Editor.

**But the two names are not reconciled on the local-user path.** `POST
/api/auth/register` takes `role` as a plain unvalidated string (`UserCreate.role:
str = "viewer"`, no validator, no enum), and this page's dropdown submits the
literal `editor`. So creating an "Editor" here stores `role="editor"`, which is
**not** in `_VALID_ROLES` and not a key in `rbac.ROLES`. That user gets:

- the sidebar's write sections (the `viewer` hide-list is checked by name, and
  `editor` isn't `viewer`), so the UI *looks* like an operator;
- `check_permission("editor", …)` → **False** for everything, because the role has
  no permission list;
- `require_role("admin")` → 403, correctly;
- the un-role-checked write routers → **200**, same as everyone else (see below).

The normalization only exists on the SSO path (`sso.py:332`), so an SSO-provisioned
operator is stored correctly and a locally-created "Editor" is not. If you create
users locally and care about the distinction, `POST` `"role": "operator"` against
the API directly rather than using the dropdown.

**What RBAC actually enforces today — read this before relying on it.** Role
restriction is mostly a *frontend* affordance:

- The sidebar hides the write sections (`Control`, `Configure`, `Test`) from a
  viewer, and hides `Admin` from everyone but an admin. `/providers` and `/users`
  are additionally wrapped in a `RequireAdmin` route guard.
- **Server-side**, only three surfaces check the role, and they're the ones you'd
  guess: provider *writes* and the key-reveal endpoint (`require_role("admin")` —
  the key-free provider *list* is readable by anyone authenticated), the
  user-management endpoints, and the Audit Log via its own inline admin/operator
  check — **which is itself conditional on `OSTIARI_REQUIRE_AUTH`**.
  `_require_audit_reader` returns early when that variable is unset, so on a
  default dev control plane a viewer reads the audit log with a 200. Only the
  provider and user-management checks hold unconditionally.
- Everything else is **not role-checked**. The write routers for policies, quotas,
  tools, gateways, agents, MCP servers, payments, and the token broker
  authenticate the caller and scope them to an org, but never look at the role. A
  viewer token `POST`ing to `/api/policies` gets **200** and the policy persists.

Verified, not inferred — probing the live API with a genuine viewer token, with
`OSTIARI_REQUIRE_AUTH=1` (the stricter of the two postures):

| Request as a viewer | Result |
|---|---|
| `POST /api/policies` | **200**, policy created and persisted |
| `POST /api/gateways` | **200**, gateway created |
| `POST /api/providers`, `DELETE /api/providers/{n}` | 403 |
| `GET /api/auth/users`, `POST /api/auth/register` | 403 |
| `GET /api/audit` | 403 — but **200** with `OSTIARI_REQUIRE_AUTH` unset |

Treat viewer as "this person won't be shown the controls", not as "this person
cannot change anything." If you need the stronger guarantee, the role checks
belong on the write routers, not the nav — `rbac.check_permission` already
encodes the right answers and is wired to nothing, so the gap is plumbing rather
than design.

**The default admin credential.** In dev/demo the control plane seeds
`admin@ostiari.ai` / `admin` on first login for convenience, and the login form
prefills it so the demo is one click to sign in. That prefill is gated on
`DEMO_LOGIN` (on under `vite dev`, or `VITE_DEMO_LOGIN=true` for a deployed
demo) precisely because the credential is dev-only — in production it would
advertise a password that cannot work. In production
(`OSTIARI_ENV=production`) it **refuses to seed at all** without an explicit
password and raises `RuntimeError: OSTIARI_ADMIN_PASSWORD must be set in
production` — so the well-known credential can't reach a real deployment by
oversight. Set `OSTIARI_ADMIN_PASSWORD` (and optionally `OSTIARI_ADMIN_EMAIL`)
before first boot.

**Best practice:** most people are viewers; a small number are operators/editors;
admin is rare. Keep the production admin password in a secret store, not an env
file on the box.

---

## 10. How config actually reaches a gateway (the push model)

A recurring source of confusion, made explicit:

```
   You edit a policy/quota/price in the control plane
        │
        ▼
   Click "Push" (or it pushes on gateway registration)
        │
        ▼
   Control plane bundles config → POST to the gateway's /config
        │
        ▼
   Gateway hot-reloads: new rules apply to the NEXT call
```

- **Tools, policies, quotas, agent-auth, payment config** all ride the config
  push and hot-reload — no gateway restart.
- **MCP servers and A2A agents are the exception:** they connect at gateway
  *startup* or via a dedicated connect call (they spawn processes / open
  sockets), so they aren't part of the routine push. That's why there are
  separate `register_demo_mcp.py` / `register_demo_a2a.py` steps.

**What to avoid:** editing config and forgetting to push. The dashboard shows the
new value, but the gateway is still enforcing the old one until you push. If a
change "isn't taking effect," check that you pushed.

### Partial pushes still have a hole — use the gate-specific route

Two of those bullets are truer of the *registration/heartbeat* path than of the
Push button, because they arrive by different routes:

- **Gateway registration and heartbeat** deliver a bundle that the gateway applies
  **key by key**, configuring each gate explicitly. Everything in the list above
  really does hot-reload this way.
- **Generic `push-config` callers** post to the gateway's `POST /config`, which
  replaces the whole config document and applies **only tools and policy**.
  `provider_routes` is reserved for the encrypted provider-route API and is
  rejected here.
- **Quota pages** use `/config/quota` and `/config/agent-auth`, so their limits
  reach the runtime enforcers directly.

Concretely, today:

| Page → Push | What actually happens |
|---|---|
| **Gateways** (Push / Push All) | Tools + policy applied. Fine. |
| **Policies** | Policy applied — but the gateway's registered **tools are cleared** and `mode` resets to `enforce`, un-shadowing a shadow gateway. |
| **Quotas** | Applied through `POST /api/quotas/{id}/push` → `/config/quota`. |
| **Agent Quotas** | Save & Push rebuilds the complete gateway map and applies it through `/config/agent-auth`; Push All also clears deleted limits. |

So: after pushing a quota, **verify against the gateway's `GET /config/quota`**,
not the dashboard and not `GET /config`. Verify agent limits with
`GET /config/agent-auth`. The pages call the working gate-specific routes.

Mechanism and reproduction in
[gateway-architecture.md](gateway-architecture.md#the-config-partial-push-trap).

---

## 10a. Tenant scoping — which org sees what

Every stored record carries an `org_id`, and read endpoints are scoped to the
caller's org. Two things make this non-obvious, and both have bitten:

**1. Gateways have no user token.** A gateway posting traces, usage, payments, or
approvals authenticates as itself, not as a person — so there is no caller org to
scope by. The org is derived from the **reporting gateway's row** (`org_of_gateway`),
which is the only trustworthy source available on those paths.

**2. The payload is not believed.** An ingest body naming its own `org_id` was
previously honored, which let any caller that could reach `/api/traces/ingest` file a
trace into an arbitrary tenant's buffer. That buffer is read back by `/recent`, the
WebSocket fan-out, compliance, ROI, trust scoring, and discovery. Any `org_id` in the
body is now overwritten with the gateway-derived value before storage.

| Surface | How the org is decided |
|---|---|
| Trace / usage / payment / approval **ingest** | the reporting gateway's `gateways` row |
| Everything a **human** reads | the caller's token (`get_current_org`) |
| Approvals addressed **by id** | owner org of that approval; a tokened caller from another org gets 404 |

An unknown or empty gateway falls back to the default org, so its records are still
kept rather than silently dropped — the demo posture, consistent across ingest paths.

**3. Who may ingest at all.** Deriving the org from the gateway row only helps if the
caller *is* that gateway. Production machine APIs therefore use a dedicated workload
OIDC trust boundary, separate from browser/user OIDC:

- each gateway uses either a projected short-lived token file or its own OAuth 2.0
  client credentials;
- the control plane validates the dedicated issuer, signature, expiry, and audience;
- first registration binds the verified issuer/subject pair to exactly one gateway;
- every later lifecycle, config, approval, trace, cost, payment, quota-alert, and
  spend request checks that binding and the gateway id in its path or body;
- a configurable gateway-id claim is enforced when present, while standard OAuth
  client-credentials tokens may rely on the immutable issuer/subject binding;
- issuer/JWKS outages fail closed with a retryable **503**, rather than falling back
  to a shared key.

Production startup rejects `OSTIARI_SERVICE_TOKEN` and `OSTIARI_INGEST_KEY`. They
remain only for the local development stack. Configure the control plane with
`OSTIARI_WORKLOAD_OIDC_ISSUER` and `OSTIARI_WORKLOAD_OIDC_AUDIENCE`, then give each
gateway its own projected token or OAuth client. Reusing one OAuth subject for two
gateway ids is rejected.

**Approvals are the subtle one.** The queue holds an agent's raw tool parameters —
SQL, recipients, payloads — plus the reviewer's identity. A flat id-keyed store put
one tenant's most sensitive call detail in every other tenant's review queue, and let
anyone decide it. It's now keyed per org. The id-addressed routes stay reachable
without a token because that's the gateway's own resume-check path; a caller that
*does* present a token is held to its own org.

**Discovery, too:** "shadow AI" is computed as seen-minus-known, so an unscoped read
listed another tenant's agent ids and gateway names as *your* shadow AI.

---

## 11. Golden-path operating checklist

For a real deployment, in order:

1. Set the production secrets **before first boot**: `OSTIARI_ADMIN_PASSWORD`
   (the control plane refuses to seed an admin without it), `OSTIARI_JWT_SECRET`,
   `OSTIARI_ENCRYPTION_KEY`, and workload OIDC settings — reading §10a first, because
   the gateway does not yet send the ingest header, so setting the key silences
   Live Traces rather than authenticating it.
2. **Register** gateways, agents, tools, MCP servers (Configure).
3. Start every gateway in **shadow** mode.
4. Write **policies** (deny-by-default for destructive; explicit allow for safe;
   `risk_adjust` for grey areas). Test patterns in the **Sandbox**.
5. Set **quotas** (per-agent budgets aligned to your reset process; a gateway backstop).
6. For agent-to-agent systems, set the **delegation matrix** deny-by-default and
   a `max_chain_depth`.
7. If you want content controls, enable detection with `injection_mode: flag`
   first (§5.5), read what it flags, then switch to `block`.
8. Let real traffic flow. Watch **Live Traces** and the **Shadow Report**.
9. When the shadow report shows only true positives, flip that gateway to
   **enforce**.
10. Check **Discovery** for shadow AI; onboard or shut down every ungoverned agent.
11. Only once *intervene* is rare: turn on `OSTIARI_HITL` and staff the
    **Approvals** queue. In production this is not really optional — with it off,
    fail-closed turns every intervene into a 403 (§7.4). Either staff the queue or
    make sure nothing scores into the band.
12. If monetizing: provision **wallets** with limits; set token **pool**
    thresholds; **reconcile** each period.
13. Review **ROI**, **Metering**, **Compliance** monthly. Keep the cost
    assumptions honest.
14. Make most users **viewers** — and remember viewer is a UI restriction, not a
    server-side one (§9.2).

## 12. Top pitfalls, collected

| Pitfall | Why it bites | Fix |
|---|---|---|
| Enforce on day one | Blocks real work, kills trust | Shadow first, read the report |
| `*.delete` to block `db_delete` | Glob needs the dot; won't match | Use `*delete*` or explicit name |
| Empty `block: []` in a 2nd policy | Clobbers a good block list on merge | Never ship empty block lists |
| Tools pointing at dead endpoints | Look healthy, do nothing | Verify calls execute, not just list |
| Edited config, didn't push | Gateway still on old rules | Always push after editing |
| Pushed a quota from the Quotas page | Stored and displayed, never enforced | Verify `GET /config/quota`; push via the API (§10) |
| Pushing a policy to a shadow gateway | Clears its tools and resets it to `enforce` | Re-push tools and re-set the mode after (§10) |
| Unlimited wallets | Runaway agent = unbounded bill | Per-call + daily limits, always |
| `default_allow` on for A2A | One agent reaches everything | Deny-by-default + explicit matrix |
| Borrowed ROI cost numbers | "Where'd that come from?" | Use your org's real risk figures |
| Broker discount with no contract | Margin is imaginary | Discount must be a signed agreement |
| Never reconciling the token pool | Silent drift compounds | Reconcile every billing period |
| Expecting approvals with HITL off | Intervene never pauses — and in production it's *refused* | `OSTIARI_HITL=on` (§7.4) |
| Thresholds tuned in dev, promoted to prod | Dev's intervene band is "log it"; prod's is "refuse" | Turn HITL on, or empty the band first (§7.4) |
| Caller ignores the 202 | Approved call never re-submitted, looks lost | Retry with `X-Approval-Id` |
| Assigning a write-capable role to a read-only user | Operator/admin tokens can mutate governance | Keep read-only users on `viewer`; backend RBAC rejects viewer writes (§9.2) |
| Enabling detection straight to `block` | Fail-closed + untuned = blocked traffic | `injection_mode: flag` first (§5.5) |
| Missing workload OIDC configuration | Lifecycle, cost, approval, payment, and alert ingest fail closed | Configure the dedicated issuer/audience and one projected token or OAuth client per gateway |
| Reusing one workload subject across gateway ids | One machine identity could impersonate multiple gateways | The control plane rejects the second binding; provision a distinct client or subject |

---

## 13. One-paragraph summary for a busy reader

Ostiari puts a **gateway** in front of every AI agent that intercepts each tool
call and runs it through a chain of gates — delegation, authorization, quota,
risk-scoring, human approval, payment — before letting it execute, then records
everything. The **control plane** is the central dashboard where you configure
those gates once and push them to every gateway, and where you watch the whole
fleet: live traces, shadow reports, the approvals queue, metering, compliance,
ROI. Start every gateway in **shadow** mode, tune your policies against real
traffic, then flip to **enforce** — that discipline, plus deny-by-default on
destructive actions and cross-agent delegation, is 90% of operating it well. Two
things are opt-in and easy to assume you have: human-in-the-loop approvals
(`OSTIARI_HITL`) and content detection (`pii_redaction` /
`injection_detection`) are both **off by default**.
