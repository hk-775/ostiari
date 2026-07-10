# Ostiari

Runtime safety and reliability layer for AI agents.

[![CI](https://github.com/hk-775/Ostiari/actions/workflows/ci.yml/badge.svg)](https://github.com/hk-775/Ostiari/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/hk-775/Ostiari/branch/main/graph/badge.svg)](https://codecov.io/gh/hk-775/Ostiari)
[![PyPI](https://img.shields.io/pypi/v/ostiari)](https://pypi.org/project/ostiari/)
[![Python 3.10+](https://img.shields.io/pypi/pyversions/ostiari)](https://pypi.org/project/ostiari/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

Ostiari intercepts every tool call an AI agent makes, scores its risk, and enforces safety policies — regardless of which framework the agent runs on. It's the guardrail layer between your agents and the outside world.

## Features

- **Policy engine** — YAML-based rules that allow, score, or block actions by pattern
- **Risk scoring** — 0-100 risk scores with configurable thresholds (allow / intervene / block)
- **Anomaly detection** — loop detection, drift detection, hallucination checks, contradiction detection
- **Circuit breaker** — automatic trip when error/block rates exceed thresholds
- **Multi-framework adapters** — OpenAI, Anthropic Claude, AWS Bedrock, Strands Agents (pluggable)
- **Intervention gateway** — human-in-the-loop approval for medium-risk actions
- **Checkpoint/rollback** — save and restore agent state
- **Observability** — full trace storage, metrics, real-time WebSocket streaming
- **Dashboard** — web UI for viewing traces, editing policies, and monitoring agents
- **Terminal UI** — rich TUI for local development

## Installation

```bash
pip install ostiari
```

With framework adapters:

```bash
pip install ostiari[openai]      # OpenAI adapter
pip install ostiari[claude]      # Anthropic Claude adapter
pip install ostiari[bedrock]     # AWS Bedrock adapter
pip install ostiari[strands]     # Strands Agents adapter
pip install ostiari[dashboard]   # Web dashboard
pip install ostiari[tui]         # Terminal UI
pip install ostiari[all]         # Everything
```

## Quick Start

```python
from ostiari import Guard
from ostiari.models import OstiariConfig, ThresholdConfig
from ostiari.storage import SQLiteBackend

# Configure thresholds
config = OstiariConfig(
    thresholds=ThresholdConfig(allow_max=30, intervene_max=70),
    fail_open=False,
)

# Create guard with storage
guard = Guard(config=config, storage=SQLiteBackend(path="traces.db"))
guard.configure("policy.yaml")
guard.start()

# Validate an action
result = guard.validate(action="email.send", params={"to": "user@example.com"})

if result.tier == "allow":
    # Proceed — low risk
    send_email(result.params)
elif result.tier == "intervene":
    # Medium risk — request human approval
    if get_approval(result):
        send_email(result.params)
else:
    # result.tier == "block" won't reach here —
    # guard.validate() raises ActionBlockedError for blocked actions
    pass
```

## Policy Configuration

Define rules in YAML:

```yaml
version: "1"
rules:
  # Always allow read operations
  - action: "file.read"
    decision: allow

  - action: "web.search"
    decision: allow

  # Block dangerous operations
  - action: "*.delete"
    decision: block
    description: "All delete operations blocked"

  - action: "code.execute"
    decision: block
    description: "Code execution blocked"

  # Score-based evaluation for everything else
  risk_adjust:
    - action: "email.send"
      adjust: +25
    - action: "file.write"
      adjust: +15
    - action: "db.query"
      adjust: +5
```

## Framework Integration

### OpenAI

```python
from ostiari import Guard
from ostiari.adapters.openai import OpenAIAdapter

guard = Guard(adapter=OpenAIAdapter())
guard.start()

# Validate before executing tool calls
result = guard.validate("web.search", {"query": "AI safety"})
```

### Anthropic Claude

```python
from ostiari.adapters.claude import ClaudeAdapter

guard = Guard(adapter=ClaudeAdapter())
```

### AWS Bedrock

```python
from ostiari.adapters.bedrock import BedrockAdapter

guard = Guard(adapter=BedrockAdapter())
```

### Strands Agents

```python
from ostiari.adapters.strands import StrandsAdapter

guard = Guard(adapter=StrandsAdapter())
```

### Multiple Adapters

```python
guard = Guard(adapter=[OpenAIAdapter(), ClaudeAdapter(), BedrockAdapter()])
```

## Anomaly Detection

Built-in detectors catch problematic agent behavior:

```python
from ostiari import Guard, AnomalyDetector

detector = AnomalyDetector()
guard = Guard(anomaly_detector=detector)
```

Detectors:
- **Loop detection** — agent repeating the same action
- **Drift detection** — agent deviating from expected behavior patterns
- **Hallucination detection** — agent referencing non-existent resources
- **Contradiction detection** — agent actions that contradict prior context

## Circuit Breaker

Automatically trips when failure rates exceed thresholds:

```python
from ostiari.models import BreakerConfig

guard = Guard(
    breaker_configs=[
        BreakerConfig(
            name="default",
            failure_threshold=5,
            recovery_timeout=60,
            half_open_max_calls=3,
        )
    ]
)
```

## Decorator API

Protect functions directly:

```python
from ostiari import protect

@protect(action="email.send")
def send_email(to: str, subject: str, body: str):
    ...
```

## Observability

All evaluations are traced and stored:

```python
from ostiari.storage import SQLiteBackend
from ostiari.models import TraceFilters

storage = SQLiteBackend(path="traces.db")

# Query traces
traces = storage.get_traces(TraceFilters(tier="block", limit=100))
for t in traces:
    print(f"{t.action} → {t.tier} (score={t.risk_score})")
```

## Dashboard

```bash
pip install ostiari[dashboard]
ostiari dashboard --port 8420
```

Web UI for:
- Real-time trace viewer with filtering
- Policy editor
- Agent metrics and health
- Intervention queue

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    guard.validate()                       │
├─────────────────────────────────────────────────────────┤
│  Adapter Pre-hooks → Policy Engine → Anomaly Detector   │
│              → Gateway (score + tier) → Trace Storage    │
├─────────────────────────────────────────────────────────┤
│  Circuit Breaker │ Checkpoint Engine │ Redaction Filter  │
└─────────────────────────────────────────────────────────┘
```

Pipeline for each `guard.validate()` call:
1. **Adapter pre-hooks** — normalize action/params from framework-specific format
2. **Policy engine** — evaluate rules, decide allow/block/score
3. **Anomaly detection** — check for loops, drift, hallucination, contradictions
4. **Gateway** — combine signals into final risk score and tier
5. **Intervention** — if tier=intervene, request human approval (or use callback)
6. **Trace storage** — record full evaluation result
7. **Circuit breaker** — update failure counters

## Development

```bash
git clone https://github.com/aws/ostiari.git
cd ostiari
pip install -e ".[dev]"

# Run tests
pytest tests/unit/ -v
pytest tests/property/ --hypothesis-seed=0
pytest tests/integration/

# Lint
ruff check src/ tests/
ruff format --check src/ tests/

# Type check
mypy --strict src/
```

## Project Structure

```
src/ostiari/
├── __init__.py          # Public API exports
├── guard.py             # Guard — central mediator
├── gateway.py           # ActionGateway — scoring and tiering
├── policy/              # Policy engine, parser, poller, rules
├── anomaly/             # Loop, drift, hallucination, contradiction detectors
├── adapters/            # OpenAI, Claude, Bedrock, Strands adapters
├── storage/             # SQLite backend, migrations, redaction
├── breaker.py           # Circuit breaker
├── checkpoint.py        # Checkpoint/rollback engine
├── tracer.py            # Execution tracer
├── health.py            # Health checker
├── report.py            # Report generator
├── decorators.py        # @protect decorator
├── cli.py               # CLI entry point
├── dashboard/           # FastAPI web dashboard
└── tui/                 # Textual terminal UI
```

## License

Apache 2.0 — see [LICENSE](LICENSE).
