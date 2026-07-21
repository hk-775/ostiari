# Embedded AxonLLM routing (smart routing)

Ostiari's gateway embeds **AxonLLM** as its routing engine. AxonLLM ships a
task classifier, smart routing, ensemble, and multi-provider adapters; the
gateway imports it as the `src.gateway.*` package and uses its `TaskClassifier`
to route by prompt content.

## Why this doc exists

The embed was coded but silently broken: the gateway imported `gateway.*` while
AxonLLM's real package root is `src.gateway.*`, so the `try/except ImportError`
always fell through and smart routing quietly no-op'd. This is now fixed — the
imports point at `src.gateway.*`, AxonLLM is installed into the gateway
environment, and startup logs which mode is active.

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

**Graceful degradation:** if AxonLLM is absent, the gateway still runs — routing
falls back to explicit rules + the default model. Nothing crashes.

## How smart routing selects a model

`ModelRouter.select_model` priority (highest first):

1. Per-agent routing policy (round-robin across models)
2. A/B experiments
3. Explicit rules (`condition -> model`)
4. **Smart routing** — AxonLLM classifies the last user message into a
   `task_type` (`coding`, `creative_writing`, `summarization`, `general`, …);
   a routing rule of the form `task_type == 'coding'` maps that class to a model
5. Default model

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

## Ensemble (not yet wired)

AxonLLM also provides ensemble (scatter-gather-synthesize). It is **not** wired
into the gateway path yet, and it does not fit behind the Claude Code shim
(which needs one Anthropic response per `/v1/messages` to drive its tool loop).
Ensemble belongs on the own-the-loop `/invoke` path — a planned follow-up.
