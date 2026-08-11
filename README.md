# Ostiari

**The runtime governance layer for AI agents.** Ostiari sits in front of every
agent, intercepts each tool call, scores its risk, enforces your policies, and
records everything — across any framework, from one central control plane.

[![CI](https://github.com/hk-775/ostiari/actions/workflows/ci.yml/badge.svg)](https://github.com/hk-775/ostiari/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

---

An AI agent that can send email can leak data; one that can run SQL can drop a
table; one that can pay for APIs can run up a bill. That power is the point of
agents — and the risk. **Ostiari is the guardrail between your agents and the
outside world.** Every tool call runs through a chain of gates — authorization,
quota, risk-scoring, payment — before it executes, and you configure and watch
all of it from one dashboard.

```
   Agent ──▶ Gateway (enforces) ──▶ Tool / MCP server / another agent
                  │
                  └──▶ Control Plane (configure once, watch everything)
```

## See it in 90 seconds

```bash
git clone https://github.com/hk-775/ostiari.git
cd ostiari
make install       # Python deps + gateway + frontend
make demo-full     # control plane + 4 gateways + demo tools, all seeded
```

Open **http://localhost:9000** and click **Sign in** — the demo admin
(`admin@ostiari.ai` / `admin`) is prefilled. You'll land on a fully-populated
dashboard: four agent gateways governing real tool calls, live traces streaming,
blocked destructive actions, MCP servers, agent-to-agent delegation, quotas with
live budget bars, a human-approval queue, payments, and ROI — no setup, no mock
data.

> Everything in the demo is **real**: the gateways actually proxy and govern
> live tool calls (a `db_delete` really gets blocked with a 403), the MCP
> servers really run (`npx` draw.io + filesystem), and one agent really
> delegates to another. The demo deliberately keeps external money movement
> simulated; production deployments can opt into x402 v2 settlement and Stripe
> Billing without changing the governance flow.

**New here?** Read [`docs/control-plane-guide.md`](docs/control-plane-guide.md)
— a complete, novice-friendly tour of the control plane with architecture
diagrams, best practices, and pitfalls.

## The three layers

| | What it is | Where |
|---|---|---|
| **Gateway** (sidecar) | A proxy that runs next to each agent and enforces the rules in the request path. Fast, local. | `gateway/` |
| **Control Plane** | The central brain + dashboard: configure policies once and push them to every gateway; watch the whole fleet. | `control-plane/` |
| **Guard** (library) | The embeddable risk engine — use it directly in Python without the gateway. | `src/ostiari/` |

For a complete inventory of implemented capabilities and their request,
configuration, approval, telemetry, and deployment flows, see
[`docs/features-and-flows.md`](docs/features-and-flows.md).

## The gate chain

Every tool call runs through this pipeline. Each control-plane feature either
*configures* one of these gates or *observes* what it did.

```
  tool call
     │
  1. DELEGATION  may this agent call that agent? (a2a)     → Protocol Governance
  2. AUTH        may this agent use this tool?             → per-agent auth
  3. QUOTA       hit its rate / budget cap?                → Quotas
  4. RISK        score 0-100 → allow / intervene / block   → Policies
  5. APPROVAL    scored intervene? pause for a human (202) → Approvals (HITL)
  6. PAYMENT     does it cost money? can the wallet pay?   → Payments (x402)
  7. EXECUTE     forward to the real tool; meter usage     → Metering / Token Broker
  8. TRACE       record it → report to the control plane   → Live Traces / Audit / ROI
```

A gateway can run in **shadow** mode — every gate still evaluates and records
what it *would* have done, but nothing is blocked and no side effect runs. Try
before you enforce.

Gate 5 is opt-in (`OSTIARI_HITL=on`) and is what makes *intervene* a real tier
rather than a label: the gateway answers **202** with an approval id, a human
decides in the dashboard, and the caller re-submits with `X-Approval-Id`. Leave it
off and the tier is advisory in dev — but *refused* in production, which is
fail-closed. See the [control-plane guide](docs/control-plane-guide.md) §7.4.

## What the control plane gives you

- **Observe** — live trace stream, shadow reports, costs, metering, audit log,
  EU AI Act compliance reports, and an ROI "damage prevented" dashboard.
- **Control** — YAML-style policies (allow / block / risk-adjust), per-agent
  model access, per-gateway and per-agent quotas, and agent-to-agent (A2A)
  delegation governance with trust scoring.
- **Monetize** — x402 pay-per-tool-call with per-agent USDC wallets, and a token
  broker (bulk-buy/resell margin + pool inventory + invoice reconciliation).
- **Configure** — register gateways, agents, tools, and MCP servers.
- **Test** — a sandbox to fire calls and watch decisions, A/B model experiments,
  and an architecture view.

## Use the Guard library directly (no gateway)

If you just want the risk engine embedded in your own Python, that's the `Guard`
class — no control plane required:

```python
from ostiari import Guard
from ostiari.models import OstiariConfig, ThresholdConfig
from ostiari.storage import SQLiteBackend

guard = Guard(
    config=OstiariConfig(thresholds=ThresholdConfig(allow_max=30, intervene_max=70)),
    storage=SQLiteBackend(path="traces.db"),
)
guard.configure("policy.yaml")
guard.start()

result = guard.validate(action="email.send", params={"to": "user@example.com"})

# `tier` is the decision as *enforced*; `original_tier` is what the call actually
# scored. They differ for intervene, because the Guard has to resolve that tier
# in-process one way or the other before it can return — so `tier` is only ever
# "allow" or "block" and you check `original_tier` to find the gray cases.
if result.original_tier == "intervene":
    if get_approval(result):        # medium risk — ask a human
        send_email(result.params)
elif result.tier == "allow":
    send_email(result.params)
# A blocked call raises ActionBlockedError instead of returning. With
# fail_open=False that includes an unresolved intervene — the exception's
# `original_tier` tells you which it was, so you can escalate rather than refuse.
```

Register an intervention callback (`guard.gateway.set_intervention_callback(...)`)
and the Guard resolves the tier itself — but it *blocks* waiting for the answer, so
for an asynchronous human queue prefer the gateway's HITL gate, which returns 202
and lets the caller re-submit.

Or protect a function inline:

```python
from ostiari import protect

@protect()                                  # action is the function name
def send_email(to: str, subject: str, body: str): ...

@protect(risk="high", confirm=True)         # hint the score, force the intervene tier
def db_delete(table: str): ...
```

`protect()` takes `risk`, `confirm`, and `policy` — there is no `action`
parameter (passing one is a `TypeError`). The action is `fn.__name__`, so name
the function what you want to see in policies and traces. It wraps sync and
async functions alike, and lazily creates a module-level Guard on first call —
use `ostiari.init(config=…)` to configure that singleton up front.

### Policy (YAML)

```yaml
allow:                       # fnmatch globs
  - "file.read"
  - "db_query"
block:
  - "*delete*"
  - "*drop*"
rules:
  - type: risk_adjust        # nudge toward "intervene"
    action: "email.send"
    risk_adjust: 25
thresholds:
  global:
    allow_max: 30
    intervene_max: 70
```

The schema is strict, and both halves of it are checked before anything loads:

- **Top level accepts exactly `allow`, `block`, `rules`, `thresholds`.** Anything
  else — including a `version:` key — is rejected as an unknown top-level key.
- **A rule is keyed by `type`, not `decision`**, and `type` must be one of
  `allow`, `block`, `risk_adjust`, `threshold_override`, `context_rule`. The
  adjustment field repeats the type name (`risk_adjust: 25`), must be a non-zero
  integer, and a rule always needs an `action` glob.
- Caps: 500 rules per file, 256 characters per pattern. The same pattern in both
  `allow` and `block` is an error, not a precedence question.

Errors carry the field and line number, so a bad policy fails loudly at
`configure()` rather than silently allowing everything.

> **Pattern gotcha:** globs are `fnmatch`. `*.delete` matches `github.delete` but
> **not** `db_delete` (no dot). To block `db_delete`, use `*delete*` or the exact
> name. See the [control plane guide](docs/control-plane-guide.md).

### Framework adapters

Ostiari normalizes tool calls from any framework into one shape:

```python
from ostiari.adapters.openai import OpenAIAdapter
from ostiari.adapters.claude import ClaudeAdapter
from ostiari.adapters.bedrock import BedrockAdapter
from ostiari.adapters.strands import StrandsAdapter

guard = Guard(adapter=[OpenAIAdapter(), ClaudeAdapter()])   # one or many
```

Built-in **anomaly detectors** (loop, drift, hallucination, contradiction) and a
**circuit breaker** feed the same risk score.

## Install

```bash
# Full platform (control plane + gateway + dashboard) — for the demo
make install

# Just the library, from a clone
pip install -e .
pip install -e ".[all]"           # + all adapters, dashboard, TUI
```

Ostiari is **not on PyPI yet**, so install from source. The extras are real
either way: `claude`, `openai`, `bedrock`, `strands`, `policy`, `fuzzy`, `tui`,
`dashboard`, `all`, and `dev`.

## Make targets

| Target | What it does |
|---|---|
| `make demo-full` | Full demo — control plane, 4 gateways, A2A agent, seeded data (→ :9000) |
| `make dev` | Control plane + frontend + primary gateway |
| `make demo` | Frontend only — landing page and build check, **no backend, so no dashboard** |
| `make clean-start` | No demo data (`OSTIARI_NO_DEMO=1`) — empty registry, empty DB |
| `make test` | Run the test suites |
| `make lint` | Ruff over `src/` + `gateway/`, then `tsc --noEmit` over the frontend |
| `make install` / `make build` / `make clean` | Deps, frontend production build, build artifacts |

> The Sandbox chat needs LLM credentials; point `make demo-full` at an env file
> with `LLM_ENV=/path/to/.env`. Everything else runs without keys.

> **`clean-start` gives you an empty control plane** — SQLite DB, `state.json`, and
> the 18 model routing configs all gone. It used to delete only the pre-`data_dir()`
> `state.json` path while the lifespan restored the live one, and `seed_models()`
> ran at import time ungated, so quotas, experiments, providers, and the model
> catalog all came back. See [`QUICKSTART.md` §3](QUICKSTART.md#3-clean-install).

## Architecture

```
┌──────────────────────── Control Plane (FastAPI + React) ────────────────────┐
│  Observe · Control · Monetize · Configure · Test · Admin                     │
│  configure once → push to gateways · collect traces ← from gateways         │
└──────────────────────────────────────────────────────────────────────────────┘
        ▲  push config          │ report traces          ▲
        │                       ▼                         │
   ┌────┴─────┐          ┌──────────────┐          ┌──────┴─────┐
   │ Gateway  │  ...     │   Gateway    │   ...    │  Gateway   │   (one per agent)
   │  gate    │          │  gate chain  │          │  gate      │
   │  chain   │          │  + MCP + A2A │          │  chain     │
   └────┬─────┘          └──────┬───────┘          └─────┬──────┘
        ▼                       ▼                        ▼
     Agent                    Agent                    Agent
```

## Development

```bash
git clone https://github.com/hk-775/ostiari.git
cd ostiari
pip install -e ".[dev]"

pytest tests/                                   # root (Guard) suite
cd control-plane/backend && PYTHONPATH=. pytest tests/    # control plane
cd gateway && PYTHONPATH=. pytest tests/                  # gateway

ruff check src/ gateway/
mypy --strict src/
```

## Project layout

```
src/ostiari/          # the Guard library — risk engine, policy, adapters, storage
gateway/              # the sidecar proxy — gate chain, MCP, A2A, payments
control-plane/
  backend/            # FastAPI control plane — routers, models, services
  frontend/           # React dashboard (Vite + Tailwind + TanStack Query)
docs/                 # architecture + the control plane guide
```

## Documentation

- [`docs/features-and-flows.md`](docs/features-and-flows.md) — canonical feature
  inventory and end-to-end flows, grounded in the current code
- [`STARTUP.md`](STARTUP.md) — full startup & deployment guide: local (no demo),
  local (full demo), and enterprise service, with per-feature config and diagrams
- [`QUICKSTART.md`](QUICKSTART.md) — condensed quick-start cheat-sheet
- [`docs/control-plane-guide.md`](docs/control-plane-guide.md) — complete
  control-plane tour (novice-friendly, with diagrams, best practices, pitfalls)
- [`docs/gateway-architecture.md`](docs/gateway-architecture.md) — gateway internals
- [`docs/detection-engine.md`](docs/detection-engine.md) — PII redaction and
  prompt-injection detection: config, what's detected, and what it can't catch
- [`docs/Ostiari-Configure-Orchestrate-Lifecycle.md`](docs/Ostiari-Configure-Orchestrate-Lifecycle.md)
  — how config reaches gateways: register/heartbeat lifecycle, Push semantics,
  and what does *not* survive a restart
- [`auth/README.md`](auth/README.md) — OIDC / Cognito: the three principals, role
  mapping, and the two env vars production needs
- [`docs/axon-router.md`](docs/axon-router.md) and
  [`docs/agent-llm-routing.md`](docs/agent-llm-routing.md) — LLM routing through
  the embedded AxonLLM router, and per-agent model access
- [`docs/adversarial-review.md`](docs/adversarial-review.md) — the security
  review: what was found, what was fixed, what's still open
- [`deploy/README.md`](deploy/README.md) — deployment reference
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to contribute

## License

Apache 2.0 — see [LICENSE](LICENSE).
