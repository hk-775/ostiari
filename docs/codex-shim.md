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
- `X-Session-Id` / `x-codex-session-id` — groups a session's calls in traces
- `X-Framework` — defaults to `codex`

## What Ostiari does per call

Same gate chain as the messages shim, in the OpenAI wire format (no cross-format
translation needed — AxonLLM is OpenAI-shaped throughout):

1. **Agent authorization** — endpoint grant + per-agent model/provider/budget (`authorize_llm`).
2. **Injection / PII** — detection-only, fail-closed (blocks on fire/unavailable/error).
3. **Quota** — Ostiari's own budget ceiling.
4. **Routing** — AxonLLM selects model + provider (smart routing when the client's model isn't in the registry), health-aware fallback, cost tracking. Single-response mode — ensemble stays on `/invoke`.
5. **Trace** — one `llm.chat` event to the control plane (model, tier, tokens, routed flag).

Returns a standard OpenAI **ChatCompletion** (or an OpenAI **SSE stream** —
`chat.completion.chunk` deltas ending in `data: [DONE]`) when `stream: true`.

## Notes / limitations

- Streaming is buffered-then-chunked (correct OpenAI SSE events, not token-by-token from upstream) — same tradeoff as the shim's cross-provider path.
- Tool calls pass through in OpenAI `tool_calls` format; Codex runs them in its own loop.
- Requires the embedded AxonLLM router (`src.gateway`); returns 503 if unavailable.
