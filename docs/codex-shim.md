# OpenAI compatibility and Codex CLI status

Ostiari exposes two governed OpenAI-compatible endpoints:

| Endpoint | Contract |
|---|---|
| `POST /v1/chat/completions` | OpenAI Chat Completions compatibility for SDKs and applications that still use that API. |
| `POST /v1/responses` | Stateless Responses compatibility for text, image-URL, and function-tool input. |

Both endpoints use the same authorization, content-security, quota, embedded
AxonLLM routing, cost, and trace pipeline.

## Codex CLI compatibility

Current Codex custom providers use the Responses wire API. Ostiari therefore
does **not** claim complete Codex CLI compatibility yet: the implemented
`/v1/responses` surface deliberately rejects fields whose semantics Ostiari
does not implement, including stored conversations, background execution,
reasoning configuration, prompt references, and structured-output settings.

Do not point a production Codex CLI at Ostiari until the release gate includes:

1. a captured request contract from the supported Codex version;
2. end-to-end function-call and function-output round trips;
3. streaming event-order verification;
4. explicit support or documented rejection for every field Codex sends.

Failing closed here is intentional. Silently ignoring an unsupported field
would make a request appear governed while changing its model behavior.

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
- reasoning and structured-output configuration;
- file-backed images;
- non-function tools;
- unsupported include, service-tier, and truncation modes.

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
