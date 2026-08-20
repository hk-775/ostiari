# AxonLLM as Ostiari's embedded router

Ostiari bundles AxonLLM `v0.3.1` and runs its public `AsyncRouter` API inside
the gateway process. There is no Axon service and no additional network hop.

```text
client
  -> Ostiari gateway
       identity -> authorization -> content security -> quota/budget
       -> embedded AxonLLM routing
       -> provider API
       -> usage + trace reconciliation
  <- governed response
```

The exact upstream tag, commit, license, and refresh procedure are recorded in
[`vendor/axonllm/UPSTREAM.md`](../vendor/axonllm/UPSTREAM.md). The upstream
MIT-0 license and third-party notices are retained beside the source.

## Ownership boundary

Ostiari owns:

- verified agent identity and tenant boundaries;
- model/provider authorization;
- prompt-injection and PII controls;
- request, token, and budget quotas;
- human approval and tool governance;
- durable usage, cost, trace, and lifecycle reporting;
- control-plane configuration and encrypted route credentials.

AxonLLM owns the in-process routing data plane:

- model and concrete provider-route selection;
- health-aware provider fallback;
- smart and ensemble routing;
- provider-dialect translation, including tool calls;
- provider connection pools and route health.

Ostiari does **not** initialize AxonLLM's standalone server, identity service,
database, admin API, or background workers.

## Installation and packaging

A source checkout is self-contained:

```bash
make install
```

That installs:

1. the Ostiari core package;
2. the pinned `vendor/axonllm[server]` distribution;
3. the gateway and control plane.

The production gateway image installs the same source snapshot and copies its
routing configuration to `/opt/axonllm`. CI verifies the installed package
version, constructs the real router, and exercises the gateway suite with Axon
enabled.

The standalone `ostiari-gateway` wheel cannot name an unpublished local path in
Python package metadata. If wheels are distributed separately, install the
matching `axon-llm` wheel from the same release bundle beside it. Source and
container installations do this automatically.

## Production behavior

The LLM module is optional; tool-only gateways do not construct AxonLLM.

When `llm_gateway` is enabled:

- `OSTIARI_ENV=production` makes AxonLLM mandatory automatically;
- `OSTIARI_REQUIRE_AXON=1` applies the same fail-closed rule in other
  environments;
- startup fails when the bundled package or configuration cannot initialize;
- a mid-flight Axon routing failure is returned as an error, not sent through a
  direct-provider bypass.

Development can deliberately exercise the diagnostic direct-provider path with
`OSTIARI_DISABLE_AXON_ROUTER=1`. That mode is not a supported production
posture.

`GET /health` reports the router contract:

```json
{
  "llm_router": {
    "embedded": true,
    "governed": true,
    "cost_tracking": true,
    "tools": true,
    "root": "/opt/axonllm"
  }
}
```

Alert on `llm_router.governed` for an LLM-enabled gateway, not only the
top-level process health.

## Governed API surfaces

All four LLM surfaces use the same embedded router:

| Endpoint | Contract |
|---|---|
| `POST /invoke` | Ostiari owns the full model/tool loop; smart and ensemble modes are available. |
| `POST /v1/messages` | Anthropic-compatible, single-response routing for clients that own their tool loop. |
| `POST /v1/chat/completions` | OpenAI Chat Completions compatibility. |
| `POST /v1/responses` | Stateless OpenAI Responses compatibility for text, image URL, and function-tool input. |

The compatibility endpoints keep client-owned tool loops intact. Streaming is
translated into the endpoint's wire format after the single routed response;
it is not upstream token passthrough.

The Responses endpoint rejects unsupported stateful/background fields such as
`previous_response_id`, `conversation`, `prompt`, `store=true`, and
`background=true`. It never silently ignores them.

## Routing modes

`POST /invoke` can select:

| Mode | Trigger |
|---|---|
| Fallback | a concrete known model |
| Smart | `context.smart_routing=true` or no model |
| Ensemble | `context.ensemble=true` or a named preset |

Ostiari applies agent policy, A/B experiments, configured rules, and defaults
before the Axon call where those controls are part of the endpoint contract.
Axon then selects a routable model/provider mapping and concrete route.

## Model and provider configuration

The initial router reads the bundled Axon files:

- `config/models.yaml`
- `config/providers.yaml` (or the bundled example)
- `config/pricing.yaml`

These can be overridden with:

| Variable | Purpose |
|---|---|
| `OSTIARI_AXON_ROOT` | Compatible Axon config/source root; normally unnecessary. |
| `AXON_MODELS_CONFIG` | Model registry file. |
| `AXON_PROVIDERS_CONFIG` | Provider mapping file. |
| `AXON_PRICING_CONFIG` | Pricing file. |
| `AXON_BEDROCK_REGION` | Bedrock region override. |

The control plane can atomically replace the model catalog and concrete
provider routes without restarting the gateway. Runtime snapshots omit
credentials and private headers.

## Tool calls

Ostiari forwards OpenAI-shaped tool definitions to AxonLLM. Axon translates
them to the selected provider and normalizes tool calls back to Ostiari. The
gateway refuses tool-bearing traffic if a substituted Axon package lacks the
required tool fields; returning fluent tool-free output would be a silent
contract violation.

## Current compatibility boundary

AxonLLM 0.3.1 exposes ordinary chat completions through its public API. Smart
and ensemble strategy configuration still requires one isolated compatibility
helper in `axon_router.py`. All request routing, catalog updates, route updates,
availability queries, and shutdown use the public embedded API. The helper is
covered by the exact-version pin and must be removed when upstream exposes
those strategy hooks publicly.
