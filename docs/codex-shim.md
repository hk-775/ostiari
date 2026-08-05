# Codex CLI shim — governed `/v1/chat/completions`

The gateway exposes an OpenAI-compatible **`POST /v1/chat/completions`** endpoint,
so OpenAI Codex CLI (and any OpenAI-SDK client) can route through Ostiari for
governance and multi-provider routing — the OpenAI-format sibling of the Claude
Code `/v1/messages` shim.

## Point Codex at it

Codex CLI uses a custom provider in `~/.codex/config.toml`:

```toml
model_provider = "ostiari"
model = "gpt-4o"                       # or any model your gateway routes to

[model_providers.ostiari]
name = "Ostiari"
base_url = "http://localhost:8421/v1"  # your Ostiari gateway + /v1
env_key = "OSTIARI_KEY"                # any value; the gateway holds real creds
wire_api = "chat"
```

Codex then calls `POST http://localhost:8421/v1/chat/completions`. The gateway
holds the real provider credentials; the client sends no real key.

Optional headers Ostiari reads for attribution:
- `X-Agent-Id` — which agent/principal this traffic belongs to
- `X-Session-Id`, or `x-codex-session-id` as a fallback — groups a session's calls in traces
- `X-Framework` — defaults to `codex`

## What Ostiari does per call

Same gate chain as the messages shim, in the OpenAI wire format (no cross-format
translation needed — AxonLLM is OpenAI-shaped throughout):

1. **Agent authorization** — endpoint grant + per-agent model/provider/budget (`authorize_llm`). Failure → **403**.
2. **Injection / PII** — detection-only and fail-closed: it blocks on a detection, and also blocks when PII is *present* rather than redacting it, since the redaction would desynchronize Codex's own conversation state. Failure → **403**.
3. **Quota** — Ostiari's own budget ceiling, plus the rate limit and model allowlist from the pushed quota. The cost estimate is booked as an in-flight reservation so concurrent calls can't all pass on a stale spend total. Failure → **429**.
4. **Routing** — AxonLLM selects model + provider (smart routing when the client's model isn't in the registry), health-aware fallback, cost tracking. Single-response mode — ensemble stays on `/invoke`.
5. **Trace** — one `llm.chat` event to the control plane (model, tier, tokens, routed flag).

Returns a standard OpenAI **ChatCompletion** (or an OpenAI **SSE stream** —
`chat.completion.chunk` deltas ending in `data: [DONE]`) when `stream: true`.

## Notes / limitations

- **Per-agent routing policies do not apply here.** `ModelRouter.select_model` —
  which is what reads the routing policies set on the control plane's Agents page,
  along with A/B experiments and explicit routing rules — is called only from the
  `/v1/messages` shim and the `/invoke` executor. On this endpoint the model comes
  from the client's request and then from AxonLLM's own smart routing. See
  [agent-llm-routing.md](agent-llm-routing.md).
- Streaming is buffered-then-chunked (correct OpenAI SSE events, not token-by-token from upstream) — same tradeoff as the shim's cross-provider path.
- Tool calls route **through** AxonLLM, which translates the specs into the target
  provider's dialect and translates the call back into OpenAI `tool_calls`; Codex
  runs the tools in its own loop. Since the wire format here is already OpenAI's,
  a call that lands on an OpenAI-style provider is a pass-through — one that lands
  on Bedrock, Anthropic, Gemini, or Cohere is translated. See
  [axon-router.md](axon-router.md#tool-calls-route-through-axonllm).
- Requires the embedded AxonLLM router (`src.gateway`). The gateway itself boots
  without it (it logs a warning; set `OSTIARI_REQUIRE_AXON=1` to refuse to start
  instead, which is the right setting in production), but *this endpoint* is
  unusable without it — it returns 503 when the router is absent or goes down
  mid-flight. `GET /health` reports `llm_router` for the machine-readable version.
  Unlike `/invoke` and `/v1/messages` it has **no** direct-provider fallback, so a
  tool-bearing call against an AxonLLM too old to carry tool specs returns **501**
  rather than a fluent answer from a model that was never told the tools exist.
