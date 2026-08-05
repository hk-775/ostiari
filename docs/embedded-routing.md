# Embedded AxonLLM routing (smart routing)

Ostiari's gateway embeds **AxonLLM** as its routing engine. AxonLLM ships a
task classifier, smart routing, ensemble, and multi-provider adapters; the
gateway imports it as the `src.gateway.*` package and uses its `TaskClassifier`
to route by prompt content.

## Why this doc exists

The embed was coded but silently broken — twice, the same way. AxonLLM imports
itself as `src.gateway.*`, but its editable install puts `<root>/src` on
`sys.path`, which makes `gateway` importable and `src.gateway` **not**. So a
`try/except ImportError` around `import src.gateway` always fell through and the
embed quietly no-op'd: first for the classifier (smart routing silently
disabled), then again in `AxonRouter`, where it meant **every** LLM call took the
direct-provider fallback with no AxonLLM cost tracking or routing governance —
and nothing looked wrong.

Both are fixed. `_prepare_axon_path()` (in `axon_router.py`) locates the repo root
with `importlib.util.find_spec("gateway")` — no import — and inserts it into
`sys.path` *before* importing; `ModelRouter` and `AxonRouter` share it. Startup
logs which mode is active, and the router's state is visible in `GET /health`.

## Install (embed AxonLLM)

AxonLLM is not on PyPI; install it editable alongside the gateway, plus its
`tiktoken` dependency:

```bash
uv pip install -e ../AxonLLM tiktoken
# or: pip install -e ../AxonLLM tiktoken
```

The gateway declares `tiktoken` under its `routing` extra. On startup you'll see
one of:

- `AxonLLM TaskClassifier embedded — smart routing active`
- `AxonLLM not importable (...) — smart routing disabled, falling back to rules/default`

**Optional to install, but don't skip it in production.** Routing governance and
token cost tracking happen inside AxonLLM, and the direct-provider fallback is
good enough that a gateway without it serves traffic and reports healthy while
enforcing neither. So with `llm_gateway` enabled, a gateway that starts without
AxonLLM logs a warning naming what stopped applying, and `GET /health` reports
`llm_router` for anything reading machine-side. Set `OSTIARI_REQUIRE_AXON=1` to
refuse to start instead — the right setting for production, where silently
ungoverned LLM traffic is not an acceptable degradation. See
[axon-router.md](axon-router.md).

The classifier line above is a *narrower* degradation: if the classifier alone
fails to import while the router embeds fine, model selection falls back to
explicit rules + the default model and the call still routes through AxonLLM.

## How smart routing selects a model

`ModelRouter.select_model` priority (highest first):

1. Per-agent routing policy (round-robin across models)
2. A/B experiments
3. Explicit rules (`condition -> model`)
4. **Smart routing** — AxonLLM classifies the last user message into a
   `task_type` (`coding`, `creative_writing`, `summarization`, `general`, …);
   a routing rule of the form `task_type == 'coding'` maps that class to a model
5. Default model

**Where this ladder runs:** `select_model` is called from the `/v1/messages` shim
and the `/invoke` executor only. The Codex shim (`/v1/chat/completions`) takes the
client's model straight to AxonLLM, so steps 1–3 and 5 above — routing policies,
A/B experiments, explicit rules, and the configured default — do not apply on that
endpoint. AxonLLM's own smart routing still does.

So smart routing only *chooses* a model when a rule maps the classified
`task_type`. Configure the mapping with routing rules:

```json
{
  "default_model": "claude-sonnet-4-6",
  "routing_rules": [
    {"condition": "task_type == 'coding'", "model": "claude-opus-4-8"},
    {"condition": "task_type == 'summarization'", "model": "claude-haiku-4-5-20251001"}
  ]
}
```

A coding prompt then routes to opus, a summarization prompt to haiku, everything
else to the default — one model per request, chosen by content.

## Note on the classifier

AxonLLM's `TaskClassifier` is keyword/heuristic based, not a model call — it's
fast and free but approximate (e.g. a trailing "?" can nudge classification).
Treat `task_type` routing as a cost/latency optimization, not a semantic
guarantee.

## Ensemble

AxonLLM's ensemble (scatter-gather-synthesize) is wired on the own-the-loop
`/invoke` path, opt-in per call via `context.ensemble` (`true` for the default
preset, or a preset name). It does **not** fit behind the Claude Code or Codex
shims, which each need exactly one response per call to drive their own tool
loops — those route in single-response mode. See
[axon-router.md](axon-router.md#routing-modes-opt-in-via-invoke-context).

## Tool calls

Tool-bearing calls route through AxonLLM like everything else; it translates the
specs into each provider's dialect. This used not to work — AxonLLM had no `tools`
field, so specs were dropped and the model answered as though no tools existed —
and it was fixed at the source rather than routed around. Details and the
per-provider dialect table are in
[axon-router.md](axon-router.md#tool-calls-route-through-axonllm).
