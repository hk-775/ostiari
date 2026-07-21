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

The **`/invoke`** own-the-loop path (`AgenticExecutor`). Each model call goes
through `AxonRouter` when available; otherwise the direct provider path runs
(graceful fallback). The interactive Claude Code **`/v1/messages` shim** keeps
its direct/translation path — ensemble in particular does not fit there (that
path must return exactly one Anthropic response per call to drive Claude Code's
tool loop).

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
