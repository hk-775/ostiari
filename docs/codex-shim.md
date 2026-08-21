# OpenAI compatibility and Codex CLI status

Ostiari exposes two governed OpenAI-compatible endpoints:

| Endpoint | Contract |
|---|---|
| `POST /v1/chat/completions` | OpenAI Chat Completions compatibility for SDKs and applications that still use that API. |
| `POST /v1/responses` | Stateless Responses compatibility for text, image-URL, and function-tool input. |

Both endpoints use the same authorization, content-security, quota, embedded
AxonLLM routing, cost, and trace pipeline.

## Codex CLI compatibility

Ostiari supports **Codex CLI 0.148.0** through the reviewed profile in
[`config/codex`](../config/codex). The profile uses the Responses wire API and
deliberately advertises no reasoning, verbosity, hosted-search, service-tier,
or stateful-conversation capability. This prevents Codex from sending fields
whose semantics Ostiari cannot preserve.

Protected CI runs the exact CLI version against Ostiari's real Responses
translator and typed SSE emitter. The gate verifies:

1. stateless streamed request shape;
2. a function-call and `function_call_output` round trip;
3. final typed text events;
4. OpenAI-shaped error propagation; and
5. prompt cancellation while a stream is active.

This is a versioned compatibility contract, not a claim that arbitrary Codex
versions or configurations work. A Codex upgrade requires updating the pinned
catalog and passing the same protected conformance gate.

The profile follows the official
[Codex configuration reference](https://developers.openai.com/codex/config-reference)
and [model configuration](https://developers.openai.com/codex/models)
contracts.

### Configure Codex 0.148.0

1. Copy [`config/codex/config.toml.example`](../config/codex/config.toml.example)
   into a dedicated Codex home or profile.
2. Replace the example gateway URL and the absolute
   `model_catalog_json` path.
3. Set `OSTIARI_CODEX_TOKEN` to an Ostiari agent bearer token.
4. Verify the client version:

   ```bash
   codex --version
   # codex-cli 0.148.0
   ```

The gateway URL in the example ends in `/v1`; Codex appends `/responses`.

Stateful continuation remains unsupported. Ostiari rejects
`previous_response_id`, stored conversations, background work, hosted prompts,
model reasoning configuration, structured output, and unsupported include or
service-tier fields. The exact Codex `0.148.0` transport pair
`reasoning.context="all_turns"` plus
`include=["reasoning.encrypted_content"]` is accepted so Codex can carry its
stateless encrypted context metadata. Ostiari does not inspect, persist,
generate, or return reasoning content. Failing closed on every other shape is
intentional.

## Chat Completions clients

Clients that support Chat Completions can use:

```text
POST http://gateway:8421/v1/chat/completions
```

The gateway holds provider credentials. Callers identify themselves with the
deployment's verified bearer token; optional attribution headers are:

- `X-Agent-Id`
- `X-Session-Id`
- `X-Framework`

The response is an OpenAI `ChatCompletion`. When `stream: true`, Ostiari emits
valid `chat.completion.chunk` events ending in `data: [DONE]`. Provider output
is currently buffered before those events are emitted.

## Stateless Responses clients

The implemented Responses subset supports:

- string input or a list of message/function items;
- `instructions`;
- text and image-URL content;
- function definitions, forced function choice, calls, and outputs;
- `model`, `max_output_tokens`, `temperature`, and `top_p`;
- non-streaming Responses objects;
- typed Responses SSE events with monotonic sequence numbers.

It explicitly rejects:

- `previous_response_id`, `conversation`, and prompt references;
- `store=true` and background execution;
- model reasoning and structured-output configuration;
- file-backed images;
- non-function tools;
- include fields other than the exact Codex encrypted-reasoning transport
  field, plus unsupported service-tier and truncation modes.

## Governance path

Every supported call passes through:

1. verified agent identity and endpoint authorization;
2. model/provider authorization and per-agent caps;
3. prompt-injection and PII enforcement;
4. gateway rate and budget reservation;
5. the bundled AxonLLM router;
6. usage reconciliation, cost reporting, and trace reporting.

Production LLM traffic never bypasses AxonLLM. Router initialization or
mid-flight failures return an error rather than falling back to a direct,
ungoverned provider request.
