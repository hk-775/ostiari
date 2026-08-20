# Per-agent LLM routing

Ostiari and AxonLLM own different routing layers:

| Layer | Question | Owner |
|---|---|---|
| Model policy | Which logical model should this agent use? | Ostiari |
| Provider route | Which credential, endpoint, region, or backend serves that model? | Embedded AxonLLM |

This separation keeps tenant policy and experiment assignment in Ostiari while
AxonLLM performs provider-aware routing, health tracking, and fallback.

## Ostiari model-selection order

`ModelRouter.select_model()` applies:

1. per-agent round-robin policy;
2. enabled A/B experiments;
3. explicit operator routing conditions;
4. operator-defined keyword categories;
5. the configured default model.

The selected name is then handed to AxonLLM. If it is not a known concrete
model in the Axon catalog, Axon performs smart routing through its embedded
public API.

## Endpoint scope

| Endpoint | Ostiari model policy |
|---|---|
| `POST /invoke` | Applies the complete selection order. |
| `POST /v1/messages` | Applies the complete selection order. |
| `POST /v1/chat/completions` | Uses the requested model when known; otherwise Axon smart-routes. |
| `POST /v1/responses` | Same model behavior as Chat Completions after request translation. |

Per-agent round-robin and A/B assignment therefore do not override an explicit
model on the OpenAI-compatible endpoints.

## Round-robin policy

Policies are keyed by `agent_id`; `*` is the fallback policy. A one-model list
acts as a pin.

```json
{
  "agent_routing": {
    "claude-code": {
      "strategy": "round_robin",
      "models": ["claude-sonnet-4-6", "gpt-4o"],
      "scope": "session"
    }
  }
}
```

Scopes:

- `request`: advance on every call;
- `session`: keep one model for the same `X-Session-Id`, then advance for a new
  session; without a session id it behaves like request scope.

Only `round_robin` is implemented. Unknown strategy names are ignored and
selection continues through experiments, rules, keyword categories, and the
default.

## Configuration

The control plane pushes the full routing map to:

```text
POST /config/agent-routing
```

`GET /config/agent-routing` returns the current map. This is a partial
configuration endpoint and does not replace provider credentials or tools.

The control-plane UI exposes the same operation on the Agents page under
**Per-Agent Model Routing**.

## Operational requirements

- Every configured model must be known or intentionally smart-routable by the
  embedded Axon catalog.
- Every selected model needs at least one funded, healthy provider route.
- Interactive agents should prefer session scope to avoid changing model
  families mid-conversation.
- Policies are tenant-scoped and included in reconnect configuration bundles.
- Production router failures fail closed; they never switch to the diagnostic
  direct-provider path.
