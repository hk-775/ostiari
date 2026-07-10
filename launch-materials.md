# Ostiari Launch Materials

---

## 1. Launch Blog Post (AWS Open Source Blog)

---

### Introducing Ostiari: Runtime Safety and Reliability for AI Agents

AI agents are moving from prototypes to production. They browse the web, send emails, execute code, query databases, and manage infrastructure — all autonomously. But with autonomy comes risk: an agent that can send emails can also spam your customers. An agent with database access can drop tables. An agent in a loop can burn through your API budget in minutes.

**Today, we're excited to announce Ostiari** — an open-source runtime safety and reliability layer that intercepts every tool call an AI agent makes, scores its risk, and enforces safety policies. Think of it as the guardrail between your agents and the outside world.

#### The Problem

Teams deploying AI agents in production face a common set of challenges:

- **No runtime guardrails.** Agents decide and act. If they hallucinate a dangerous action, nothing stops them.
- **No visibility.** When an agent goes off the rails, there's no trace of what happened or why.
- **Framework lock-in.** Safety logic written for one agent framework (OpenAI, Claude, Bedrock) doesn't transfer to another.
- **Binary safety models.** Existing approaches either block everything (too restrictive) or allow everything (too dangerous). There's no middle ground where medium-risk actions get human review.
- **No recovery.** When an agent fails mid-task, there's no way to roll back to a known-good state.

#### How Ostiari Works

Ostiari sits between your agent and its tools. Every action passes through a pipeline:

```
Agent → Ostiari Guard → Policy Engine → Anomaly Detection → Risk Score → Decision
                                                                              ↓
                                                              allow / intervene / block
```

1. **Policy engine** — YAML-based rules that allow, block, or score actions by pattern. Define rules like "block all `*.delete` operations" or "add +25 risk to `email.send`."

2. **Risk scoring** — Every action gets a 0–100 risk score. Configurable thresholds determine whether it's allowed (low risk), requires human approval (medium risk), or is blocked (high risk).

3. **Anomaly detection** — Built-in detectors catch:
   - **Loops** — agent repeating the same action
   - **Drift** — agent deviating from expected patterns
   - **Hallucinations** — agent referencing non-existent resources
   - **Contradictions** — agent actions that conflict with prior context

4. **Circuit breaker** — Automatically trips when error or block rates exceed thresholds, preventing runaway agents from causing damage.

5. **Intervention gateway** — Medium-risk actions can be routed to a human for approval via callbacks, webhooks, or the built-in dashboard.

6. **Checkpoint/rollback** — Save agent state at safe points; roll back when things go wrong.

#### Framework Agnostic

Ostiari works with any agent framework through pluggable adapters:

```bash
pip install ostiari[openai]      # OpenAI function calling
pip install ostiari[claude]      # Anthropic Claude tool use
pip install ostiari[bedrock]     # AWS Bedrock agents
pip install ostiari[strands]     # Strands Agents
pip install ostiari[all]         # All adapters
```

Your safety policies stay the same regardless of which LLM or framework you use. Switch from OpenAI to Bedrock — your guardrails come with you.

#### Five Lines to Safety

```python
from ostiari import Guard

guard = Guard()
guard.configure("policy.yaml")
guard.start()

result = guard.validate(action="email.send", params={"to": "ceo@bigcorp.com"})
# result.tier → "allow" | "intervene" | "block"
```

#### Full Observability

Every evaluation is stored as a trace — action, parameters, risk score, decision tier, anomaly flags, and timestamps. Query them programmatically or browse them in the web dashboard:

```bash
ostiari dashboard --port 8420
```

The dashboard provides real-time trace viewing, policy editing, intervention queues, and agent health metrics — all in a browser.

#### Built for Production

- **585 tests** including unit, property-based (Hypothesis), and integration tests
- **90%+ code coverage** enforced in CI
- **CI matrix** across Python 3.10–3.13 on Ubuntu, macOS, and Windows
- **Strict type checking** with mypy in strict mode
- **PyPI published** via trusted publishing

#### Get Started

```bash
pip install ostiari
```

Full documentation, examples, and contributing guide: [github.com/aws/ostiari](https://github.com/aws/ostiari)

Ostiari is licensed under Apache 2.0. We welcome contributions — whether that's new framework adapters, anomaly detectors, policy patterns, or bug reports. See [CONTRIBUTING.md](https://github.com/aws/ostiari/blob/main/CONTRIBUTING.md) to get started.

---

## 2. Slack / Internal Announcement (Short)

---

🚀 **Ostiari is now open-source!**

Runtime safety layer for AI agents — intercepts every tool call, scores risk, enforces policies.

**What it does:**
• Policy engine (YAML rules → allow / intervene / block)
• Risk scoring (0–100) with configurable thresholds
• Anomaly detection (loops, drift, hallucinations, contradictions)
• Circuit breaker for runaway agents
• Human-in-the-loop for medium-risk actions
• Works with OpenAI, Claude, Bedrock, Strands Agents

**Why it matters:** Agents are going to production. Without runtime guardrails, one hallucinated `rm -rf` or one infinite loop burns real money and real trust. Ostiari is the seatbelt.

🔗 **GitHub:** github.com/aws/ostiari
📦 **PyPI:** `pip install ostiari`
📄 **License:** Apache 2.0

5 lines of code to add safety to any agent:
```python
from ostiari import Guard
guard = Guard()
guard.configure("policy.yaml")
guard.start()
result = guard.validate(action="email.send", params={"to": "user@corp.com"})
```

Built by @harlnk from the TMEGS M&E team. PRs welcome!

---

## 3. Email (Internal Amplification / DevAx)

---

**Subject:** [Launch] Ostiari — Open-Source Runtime Safety for AI Agents (github.com/aws/ostiari)

Hi team,

I'm excited to share that **Ostiari** is now publicly available on GitHub under the `aws` org.

**What is it?**
Ostiari is a runtime safety and reliability layer for AI agents. It intercepts every tool call an agent makes, scores its risk, and enforces safety policies — regardless of which framework the agent runs on (OpenAI, Claude, Bedrock, Strands Agents).

**Why does it matter?**
As customers move AI agents from prototypes to production, the #1 concern I hear is: "How do we make sure the agent doesn't do something catastrophic?" There's no existing AWS service that provides runtime action-level guardrails for agents. Ostiari fills that gap.

**Key capabilities:**
- YAML-based policy engine (allow / score / block by action pattern)
- Risk scoring (0–100) with three tiers: allow, intervene (human approval), block
- Anomaly detection: loop detection, drift, hallucination checks, contradictions
- Circuit breaker for automatic protection against runaway agents
- Checkpoint/rollback for agent state recovery
- Full trace storage and web dashboard for observability
- Framework-agnostic: plug into any agent with a 5-line integration

**Links:**
- GitHub: https://github.com/aws/ostiari
- PyPI: https://pypi.org/project/ostiari/
- License: Apache 2.0

**How you can help:**
- ⭐ Star the repo
- Share with customers building agents on AWS
- File issues or contribute adapters/detectors
- Include in relevant workshops and immersion days

This came out of repeated customer conversations about agent safety during Bedrock and Strands Agents engagements. Happy to do a lunch-and-learn or brown bag if there's interest.

Thanks,
Harleen

---

## 4. One-Liner Descriptions (for various contexts)

---

**Twitter/LinkedIn (280 chars):**
Introducing Ostiari — open-source runtime safety for AI agents. Intercepts every tool call, scores risk, enforces policies. Works with OpenAI, Claude, Bedrock, Strands. `pip install ostiari` 🔗 github.com/aws/ostiari

**README badge line:**
Ostiari intercepts every tool call an AI agent makes, scores its risk, and enforces safety policies — regardless of which framework the agent runs on.

**One-sentence pitch:**
The seatbelt for AI agents — runtime guardrails that prevent catastrophic actions without blocking useful ones.

**Elevator pitch (30 seconds):**
AI agents are going to production, but there's nothing stopping them from hallucinating a dangerous action and executing it. Ostiari is an open-source safety layer that sits between your agent and its tools. Every action gets a risk score — low-risk actions proceed, medium-risk actions get human review, high-risk actions are blocked. It works with any framework, deploys with pip install, and comes with anomaly detection, circuit breakers, and full observability out of the box.
