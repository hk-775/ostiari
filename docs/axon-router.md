# AxonLLM as Ostiari's embedded LLM router

Ostiari **governs** (auth, injection, quota, trace, HITL) and delegates the
**routing of the actual model call** to AxonLLM's in-process `GatewayAgent`.
AxonLLM owns model/provider selection, health-aware fallback, cost tracking,
smart (task-classification) routing, and ensemble. There is **no extra network
hop** — it is one Python call inside the gateway process.

```
Claude Code / caller
   │  ① HTTP → Ostiari gateway   (govern: auth, injection, quota, trace)
   │      in-process (no network):
   │        AxonRouter → build_gateway_agent().handle_chat_completion(req, ctx)
   │          → smart / ensemble / health-aware fallback, picks model + provider
   │  ② the ONE outbound LLM call (Anthropic | Bedrock | OpenAI | …)
   ◀── response → (translated) → caller
```

Two hops total, both irreducible (client→Ostiari, Ostiari→LLM). AxonLLM sits in
the middle as embedded code, not a service.

## Where it's wired

**Both** the `/invoke` own-the-loop path *and* the Claude Code `/v1/messages`
shim — AxonLLM is the single routing authority across the gateway. Each model
call goes through `AxonRouter` when available; otherwise the direct provider
path runs (graceful fallback).

- **`/invoke`** — full delegate, all modes (fallback / smart / ensemble) — except
  rounds that carry tool specs, which go direct to the provider (below).
- **`/v1/messages` shim** — routes through AxonLLM in **single-response mode
  (ensemble disabled)**, since Claude Code needs exactly one Anthropic response
  per call to drive its tool loop. AxonLLM's OpenAI-shaped result is translated
  back to Anthropic Messages format (and re-emitted as Anthropic SSE when
  streaming). This means the shim's streaming is buffered-then-chunked rather
  than token-by-token — the tradeoff for a single routing authority that also
  gives the shim AxonLLM's cost tracking, model access control, and
  health-aware fallback.

### Model names

AxonLLM selects from its own registry (e.g. `claude-sonnet`), which does not use
Anthropic's dated IDs (`claude-sonnet-4-6`). When a caller sends a concrete model,
`AxonRouter.knows_model()` checks AxonLLM's registry: if known, it's honored; if not,
the call **smart-routes** so AxonLLM picks a model it can actually serve. The client's
requested model is advisory once AxonLLM is the authority.

Both `/invoke` and the shim apply this guard. `/invoke` previously passed a configured
default straight through, which 404'd inside AxonLLM whenever that default was a dated
ID its registry didn't carry.

### Tool calls bypass AxonLLM

AxonLLM has **no tool-calling pass-through**: `ChatCompletionRequest` has no `tools`
field, so a `tools` key in the request dict is dropped without error. The model is
simply never told any tools exist and answers confidently that it has no database
access — a response that looks successful and isn't.

So an agentic round carrying tool specs goes **direct to the provider**, and
`AxonRouter.route()` raises rather than silently dropping them. `supports_tools()`
probes the dataclass for the field instead of hardcoding `False`, so this reverts to
AxonLLM routing on its own once AxonLLM gains the field.

The practical consequence: tool-using `/invoke` traffic doesn't get AxonLLM's smart
routing or ensemble today. Tool-free traffic and the shim still do.

### Errors are not silent

AxonLLM signals failure by *returning* `{"error": …, "status_code": …}` rather than
raising. Such a payload has no `choices`, so parsing it optimistically yielded
`content=""` with 0 tokens — an empty HTTP 200 that read as a successful call.
`_to_result` now raises on it, so the caller falls back to the direct provider path.

## Routing modes (opt-in via `/invoke` context)

`POST /invoke` body `context` flags select the mode (matching AxonLLM's contract):

| Mode | Trigger | Behavior |
|---|---|---|
| **Fallback** | a concrete `model` | health-aware fallback across that model's backends |
| **Smart** | `context.smart_routing = true` (or empty model) | task classifier → best model |
| **Ensemble** | `context.ensemble = true` or `"<preset>"` | fan out to a panel, synthesize |

```bash
# smart routing
curl -X POST localhost:8421/invoke -d '{"messages":[...],"context":{"smart_routing":true}}'
# ensemble (default preset, or a named preset string)
curl -X POST localhost:8421/invoke -d '{"messages":[...],"context":{"ensemble":true}}'
```

## Install / embed

AxonLLM is installed editable alongside the gateway (its package root is
`src.gateway`), plus `tiktoken`:

```bash
uv pip install -e ../AxonLLM tiktoken
```

`AxonRouter` locates AxonLLM's `config/` dir from the installed package and
transiently `chdir`s there while building the agent (AxonLLM resolves its config
files relative to cwd), then restores cwd. Override the root with
`OSTIARI_AXON_ROOT` if needed.

## Controls / fallback

- On startup you'll see `AxonLLM router embedded — GatewayAgent routing active
  (root=…)` or `AxonLLM router unavailable (…) — falling back to direct provider
  calls`.
- `OSTIARI_DISABLE_AXON_ROUTER=1` forces the direct provider path (used by the
  deterministic `/invoke` unit tests).
- If AxonLLM or its config/creds are absent, the gateway still runs — routing
  falls back to the direct provider path. Nothing crashes.

## Notes

- AxonLLM's provider layer uses the ambient cloud credentials — in this
  environment it routed to **Bedrock** (`us.anthropic.claude-*`). Which provider
  serves a model is AxonLLM's registry/config decision.
- Governance is unaffected: Ostiari still runs auth/injection/quota/trace around
  the routed call; only model/provider *selection and execution* is delegated.
