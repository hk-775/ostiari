# Per-agent LLM routing (round-robin across models)

Rotate one agent's calls across several LLMs — e.g. `claude-code` round-robins
across `claude-sonnet-4-6` and `gpt-4o`. Configured in the control plane,
**enforced at the gateway** on every `/v1/messages` (shim) and `/invoke` call.

## Two layers of "routing" — don't confuse them

| Layer | Question | Where |
|---|---|---|
| **Model selection** (this feature) | *Which LLM* does this agent's call use? | Ostiari `ModelRouter` |
| Backend load-balancing | For a *chosen* model, which replica/region/key? | AxonLLM (round-robin/weighted/least-latency across a model's backends) |

The strategies on the Models page (`round-robin`, `least-latency`, …) are the
**second** layer — balancing one model across its provider backends. This
feature is the **first** layer: choosing *which model* per agent. "Round-robin
across different LLMs" only makes sense here.

## How selection is prioritized

`ModelRouter.select_model` order:

1. **Per-agent routing policy** (this feature) ← highest
2. A/B experiments
3. Explicit routing rules
4. Smart routing (task classification)
5. Default model

So an agent opted into round-robin always rotates, regardless of other rules.

## Scope: per-request vs per-session

- **`request`** — advance the rotation on *every* call (true round-robin; good
  for stateless / batch / cost-spreading).
- **`session`** — all calls sharing an `X-Session-Id` use one model; the
  rotation advances between sessions. Better for an interactive coding agent so
  it doesn't switch models mid-conversation. With no session id on the request,
  `session` degrades to `request` scope rather than pinning everything to one
  model.

Policies are keyed per `agent_id`; a `"*"` key applies to any agent without a
specific entry. A single-model list acts as a pin.

## Configure it

**Control plane UI:** Agents page → **LLM Round-Robin (live)** → pick agent,
gateway, scope, add models, **Save & Push**.

**HTTP (control plane, persists + pushes to the gateway):**

```bash
curl -X POST http://localhost:8400/api/agent-routing -H 'Content-Type: application/json' -d '{
  "agent_id": "claude-code",
  "gateway_id": "crm-agent",
  "strategy": "round_robin",
  "models": ["claude-sonnet-4-6", "gpt-4o"],
  "scope": "session"
}'
```

The control plane pushes the gateway's full agent-routing map to
`POST /config/agent-routing` — a **partial** update that never touches the
gateway's provider credentials.

**Gateway (direct):** `POST /config/agent-routing` with
`{"agent_routing": {"claude-code": {"strategy": "round_robin", "models": [...], "scope": "request"}}}`.
`GET /config/agent-routing` returns the current policies.

## Caveats

- Every model in the list must be a valid, credentialed model on that gateway
  (a bad model ID returns the provider's error for that one call — rotation
  itself is unaffected).
- Cross-provider models (e.g. `gpt-4o` alongside Claude) work because the shim
  translates to/from Anthropic format — but for an interactive coding agent,
  rotating across *families* can produce inconsistent behavior; prefer
  `session` scope or same-family models there.
- Control-plane policies are held in memory (like the other config routers), so
  they need re-applying after a control-plane restart. They are keyed per org, so
  a policy set in one tenant doesn't leak into another.
- The **Codex shim** (`POST /v1/chat/completions`) does not apply per-agent
  routing — it forwards the requested model to AxonLLM as-is. Only `/v1/messages`
  and `/invoke` go through `ModelRouter.select_model`.
- Only `strategy: "round_robin"` is implemented; a policy with any other strategy
  is ignored and selection falls through to A/B, rules, smart routing, and default.
