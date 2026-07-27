# Claude Code shim — governed `/v1/messages` with cross-provider routing

The gateway exposes an Anthropic-compatible `POST /v1/messages` endpoint. Point
Claude Code (or any Anthropic-SDK client) at an Ostiari gateway instead of
`api.anthropic.com`, and every model call flows through Ostiari's governance and
routing on the way to a provider — without Ostiari touching the client's own
tool loop.

```
Claude Code ──▶ Ostiari gateway /v1/messages ──▶ (routed by embedded AxonLLM) ──▶ Anthropic / OpenAI / Azure / Bedrock
                  │  auth → injection → quota → AxonLLM routing → trace
                  ▼
             control plane (traces, spend)
```

## Why a shim (and not `/invoke`)

Ostiari's native `/invoke` *owns* the agentic loop — it calls the model **and
executes the tools itself**. Claude Code runs its own loop: the model returns
`tool_use` blocks, Claude Code executes them locally (Bash, Edit, …) and calls
back. So `/invoke` can't sit under Claude Code. The shim is a **governed
passthrough**: it inspects, routes, and meters the request, then returns the
model's `tool_use` blocks untouched so Claude Code keeps driving its own tools.

## Point Claude Code at it

The gateway holds the provider credentials — the client sends no real key.

```bash
export ANTHROPIC_BASE_URL="http://localhost:8421"   # your Ostiari gateway
export ANTHROPIC_API_KEY="unused-placeholder"        # gateway uses its own key
claude
```

Optional headers Ostiari reads for attribution:

- `X-Agent-Id` — which agent/principal this traffic belongs to (default `unknown`)
- `X-Session-Id` — groups a run's calls in traces
- `X-Framework` — defaults to `claude-code`

## What Ostiari does to each call

1. **Agent authorization** — `agent_auth.check(agent_id, "/v1/messages")`.
2. **Prompt-injection detection** — detection-only; blocks but never rewrites
   the forwarded body (rewriting would corrupt tool round-trips).
3. **Routing** — the embedded **AxonLLM** router selects model + provider,
   enforces model access, tracks cost, and does health-aware fallback. Run in
   single-response mode (ensemble stays on `/invoke`), since Claude Code needs
   exactly one Anthropic response per call to drive its tool loop.
4. **Quota / budget** — pre-call projection; blocks with a `rate_limit_error`
   when over budget; records spend from response usage.
5. **Trace** — one `llm.messages` event to the control plane per call, with
   model, tier, token counts, and whether it was re-routed.

## Cross-provider routing

Every call — tool-bearing or not — routes through AxonLLM, which is the single
routing authority across the gateway and a required dependency (see
[axon-router.md](axon-router.md)). AxonLLM's OpenAI-shaped result is translated
back into an **Anthropic Messages object**, re-emitted as valid Anthropic SSE when
the client asked to stream, so Claude Code sees Anthropic format regardless of
which provider served the call. Tool specs and `tool_use`/`tool_result` blocks are
translated per provider inside AxonLLM.

The tradeoff: streaming on this path is buffered-then-chunked rather than
token-by-token, which is the cost of having one routing authority that also gives
the shim cost tracking, model access control, and health-aware fallback.

**Degraded path.** If AxonLLM fails *mid-flight*, the shim falls back to Ostiari's
own `ModelRouter` + a direct provider call for that one call, logged as a warning:

- **Anthropic target** → raw SSE passthrough. True end-to-end streaming,
  byte-for-byte fidelity (httpx auto-decompresses; we relay decoded SSE).
- **Other provider** (OpenAI / Azure / Bedrock) → Ostiari translates the Anthropic
  request itself (including `tool_use`/`tool_result` round-trip blocks and tool
  schemas) and translates the response back.

Tool names with dots (`fs.delete`) are sanitized to `fs_delete` for OpenAI's
name regex and restored on the way back.

## Configuration

The endpoint activates when the LLM Gateway module is on:

```yaml
# llm-gateway-config.yaml
modules:
  llm_gateway: true
llm:
  default_model: claude-sonnet-4-6
  routing_rules:
    - condition: "task_type == 'code'"
      model: claude-sonnet-4-6
    - condition: "task_type == 'chat'"
      model: claude-haiku-4-5-20251001
  credentials:
    anthropic: ${ANTHROPIC_API_KEY}   # or set ANTHROPIC_API_KEY in the gateway env
    # openai / azure_* / bedrock_region for cross-provider targets
```

Env overrides:

- `ANTHROPIC_API_KEY` — used if `credentials.anthropic` is unset.
- `OSTIARI_ANTHROPIC_BASE_URL` — override the upstream Anthropic base (default
  `https://api.anthropic.com`).

## Limitations

- Routing to a non-Anthropic provider requires that provider's SDK and
  credentials to be present in the gateway.
- Cross-provider responses are buffered upstream (one provider call) then
  streamed to the client as Anthropic SSE — correct event semantics, but not
  token-by-token from the upstream provider. Anthropic-target streaming *is*
  token-by-token.
