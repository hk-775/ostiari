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

## AxonLLM is optional to install, but load-bearing when absent

Routing governance and **token cost tracking** happen inside AxonLLM. A gateway
running without it answers every request and reports healthy while enforcing none
of that — which is precisely how it ran unnoticed for a while (see *Import
ordering* below). That invisibility is the actual defect, so the dependency is
checked once, loudly, at startup:

```
WARNING AxonLLM could not be embedded (ModuleNotFoundError: No module named
'src'): routing governance and token cost tracking happen in AxonLLM, so LLM
calls return 200s while enforcing neither. Install AxonLLM (pip install -e
/path/to/AxonLLM) or point OSTIARI_AXON_ROOT at its checkout. Continuing WITHOUT
AxonLLM: LLM calls take the direct provider path with NO routing governance and
NO token cost tracking. GET /health reports llm_router for the machine-readable
version. Set OSTIARI_REQUIRE_AXON=1 to refuse to start instead.
```

A warning and not a refusal, because AxonLLM is a separate private repo and isn't
on PyPI: requiring it makes it a *deployment* dependency of every gateway, CI
runner, and contributor checkout — including the ones that only ever proxy tools
and never make an LLM call. **Set `OSTIARI_REQUIRE_AXON=1` in production**, where
silently ungoverned LLM traffic is not an acceptable degradation.

`GET /health` reports the state under `llm_router`, because "the gateway is up"
and "LLM calls are governed" are different facts and nothing else in that payload
distinguishes them:

```json
"llm_router": {"embedded": true, "root": "/path/to/AxonLLM",
               "governed": true, "cost_tracking": true, "tools": true}
```

`/invoke` and `/v1/messages` also keep a direct-provider fallback for a *mid-flight*
failure (one call, logged as a warning), which is not a supported way to run the
gateway. `/v1/chat/completions` deliberately has none.

## Where it's wired

All three LLM entry points — the `/invoke` own-the-loop path, the Claude Code
`/v1/messages` shim, and the Codex `/v1/chat/completions` shim. AxonLLM is the
single routing authority across the gateway; every model call goes through
`AxonRouter`, tool-bearing or not.

- **`/invoke`** — full delegate, all modes (fallback / smart / ensemble).
- **`/v1/messages` shim** — routes through AxonLLM in **single-response mode
  (ensemble disabled)**, since Claude Code needs exactly one Anthropic response
  per call to drive its tool loop. AxonLLM's OpenAI-shaped result is translated
  back to Anthropic Messages format (and re-emitted as Anthropic SSE when
  streaming). This means the shim's streaming is buffered-then-chunked rather
  than token-by-token — the tradeoff for a single routing authority that also
  gives the shim AxonLLM's cost tracking, model access control, and
  health-aware fallback.
- **`/v1/chat/completions` shim** — same single-response mode, but already in
  AxonLLM's own OpenAI wire format, so no translation on either leg. This is the
  only path with **no** direct-provider fallback: it returns 503 when AxonLLM is
  unavailable rather than answering ungoverned.

### Model names

AxonLLM selects from its own registry (e.g. `claude-sonnet`), which does not use
Anthropic's dated IDs (`claude-sonnet-4-6`). When a caller sends a concrete model,
`AxonRouter.knows_model()` checks AxonLLM's registry: if known, it's honored; if not,
the call **smart-routes** so AxonLLM picks a model it can actually serve. The client's
requested model is advisory once AxonLLM is the authority.

Both `/invoke` and the shim apply this guard. `/invoke` previously passed a configured
default straight through, which 404'd inside AxonLLM whenever that default was a dated
ID its registry didn't carry.

### Tool calls route through AxonLLM

Tool specs are carried through AxonLLM and translated into each provider's own
dialect, so tool-using traffic gets the same routing governance and cost tracking
as everything else. `AxonRouter.route()` forwards `tools` OpenAI-shaped.

This was not always true, and the failure was silent: `ChatCompletionRequest` had
no `tools` field, so a `tools` key in the request dict was dropped without error.
The model was never told any tools existed and answered confidently that it had no
database access — a response that looks successful and isn't. It was fixed at the
source (in AxonLLM) rather than worked around, because every workaround meant
routing that traffic outside the governed path.

`supports_tools()` remains as a **version guard** — Ostiari doesn't pin an AxonLLM
version, so it probes the dataclass for the field rather than assuming. Against an
older checkout:

- `/invoke` and the `/v1/messages` shim log a warning naming what's bypassed and
  take the direct provider path, so the tool loop keeps working;
- `/v1/chat/completions` (Codex) has no direct-provider fallback, so it returns
  **501** rather than a fluent tool-free answer.

See AxonLLM's own `tests/unit/adapters/test_tool_translation.py` for the
per-dialect coverage. The translations are not symmetric:

| | Tool spec | Assistant call | Tool result | Signals a call via |
|---|---|---|---|---|
| **OpenAI** | `tools[].function.parameters` | `tool_calls[]` | `role:"tool"` | `finish_reason:"tool_calls"` |
| **Anthropic** | `tools[].input_schema` | `tool_use` block | `tool_result` block | `stop_reason:"tool_use"` |
| **Bedrock Converse** | `toolConfig..toolSpec` | `toolUse` block | `toolResult` block | `stopReason:"tool_use"` |
| **Gemini** | `functionDeclarations` | `functionCall` part | `functionResponse` part | *the part itself* — `finishReason` stays `STOP` |
| **Cohere** | `parameter_definitions` | history `tool_calls` | top-level `tool_results` | `tool_calls` present |

Two cross-cutting details bit during implementation and are worth knowing:

- **arguments encoding** — OpenAI carries tool arguments as a JSON *string*; every
  other dialect uses an object. Callers `json.loads()` the field, so it is
  re-encoded at each boundary. Malformed model output yields `{}` rather than
  failing the request — the tool reports the bad call.
- **Gemini schema filtering** — Gemini *rejects* unknown JSON Schema keys
  (`additionalProperties`, `$schema`, `title`, `default`) instead of ignoring
  them, so schemas are filtered recursively rather than passed through.

The tool list is also part of AxonLLM's cache key now: the same prompt sent with
tools can return a tool call and sent without them returns prose, so omitting them
served a cached tool-free reply to a request that needed a tool call.

### Import ordering (why the router silently never loaded)

AxonLLM's modules import each other as `src.gateway.*`, but its editable install
puts `<root>/src` on `sys.path` — which makes `gateway` importable and
`src.gateway` **not**. `_ensure()` used to `import src.gateway` to locate the
checkout, then call `_axon_root()`, which imported `src.gateway` again: a
chicken-and-egg that could never succeed. `available` was therefore always False,
every call took the direct-provider fallback, and nothing looked wrong.

The fix finds the root with `importlib.util.find_spec("gateway")` — no import — and
inserts it into `sys.path` *before* importing. This is what the startup
requirement above is guarding against recurring.

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
`src.gateway`), plus `tiktoken`. The gateway boots without it (warning above), and
`/v1/chat/completions` is the one endpoint that hard-requires it, but treat this as
a required step for any deployment that makes LLM calls:

```bash
uv pip install -e ../AxonLLM tiktoken
```

`AxonRouter` locates AxonLLM's repo root (which holds its `config/` dir) via
`importlib.util.find_spec`, without importing it, then transiently `chdir`s there
while building the agent (AxonLLM resolves its config files relative to cwd) and
restores cwd. Override the root with `OSTIARI_AXON_ROOT` if needed.

## Controls / fallback

| Variable | Effect |
|---|---|
| `OSTIARI_AXON_ROOT` | Point at AxonLLM's checkout when auto-detection can't find it. |
| `OSTIARI_DISABLE_AXON_ROUTER=1` | Force the direct provider path — LLM calls then run **ungoverned and untracked**. Used by the deterministic `/invoke` unit tests. |
| `OSTIARI_REQUIRE_AXON=1` | Refuse to start when AxonLLM can't embed, instead of warning. **Set this in production.** |

On startup you'll see `AxonLLM embedded — routing governance active (root=…)`, or
a warning naming what is no longer being enforced. Check `GET /health` →
`llm_router` to confirm from outside the process.

## Notes

- AxonLLM's provider layer uses the ambient cloud credentials — in this
  environment it routed to **Bedrock** (`us.anthropic.claude-*`). Which provider
  serves a model is AxonLLM's registry/config decision.
- Governance is unaffected: Ostiari still runs auth/injection/quota/trace around
  the routed call; only model/provider *selection and execution* is delegated.
