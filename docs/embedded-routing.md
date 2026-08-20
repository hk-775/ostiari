# Embedded routing

Ostiari embeds the pinned AxonLLM routing data plane inside the gateway process.
See [axon-router.md](axon-router.md) for provenance, packaging, and the security
boundary.

## Selection layers

Ostiari and AxonLLM solve different parts of routing:

1. Ostiari verifies the agent and its endpoint/model/provider grants.
2. Ostiari applies per-agent rotation, A/B experiments, explicit rules, and
   endpoint-specific model semantics.
3. AxonLLM resolves a routable model/provider mapping and concrete credential,
   endpoint, region, and connection pool.
4. Ostiari reconciles token usage, budget reservations, costs, and traces.

The split keeps tenant policy in Ostiari while using Axon's provider routing
and health state.

## Endpoint behavior

| Endpoint | Model selection |
|---|---|
| `/invoke` | Ostiari policy/A-B/rules/default, followed by Axon fallback, smart, or ensemble execution. |
| `/v1/messages` | Ostiari authorization and configured selection, followed by one Axon response. |
| `/v1/chat/completions` | The requested model is used when known; otherwise Axon smart-routes. |
| `/v1/responses` | Same governed route as Chat Completions after stateless Responses translation. |

`/v1/messages`, Chat Completions, and Responses leave tool execution to the
client. `/invoke` owns the complete model/tool loop and validates every tool
through the normal Ostiari gates.

## Explicit routing controls

Ostiari's configured selection order is:

1. per-agent routing policy;
2. enabled A/B experiment;
3. explicit routing rule;
4. default model.

Axon's smart routing is selected when the endpoint has no known concrete model
or `/invoke` requests `context.smart_routing=true`. Ensemble execution is
available only on `/invoke` through `context.ensemble`.

Example:

```yaml
modules:
  llm_gateway: true

llm:
  default_model: claude-sonnet
  routing_rules:
    - condition: "risk_tier == 'high'"
      model: claude-sonnet
  fallback_chain:
    - claude-sonnet
    - gpt-4o
```

The rule condition language is intentionally small; see
[agent-llm-routing.md](agent-llm-routing.md) for supported expressions and
endpoint-specific behavior.

## Route catalogs

A logical model can have several concrete routes. Each route can bind:

- provider and model allowlist;
- credential or ambient cloud identity;
- endpoint, region, proxy, and TLS policy;
- weight, priority, and capacity group;
- connection and request timeout policy.

The control plane pushes the complete encrypted catalog to the gateway, which
applies it atomically. Runtime health output contains no credentials.

## Failure behavior

Production LLM traffic never bypasses AxonLLM. Startup and mid-flight failures
are errors, not a direct-provider downgrade. Development can opt into the
diagnostic fallback with `OSTIARI_DISABLE_AXON_ROUTER=1`.

This distinction is important:

- a provider-route failure is handled by Axon's bounded fallback;
- an unavailable embedded router means the routing authority is gone and must
  fail closed.

## Verification

For an LLM-enabled production gateway:

1. use the production container or `make install`;
2. set `OSTIARI_ENV=production`;
3. confirm `GET /health` reports `llm_router.governed=true`;
4. exercise all enabled compatibility endpoints;
5. confirm usage, cost, and trace records reach the control plane;
6. test a router/provider failure and verify no direct bypass occurs.
