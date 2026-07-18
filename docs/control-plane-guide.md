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
   │  5. PAYMENT      Does this call cost money? Can the wallet pay?  │  ← Payments (x402)
   │        │                                                         │
   │        ▼                                                         │
   │  6. EXECUTE      Forward to the real tool (HTTP / MCP / agent).  │
   │        │          Meter usage; draw down token pool.             │  ← Metering, Token Broker
   │        ▼                                                         │
   │  7. TRACE        Record everything → report to control plane.    │  ← Live Traces, Audit, ROI
   └────────────────────────────────────────────────────────────────┘
                │
                ▼
   Result returns to the agent (or a "blocked" / "needs approval" response)
```

Two cross-cutting ideas that ride on top of this chain:

- **Shadow mode.** A gateway can run in *shadow* instead of *enforce*. In shadow,
  every gate still evaluates and records what it *would* have done — but nothing
  is ever blocked and no real side effect runs. It's "try before you enforce."
- **Everything is configured centrally.** You never edit a gateway directly. You
  change a policy / quota / price in the control plane and click **Push**; the
  control plane sends the new config to the gateway(s).

Keep this diagram in mind. Every control-plane page below is either **setting up
one of these gates** (Configure / Control / Monetize) or **watching what the
gates did** (Observe / Test).

---

## 3. The control plane, section by section

The dashboard's left nav has six sections. They map to a natural lifecycle:

```
  CONFIGURE ─▶ CONTROL ─▶ MONETIZE ─▶ (agents run) ─▶ OBSERVE ─▶ TEST/tune
  (register)   (rules)    (charging)                  (watch)    (verify)
```

1. **Configure** — register your gateways, agents, tools, and MCP servers.
2. **Control** — set the rules: model access, policies, quotas, agent-to-agent.
3. **Monetize** — charge for tool calls (x402) and broker LLM tokens.
4. **Observe** — watch it happen: traces, costs, metering, compliance, ROI.
5. **Test** — sandbox, A/B experiments, architecture view.
6. **Admin** — LLM providers and users.

We'll take each in the order you'd actually use them.

---

## 4. CONFIGURE — tell Ostiari what exists

Before Ostiari can govern anything, it needs to know your world: which gateways
are out there, which agents run behind them, what tools they can call.

### 4.1 Agent Gateways (`/gateways`)

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

### 4.2 Agents (`/agents`)

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
what you don't know exists.

### 4.3 Tools (`/tools`)

**What it is:** the tools each gateway can proxy — name, HTTP endpoint, method.
When an agent calls `db_query`, the gateway looks up that tool here to know where
to forward it.

**What to avoid:** pointing tools at endpoints the gateway can't actually reach.
A tool that lists but 502s on call looks "configured" but does nothing — worse
than not having it, because it *looks* healthy. (This was a real bug we fixed:
demo gateways had tools pointing at dead endpoints.)

### 4.4 MCP Servers (`/mcp-servers`)

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
  block — let them go to *intervene* (human approval) instead of failing.
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

- **Per gateway** — "this whole gateway may do 1000 calls/min and spend
  $500/day."
- **Per agent** — "the payments-agent specifically may spend $50/day."

When a quota is hit, the gate returns **429 (too many requests)** and the call is
refused — the same circuit-breaker mechanism that trips on failures.

**Novice framing:** a quota is a spending/traffic limit, like a prepaid phone
plan. Run out of minutes → calls stop until the period resets. This protects you
from a runaway agent looping and burning $10k overnight.

**Best practice:** set per-agent daily budgets slightly above normal usage so
they only trip on genuine anomalies. Set a *global* per-gateway cap as a
backstop. Alerts fire at 80% / 90% / 100% — watch the 80% alerts, they're your
early warning.

**What to avoid:** setting quotas so tight that normal bursty traffic trips them.
A quota that cries wolf gets ignored (or raised until it's useless).

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
blockchain. Going live is one config flip (`OSTIARI_X402_MODE=live`) plus a
funded on-chain wallet and a settlement "facilitator." The governance/UX is
production code; only the on-chain settlement is swapped in. **What no code can
create: the actual funded wallets and provider relationships** — those are
business setup.

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
  tokens, with a low-balance alert that halts routing on depletion.
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
before the pool depletes and routing halts. Reconcile every billing period —
drift over a few percent means your token-counting or the provider's billing
disagree, and you want to know *before* it compounds.

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

### 7.4 Costs (`/costs`) and Metering (`/metering`)
- **Costs** — dollar spend by model/agent/gateway (LLM token costs).
- **Metering** — *governed-call counts* per agent/gateway/tool, with tiering
  (free / pro / enterprise). This is the billing lens: "Team X made 47,000
  governed calls this month." Metering counts; Costs prices.

### 7.5 Audit Log (`/audit`)
An immutable record of *administrative* actions (who changed which policy, when).
Distinct from traces (which record agent behavior). This is your compliance and
"who did that?" trail.

### 7.6 Compliance (`/compliance`)
Auto-generated regulator-shaped reports (EU AI Act first) mapping Ostiari's
evidence — audit logs, traces, policies, human-oversight interventions — to
specific requirements, scored green/yellow/red. One command → auditor-ready PDF.

### 7.7 ROI / Savings (`/roi`)
"We blocked N unsafe actions worth ~$X in prevented damage." **Honesty model:**
the *counts and risk scores are measured*; the *dollar value per blocked action
is your editable assumption*, risk-weighted so a barely-over-threshold block
counts less than a max-risk one. Edit the cost model and the number recomputes —
because a defensible ROI figure is one the CIO's own assumptions produced.

**Best practice for ROI:** set the incident costs to *your* org's real risk
figures before showing anyone. A borrowed default ("$500k per DB delete") invites
"where'd that come from?"; your own number survives the question.

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

### 9.2 Users (`/users`)
Control-plane accounts and roles (admin / operator / viewer). RBAC: viewers can
watch but not change config. **Best practice:** most people are viewers; a small
number are operators; admin is rare. The default `admin@ostiari.ai` / `admin`
login is for first-run only — **change it immediately** in any real deployment.

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

---

## 11. Golden-path operating checklist

For a real deployment, in order:

1. **Register** gateways, agents, tools, MCP servers (Configure).
2. Start every gateway in **shadow** mode.
3. Write **policies** (deny-by-default for destructive; explicit allow for safe;
   `risk_adjust` for grey areas). Test patterns in the **Sandbox**.
4. Set **quotas** (per-agent daily budgets a bit above normal; a global backstop).
5. For agent-to-agent systems, set the **delegation matrix** deny-by-default and
   a `max_chain_depth`.
6. Let real traffic flow. Watch **Live Traces** and the **Shadow Report**.
7. When the shadow report shows only true positives, flip that gateway to
   **enforce**.
8. If monetizing: provision **wallets** with limits; set token **pool**
   thresholds; **reconcile** each period.
9. Review **ROI**, **Metering**, **Compliance** monthly. Keep the cost
   assumptions honest.
10. Rotate the default admin credential; make most users **viewers**.

## 12. Top pitfalls, collected

| Pitfall | Why it bites | Fix |
|---|---|---|
| Enforce on day one | Blocks real work, kills trust | Shadow first, read the report |
| `*.delete` to block `db_delete` | Glob needs the dot; won't match | Use `*delete*` or explicit name |
| Empty `block: []` in a 2nd policy | Clobbers a good block list on merge | Never ship empty block lists |
| Tools pointing at dead endpoints | Look healthy, do nothing | Verify calls execute, not just list |
| Edited config, didn't push | Gateway still on old rules | Always push after editing |
| Unlimited wallets | Runaway agent = unbounded bill | Per-call + daily limits, always |
| `default_allow` on for A2A | One agent reaches everything | Deny-by-default + explicit matrix |
| Borrowed ROI cost numbers | "Where'd that come from?" | Use your org's real risk figures |
| Broker discount with no contract | Margin is imaginary | Discount must be a signed agreement |
| Never reconciling the token pool | Silent drift compounds | Reconcile every billing period |

---

## 13. One-paragraph summary for a busy reader

Ostiari puts a **gateway** in front of every AI agent that intercepts each tool
call and runs it through a chain of gates — delegation, authorization, quota,
risk-scoring, payment — before letting it execute, then records everything. The
**control plane** is the central dashboard where you configure those gates once
and push them to every gateway, and where you watch the whole fleet: live
traces, shadow reports, metering, compliance, ROI. Start every gateway in
**shadow** mode, tune your policies against real traffic, then flip to
**enforce** — that discipline, plus deny-by-default on destructive actions and
cross-agent delegation, is 90% of operating it well.
