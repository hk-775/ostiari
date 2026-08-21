# Claude Code shim: governed `/v1/messages`

Ostiari exposes an Anthropic-compatible `POST /v1/messages` endpoint. A Claude
Code or Anthropic SDK client can point at the gateway while Ostiari owns
identity, authorization, content controls, quota, routing, cost, and trace
reporting.

```text
Claude Code
    -> Ostiari /v1/messages
    -> embedded AxonLLM
    -> Anthropic, OpenAI, Azure, Bedrock, or another configured provider
```

Ostiari does not execute Claude Code's local tools. It returns Anthropic
`tool_use` blocks so Claude Code remains the owner of its agent loop.

## Configure the client

```bash
export ANTHROPIC_BASE_URL="http://localhost:8421"
export ANTHROPIC_API_KEY="unused-placeholder"
claude
```

The gateway holds the real provider credentials. Production deployments must
also send the bearer token required by the configured OIDC gateway contract.

Optional attribution headers:

- `X-Agent-Id`
- `X-Session-Id` or `x-claude-code-session-id`
- `X-Framework` (defaults to `claude-code`)

## Governance path

Each request passes through:

1. verified agent identity and `/v1/messages` authorization;
2. per-agent model/provider permissions and token caps;
3. prompt-injection and PII enforcement;
4. gateway rate and projected-budget reservation;
5. Ostiari model policy, when configured;
6. the bundled AxonLLM router and provider adapters;
7. usage reconciliation, cost reporting, and trace reporting.

AxonLLM translates OpenAI-shaped tool definitions and results to the selected
provider's dialect, then Ostiari translates the result back to Anthropic
Messages format.

## Production failure behavior

AxonLLM `v0.3.1` is bundled with Ostiari and is mandatory whenever the LLM
module is active in production.

- Failure to initialize the router prevents production startup.
- A mid-flight router failure returns an error.
- Production never falls back to a direct provider path that bypasses routing
  governance or cost tracking.

Development can deliberately exercise the legacy diagnostic direct-provider
path with `OSTIARI_DISABLE_AXON_ROUTER=1`. That mode is observable in
`GET /health` and must not be used as a production topology.

## Streaming and tools

Governed production responses are buffered at the upstream boundary and
re-emitted as valid Anthropic SSE. The events are compatible with Anthropic
clients, but they are not token-by-token passthrough from the selected
provider.

Function definitions and `tool_use`/`tool_result` blocks remain in the client
loop. Tool names that require provider-safe normalization are restored before
the response returns to Claude Code.

## Content controls

The shim cannot silently rewrite the client's conversation without
desynchronizing its local tool loop:

- prompt injection can run in flag or block mode;
- when PII enforcement is enabled and PII is detected, the shim returns `403`;
- `/invoke`, where Ostiari owns the loop, can redact and restore content
  instead.
