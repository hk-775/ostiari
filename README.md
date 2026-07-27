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

Open **http://localhost:9000**, log in with `admin@ostiari.ai` / `admin`, and
you'll land on a fully-populated dashboard: four agent gateways governing real
tool calls, live traces streaming, blocked destructive actions, MCP servers,
agent-to-agent delegation, payments, and ROI — no setup, no mock data.

> Everything in the demo is **real**: the gateways actually proxy and govern
> live tool calls (a `db_delete` really gets blocked with a 403), the MCP
> servers really run (`npx` draw.io + filesystem), and one agent really
> delegates to another. Only external money movement (on-chain x402, Stripe) is
> simulated behind a clean seam.

**New here?** Read [`docs/control-plane-guide.md`](docs/control-plane-guide.md)
— a complete, novice-friendly tour of the control plane with architecture
diagrams, best practices, and pitfalls.

## The two halves

| | What it is | Where |
|---|---|---|
| **Gateway** (sidecar) | A proxy that runs next to each agent and enforces the rules in the request path. Fast, local. | `gateway/` |
| **Control Plane** | The central brain + dashboard: configure policies once and push them to every gateway; watch the whole fleet. | `control-plane/` |
| **Guard** (library) | The embeddable risk engine — use it directly in Python without the gateway. | `src/ostiari/` |

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
  5. PAYMENT     does it cost money? can the wallet pay?   → Payments (x402)
  6. EXECUTE     forward to the real tool; meter usage     → Metering / Token Broker
  7. TRACE       record it → report to the control plane   → Live Traces / Audit / ROI
```

A gateway can run in **shadow** mode — every gate still evaluates and records
what it *would* have done, but nothing is blocked and no side effect runs. Try
before you enforce.

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
if result.tier == "allow":
    send_email(result.params)
elif result.tier == "intervene":
    if get_approval(result):        # medium risk — ask a human
        send_email(result.params)
# tier == "block" raises ActionBlockedError from guard.validate()
```

Or protect a function inline:

```python
from ostiari import protect

@protect(action="email.send")
def send_email(to: str, subject: str, body: str): ...
```

### Policy (YAML)

```yaml
version: "1"
rules:
  - action: "file.read"     # always allow reads
    decision: allow
  - action: "*.delete"      # block deletes  (note: matches "x.delete", not "db_delete")
    decision: block
  risk_adjust:
    - action: "email.send"
      adjust: +25            # nudge toward "intervene"
```

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

# Just the library
pip install ostiari
pip install ostiari[all]          # + all adapters, dashboard, TUI
```

## Make targets

| Target | What it does |
|---|---|
| `make demo-full` | Full demo — control plane, 4 gateways, A2A agent, seeded data (→ :9000) |
| `make dev` | Control plane + frontend + primary gateway |
| `make demo` | Frontend only, mock data |
| `make clean-start` | Everything empty — no demo data |
| `make test` | Run the test suites |
| `make lint` | Ruff |

> The Sandbox chat needs LLM credentials; point `make demo-full` at an env file
> with `LLM_ENV=/path/to/.env`. Everything else runs without keys.

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

- [`STARTUP.md`](STARTUP.md) — full startup & deployment guide: local (no demo),
  local (full demo), and enterprise service, with per-feature config and diagrams
- [`QUICKSTART.md`](QUICKSTART.md) — condensed quick-start cheat-sheet
- [`docs/control-plane-guide.md`](docs/control-plane-guide.md) — complete
  control-plane tour (novice-friendly, with diagrams, best practices, pitfalls)
- [`docs/gateway-architecture.md`](docs/gateway-architecture.md) — gateway internals
- [`docs/detection-engine.md`](docs/detection-engine.md) — PII redaction and
  prompt-injection detection: config, what's detected, and what it can't catch
- [`deploy/README.md`](deploy/README.md) — deployment reference
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to contribute

## License

Apache 2.0 — see [LICENSE](LICENSE).
