# OTLP export — Ostiari governance traces in your observability stack

Ostiari's trace events carry the signal nothing else has: per-call **risk score**,
**decision tier** (allow/intervene/block), **blocked reason**, **per-agent cost**,
and the **session parent-span grouping**. This exports each event as an
OpenTelemetry span over **OTLP**, so that governance signal lands in whatever
backend you already run — Datadog, Honeycomb, Grafana Tempo, Jaeger, or an OTEL
Collector.

OTEL is the data model; OTLP is the wire protocol that carries it. Ostiari emits
OTEL-format spans and ships them via OTLP/HTTP.

## Enable it

Opt-in via the standard OTEL env vars (no-op when unset):

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="http://otel-collector:4318"
export OTEL_SERVICE_NAME="ostiari"          # optional (default: ostiari)
# plus any OTEL_EXPORTER_OTLP_HEADERS the SDK supports (auth to your backend)
```

Export happens at the control plane's `/api/traces/ingest`, so **every** gateway's
traces are exported from one place. Requires `opentelemetry-exporter-otlp-proto-http`
(installed with the control-plane extras); if it's absent, export is disabled
gracefully.

## What a span looks like

| OTEL field | From Ostiari |
|---|---|
| trace_id | the session `parent_trace_id` — a prompt's sub-calls share one trace |
| span_id | the event's own `trace_id` |
| parent | `parent_trace_id` (unset when the event is the session root) |
| name | the action (`llm.messages`, `llm.chat`, tool name) |
| status | `ERROR` when tier is `block`, else `OK` |
| start/end | the event timestamp |

Attributes (see `control_plane/services/otlp_exporter.py`):

| Attribute | Notes |
|---|---|
| `gen_ai.request.model` | GenAI semantic convention |
| `gen_ai.usage.input_tokens` | GenAI semantic convention |
| `gen_ai.usage.output_tokens` | GenAI semantic convention |
| `ostiari.action` | the tool or LLM action |
| `ostiari.agent_id` | calling agent |
| `ostiari.tier` | `allow` / `intervene` / `block` |
| `ostiari.score` | risk score, 0–100 |
| `ostiari.framework` | the agent's framework |
| `ostiari.session_id` | session grouping |
| `ostiari.routed` | whether AxonLLM routed the model choice |
| `ostiari.trace_id` | Ostiari's own event id |
| `ostiari.blocked_reason` | only present when set |
| `ostiari.limit_type` | only present when a quota fired |

The `ostiari.*` namespace exists because no OTEL standard covers these fields.

So in your existing tracing UI you get a span tree per coding-agent prompt, with
each sub-call annotated by *why* Ostiari allowed or blocked it and what it cost —
the behavioral-governance data that generic LLM gateways don't produce.

## Notes

- GenAI OTEL semantic conventions are still evolving; standard keys are used where
  they're stable, `ostiari.*` for everything governance-specific.
- Export is best-effort and never blocks or breaks trace ingestion.
- Spans are point-in-time (start == end at the event timestamp); per-call duration
  can be added once the gateway reports it end-to-end.
