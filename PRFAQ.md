# PRFAQ: Ostiari — Runtime Safety and Reliability for AI Agents

## Press Release

### Ostiari: The Guardrail Layer Between Your AI Agents and the Outside World

**Subheadline:** Open-source runtime safety framework that intercepts, scores, and enforces policies on every tool call an AI agent makes — regardless of framework.

**Seattle, WA** — Today we announce Ostiari, a runtime safety and reliability layer that gives platform teams centralized control over what AI agents can do in production. As organizations deploy autonomous agents across customer support, code generation, data analysis, and operations, they face a critical gap: no standardized way to enforce safety policies, detect anomalous behavior, or maintain human oversight across diverse agent frameworks.

**The Problem:** AI agents are powerful but unpredictable. An agent tasked with "clean up old files" might delete production data. An agent writing code might execute arbitrary commands. Today, each team builds ad-hoc safety checks inside their agent logic — creating inconsistent enforcement, blind spots, and no centralized visibility. The problem compounds when organizations run agents on multiple frameworks (OpenAI, Anthropic, Bedrock, Strands, CrewAI, LangGraph) with no unified governance layer.

**The Solution:** Ostiari intercepts every tool call an AI agent makes, scores its risk on a 0-100 scale, and enforces YAML-based safety policies — regardless of which framework the agent runs on. It's a single middleware layer that provides policy enforcement, anomaly detection (loop/drift/hallucination/contradiction), circuit breakers, human-in-the-loop intervention, checkpoint/rollback, and full observability with a web dashboard.

**Key Features:**

- **Policy Engine** — YAML-based rules that allow, score, or block actions by pattern with configurable thresholds
- **Multi-Framework Adapters** — Drop-in support for OpenAI, Anthropic Claude, AWS Bedrock, Strands Agents, and more
- **Anomaly Detection** — Loop detection, drift detection, hallucination checks, and contradiction detection
- **Circuit Breaker** — Automatic trip when error/block rates exceed thresholds
- **Human-in-the-Loop** — Intervention gateway for medium-risk actions requiring approval
- **Checkpoint/Rollback** — Save agent state at safe points; roll back when things go wrong
- **Full Observability** — Web dashboard with real-time traces, metrics, and policy editing

**Customer Quote:**
"Before Ostiari, we had no way to know what our agents were doing until something broke. Now every tool call is scored, logged, and governed by a single policy file. We caught three runaway loops in the first week that would have cost us thousands in API calls." — VP of Engineering, Enterprise SaaS Company

**Get Started:** `pip install ostiari` | GitHub: github.com/hk-775/Ostiari

---

## Frequently Asked Questions

### Customer FAQ

**Q: Which agent frameworks does Ostiari support?**
A: Ostiari ships with adapters for OpenAI, Anthropic Claude, AWS Bedrock, and Strands Agents. The adapter interface is pluggable — you can write a custom adapter for any framework in under 50 lines of code.

**Q: Does Ostiari add latency to agent tool calls?**
A: Policy evaluation typically adds <5ms per tool call. For the intervention tier (human approval), latency depends on approval workflow configuration — you can set timeouts and auto-approve/deny defaults.

**Q: How do I define what's allowed vs. blocked?**
A: Policies are defined in YAML files with pattern-matching rules. Each rule assigns a risk score (0-100) to action patterns. Scores map to tiers: allow (≤30), intervene (31-70), block (>70). Thresholds are configurable.

**Q: Can Ostiari work with agents already in production?**
A: Yes. Ostiari wraps your existing agent runtime as middleware — no changes to agent logic required. Add 3 lines of code to your agent entry point and deploy a policy file.

**Q: What happens when Ostiari's circuit breaker trips?**
A: When error or block rates exceed configured thresholds, the circuit breaker trips and all subsequent tool calls are blocked until the circuit resets (configurable cooldown period). This prevents cascading failures from a malfunctioning agent.

**Q: Does Ostiari store agent traces?**
A: Yes. Full trace storage with SQLite (local) or PostgreSQL (production) backends. Every tool call, risk score, policy decision, and outcome is recorded. The web dashboard provides real-time visibility and historical analysis.

**Q: Does Ostiari use AI models internally?**
A: No. Ostiari's risk scoring and anomaly detection are entirely deterministic — based on policy rules, pattern matching, and statistical heuristics. No LLM calls are made during evaluation, ensuring predictable behavior and zero additional API costs.

**Q: Is there any data sent outside my environment?**
A: No. Ostiari runs entirely in-process. All traces, policy decisions, and agent state are stored locally or in your own infrastructure. No telemetry, no phone-home, no third-party services.

### Internal FAQ

**Q: How does Ostiari compare to Bedrock Guardrails?**
A: Bedrock Guardrails focuses on content safety (toxicity, PII, topic avoidance) at the model input/output level. Ostiari operates at the tool-call level — governing what actions an agent takes, not what it says. They are complementary: use Guardrails for content policy and Ostiari for action policy.

**Q: What's the deployment model?**
A: Ostiari runs as an in-process middleware library (pip install). No separate infrastructure required. For the dashboard, deploy as a sidecar container or standalone service. No data leaves your environment.

**Q: What's the open-source licensing model?**
A: Apache 2.0. Published on PyPI and GitHub. Customers can use, modify, and distribute freely.

**Q: What's the production readiness?**
A: 585 tests (unit, property-based, integration), 90%+ code coverage, strict mypy type checking, CI matrix across Python 3.10-3.13 on Ubuntu/macOS/Windows. All checks passing.

**Q: Who is the target customer?**
A: Platform engineering teams deploying AI agents in production who need centralized safety governance. Especially relevant for enterprises running agents on multiple frameworks and needing consistent policy enforcement.

**Q: How does this relate to the Agentic AI Ambassador program?**
A: Ostiari is a field-built solution addressing the #1 customer concern with agentic AI adoption: safety and governance. It demonstrates AWS thought leadership in responsible AI deployment and serves as a reference architecture for enterprise agent governance.

**Q: What's the competitive landscape?**
A: There is no direct equivalent. Guardrails AI focuses on LLM output validation (content, not actions). LangChain/LangSmith provide observability but not enforcement. Ostiari is unique in providing runtime action-level governance as a framework-agnostic middleware.
