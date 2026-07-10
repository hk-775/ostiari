# OSPO Business Justification: Ostiari Open-Source Publication

---

## Project Summary

| Field | Value |
|-------|-------|
| **Project Name** | Ostiari |
| **Repository** | github.com/aws/ostiari |
| **Target Org** | `aws` (first-party) |
| **License** | Apache 2.0 |
| **Language** | Python 3.10+ |
| **Owner** | Harleen Kaur (harlnk), Sr. Solutions Architect, TMEGS M&E |
| **Manager** | Cynthia Mazza |
| **Current State** | Private repo, 585 tests, PyPI-ready |
| **Request** | Make repository public |

---

## What Is Ostiari?

Ostiari is a runtime safety and reliability layer for AI agents. It intercepts every tool call an AI agent makes, scores its risk, and enforces safety policies — regardless of which framework the agent runs on.

**Core capabilities:**
- Policy engine (YAML-based rules: allow / score / block by action pattern)
- Risk scoring (0–100) with configurable thresholds and three tiers (allow / intervene / block)
- Anomaly detection (loop detection, drift detection, hallucination checks, contradiction detection)
- Circuit breaker for automatic protection against runaway agents
- Human-in-the-loop intervention gateway for medium-risk actions
- Checkpoint/rollback for agent state recovery
- Multi-framework adapters: OpenAI, Anthropic Claude, AWS Bedrock, Strands Agents
- Full trace storage, web dashboard, and terminal UI for observability

---

## Business Justification

### 1. Drives Adoption of AWS AI Agent Services

Ostiari directly complements and drives usage of:

| AWS Service | How Ostiari Drives Adoption |
|-------------|-------------------------------|
| **Amazon Bedrock Agents** | Provides the runtime safety layer customers need to deploy Bedrock agents in production. Removes the #1 cited blocker: "How do we prevent the agent from doing something catastrophic?" |
| **Strands Agents** | Ships with a native Strands adapter. Customers adopting Strands get production-grade safety with `pip install ostiari[strands]` |
| **Amazon Bedrock AgentCore** | Ostiari can be deployed on AgentCore, showcasing the platform's capabilities for safety-critical workloads |
| **Amazon Bedrock Guardrails** | Complementary — Guardrails handles content filtering; Ostiari handles action-level runtime safety. Together they form the complete safety stack. |

**Key insight:** Customers won't deploy agents in production without safety guardrails. Every team that adopts Ostiari is a team running agents on AWS.

### 2. Fills a Gap No AWS Service Addresses

Current AWS capabilities address content safety (Guardrails) and model access control (IAM), but **no service provides runtime action-level guardrails for agents:**

| Need | AWS Today | Ostiari |
|------|-----------|------------|
| Block dangerous tool calls at runtime | ❌ | ✅ |
| Risk-score every agent action | ❌ | ✅ |
| Detect agent loops, drift, hallucinations | ❌ | ✅ |
| Human-in-the-loop for medium-risk actions | ❌ | ✅ |
| Circuit breaker for runaway agents | ❌ | ✅ |
| Checkpoint/rollback for agent state | ❌ | ✅ |
| Framework-agnostic safety policies | ❌ | ✅ |

Without Ostiari, customers either build custom safety middleware (weeks of engineering) or deploy agents without guardrails (risk). Open-sourcing gives them a production-ready option that keeps them on AWS.

### 3. Competitive Positioning

The AI agent safety space is emerging. Key landscape:

| Solution | Scope | Open Source? | AWS-Native? |
|----------|-------|-------------|-------------|
| **Ostiari** | Runtime action guardrails, anomaly detection, circuit breaker | ✅ Apache 2.0 | ✅ Bedrock + Strands adapters |
| Guardrails AI (competitor) | Content filtering for LLM I/O | ✅ (limited) | ❌ |
| LangChain Safety | Basic tool restrictions | Partial | ❌ |
| Custom middleware | Varies | N/A | Varies |

By open-sourcing under the `aws` org, we establish AWS as the leader in agent safety — before competitors consolidate the space.

### 4. Community and Ecosystem Growth

| Metric | Expected Impact |
|--------|----------------|
| **GitHub stars** | 500–2,000 within 6 months (based on comparable AWS OSS projects) |
| **PyPI downloads** | Grows with agent adoption — every Strands/Bedrock agent deployment is a potential user |
| **Contributions** | New framework adapters, anomaly detectors, and policy patterns from community |
| **Conference talks** | Ready for re:Invent, PyCon, AI Engineering Summit content |
| **Customer conversations** | Immediate talking point for SA teams during agent workshops and immersion days |

### 5. Low Risk, High Return

| Risk Factor | Assessment |
|-------------|-----------|
| **IP exposure** | No proprietary algorithms. Standard policy engine, risk scoring, and circuit breaker patterns. The value is in the integration and AWS-native experience. |
| **Competitive enablement** | Competitors could fork — but the project's value is its AWS integration (Bedrock adapter, Strands adapter, AgentCore deployment). Forks lose this. |
| **Maintenance burden** | Minimal. 585 tests + CI ensure stability. Community PRs reduce load over time. |
| **Security** | Full audit completed. No secrets, no internal references, no customer data. Apache 2.0 includes patent grant protecting users and contributors. |

---

## Customer Signal

Direct feedback from enterprise customers building agents on AWS:

- **Platform engineering teams** consistently cite runtime safety as the #1 blocker to production agent deployment
- **Compliance teams** in regulated industries (FSI, healthcare) require auditable guardrails before approving agent workloads
- **Developers** want framework-agnostic safety that doesn't require rewriting policies when they switch LLM providers
- Conversations across Hearst portfolio companies (Fitch Group, QGenda, CAMP Systems), Tinder/Match Group, and others confirm this is a universal need once agents move beyond prototypes

---

## Technical Readiness

| Criteria | Status |
|----------|--------|
| Test suite | 585 tests (unit, property-based, integration) |
| Coverage | 90%+ enforced |
| CI/CD | GitHub Actions matrix: Python 3.10–3.13 × Ubuntu/macOS/Windows |
| Type safety | mypy strict mode |
| Linting | ruff (comprehensive rule set) |
| Documentation | README, CHANGELOG, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY |
| License | Apache 2.0 (verified compatible dependencies) |
| Secrets scan | Clean — no credentials, no internal URLs, no customer data |
| `.gitignore` | Covers all dev artifacts (db files, research docs, CLAUDE.md) |
| PyPI | Package structured and ready for trusted publishing |

---

## Requested Approval

**Action:** Make `github.com/aws/ostiari` public under Apache 2.0 license.

**Approvals needed:**
- [ ] Manager sign-off (Cynthia Mazza)
- [ ] OSPO review
- [ ] Security scan (automated)
- [ ] Legal/IP confirmation

**Timeline:** Ready for publication immediately upon approval. No additional engineering work required.

---

## Contact

- **Owner:** Harleen Kaur (harlnk@amazon.com)
- **Team:** TMEGS Media & Entertainment, Solutions Architecture
- **Location:** JFK27-CO, New York
