"""Cohere provider adapter for the LLM-Router."""

import json
import logging

from src.gateway.adapters.base import ProviderAdapter
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ModelInfo,
    StreamChunk,
    TokenUsage,
)
from src.gateway.router import ProviderError

logger = logging.getLogger(__name__)

PROVIDER_NAME = "cohere"

_COHERE_MODELS = [
    ModelInfo(model_id="command-r-plus", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="command-r", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="command-light", provider=PROVIDER_NAME, capabilities=["chat"]),
]
_COHERE_FINISH_REASONS = {
    "COMPLETE": "stop",
    "STOP_SEQUENCE": "stop",
    "MAX_TOKENS": "length",
    "MAX_TOKENS_REACHED": "length",
    "ERROR_TOXIC": "content_filter",
}


def _openai_tool_calls(messages: list[dict]) -> dict[str, dict]:
    calls: dict[str, dict] = {}
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        raw_calls = message.get("tool_calls")
        if not isinstance(raw_calls, list):
            continue
        for raw_call in raw_calls:
            if not isinstance(raw_call, dict):
                continue
            function = raw_call.get("function")
            if not isinstance(function, dict):
                function = {}
            call_id = raw_call.get("id")
            name = function.get("name") or raw_call.get("name")
            if (
                isinstance(call_id, str)
                and call_id
                and isinstance(name, str)
                and name
            ):
                calls[call_id] = {
                    "name": name,
                    "parameters": _cohere_args(
                        function.get(
                            "arguments",
                            raw_call.get("arguments", {}),
                        )
                    ),
                }
    return calls


def _cohere_finish_reason(value: object) -> str:
    raw_reason = str(value or "COMPLETE").upper()
    if raw_reason.startswith("ERROR"):
        if raw_reason in _COHERE_FINISH_REASONS:
            return _COHERE_FINISH_REASONS[raw_reason]
        raise ProviderError(
            status_code=502,
            provider=PROVIDER_NAME,
            message="Cohere generation failed",
        )
    return _COHERE_FINISH_REASONS.get(
        raw_reason,
        raw_reason.casefold(),
    )


class CohereAdapter(ProviderAdapter):
    """Translates between the unified Gateway format and Cohere's native chat API format.

    Cohere uses: message (last user message), chat_history (previous messages),
    preamble (system message), temperature, max_tokens, p (top_p), stop_sequences.
    """

    PROVIDER_NAME = PROVIDER_NAME
    _MODELS = _COHERE_MODELS

    def validate_request(self, request: ChatCompletionRequest) -> None:
        if not request.tools or request.tool_choice in (None, "auto", "none"):
            return

        if isinstance(request.tool_choice, dict):
            unsupported_control = "named-tool selection"
        elif request.tool_choice in ("required", "any"):
            unsupported_control = "required-tool selection"
        else:
            unsupported_control = "explicit tool-choice"
        raise ProviderError(
            status_code=400,
            provider=PROVIDER_NAME,
            message=(
                f"Cohere v1 chat has no {unsupported_control} control; "
                f"tool_choice={request.tool_choice!r} cannot be honored"
            ),
            retryable=False,
            provider_unavailable=False,
        )

    async def translate_request(
        self, request: ChatCompletionRequest, *, prompt_caching_enabled: bool = False
    ) -> dict:
        self.validate_request(request)

        preamble = request.system
        chat_history: list[dict] = []
        last_user_message = ""
        tool_results: list[dict] = []
        tool_calls_by_id = _openai_tool_calls(request.messages)

        for msg in request.messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                if preamble is None:
                    preamble = content
                continue
            # Cohere carries tool output in a top-level `tool_results` field, not
            # as a history turn — and pairs each result with the call that
            # produced it. Keeping it out of chat_history also stops a tool
            # result from being mistaken for the user's next message below.
            if role == "tool":
                call = tool_calls_by_id.get(msg.get("tool_call_id", ""), {})
                name = (
                    msg.get("name")
                    or call.get("name", "")
                )
                tool_results.append({
                    "call": {
                        "name": name,
                        "parameters": call.get("parameters", {}),
                    },
                    "outputs": [{"output": content if isinstance(content, str)
                                 else json.dumps(content)}],
                })
                continue
            cohere_role = "CHATBOT" if role == "assistant" else "USER"
            entry: dict = {"role": cohere_role, "message": content or ""}
            # An assistant turn that called tools has content=None; record the
            # calls so the model sees its own prior turn.
            if role == "assistant" and msg.get("tool_calls"):
                entry["tool_calls"] = [
                    {"name": (tc.get("function") or {}).get("name", tc.get("name", "")),
                     "parameters": _cohere_args((tc.get("function") or {}).get(
                         "arguments", tc.get("arguments", {})))}
                    for tc in msg["tool_calls"]
                ]
            chat_history.append(entry)

        if chat_history and chat_history[-1]["role"] == "USER":
            last_user_message = chat_history.pop()["message"]

        payload: dict = {
            "message": last_user_message,
            "model": request.model,
        }

        if tool_results:
            # Cohere requires message to be empty when tool_results is set — the
            # turn *is* the tool output, not new user text.
            payload["tool_results"] = tool_results

        if chat_history:
            payload["chat_history"] = chat_history
        if preamble is not None:
            payload["preamble"] = preamble
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.top_p is not None:
            payload["p"] = request.top_p
        if request.stop is not None:
            payload["stop_sequences"] = request.stop
        if request.tools and request.tool_choice != "none":
            payload["tools"] = [_openai_tool_to_cohere(t) for t in request.tools]
        if request.stream:
            payload["stream"] = True

        return payload

    def translate_response(self, provider_response: dict) -> ChatCompletionResponse:
        text = provider_response.get("text", "")

        tool_calls = _cohere_tool_calls(
            provider_response.get("tool_calls") or []
        )

        message: dict = {"role": "assistant", "content": text}
        finish_reason = _cohere_finish_reason(
            provider_response.get("finish_reason")
        )
        if tool_calls:
            message["tool_calls"] = tool_calls
            if not text:
                message["content"] = None
            finish_reason = "tool_calls"

        choices = [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ]

        return ChatCompletionResponse(
            id=provider_response.get("id", ""),
            choices=choices,
            usage=_cohere_usage(provider_response),
            model=provider_response.get("model", ""),
            provider=PROVIDER_NAME,
        )

    def translate_stream_chunk(self, chunk: dict) -> StreamChunk:
        event_type = chunk.get("event_type", "")
        response = chunk.get("response", {}) or {}
        delta: dict = {}
        choices: list[dict] = []
        is_final = False
        usage = None
        finish_reason = None

        if event_type == "text-generation":
            text = chunk.get("text", "")
            if text:
                delta["content"] = text
        elif event_type == "stream-end":
            is_final = True
            tool_calls = _cohere_tool_calls(
                response.get("tool_calls") or []
            )
            if tool_calls:
                delta["tool_calls"] = [
                    {"index": index, **tool_call}
                    for index, tool_call in enumerate(tool_calls)
                ]
                finish_reason = "tool_calls"
            else:
                finish_reason = _cohere_finish_reason(
                    response.get("finish_reason")
                    or chunk.get("finish_reason")
                )
            usage = _cohere_usage(response or chunk)

        if delta or is_final:
            choices = [{
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }]

        return StreamChunk(
            id=(
                response.get("response_id")
                or response.get("generation_id")
                or chunk.get("generation_id")
                or chunk.get("id", "")
            ),
            choices=choices,
            model=response.get("model") or chunk.get("model", ""),
            is_final=is_final,
            usage=usage,
        )


# --- OpenAI ⇄ Cohere tool translation ---------------------------------------


def _cohere_tool_calls(raw_calls: list[dict]) -> list[dict]:
    """Translate complete Cohere calls into OpenAI-compatible tool calls."""
    return [
        {
            # Cohere returns no call id; synthesize a stable one for the
            # round-trip (the caller echoes it back, Cohere matches on name).
            "id": f"call_{call.get('name', 'fn')}_{index}",
            "type": "function",
            "function": {
                "name": call.get("name", ""),
                "arguments": json.dumps(call.get("parameters", {})),
            },
        }
        for index, call in enumerate(raw_calls)
        if isinstance(call, dict)
    ]


def _cohere_usage(payload: dict) -> TokenUsage:
    meta = payload.get("meta", {}) or {}
    billed_units = meta.get("billed_units")
    if isinstance(billed_units, dict) and (
        "input_tokens" in billed_units
        or "output_tokens" in billed_units
    ):
        tokens = billed_units
    else:
        tokens = meta.get("tokens") or {}
    if not isinstance(tokens, dict):
        tokens = {}
    prompt_tokens = int(tokens.get("input_tokens", 0) or 0)
    completion_tokens = int(tokens.get("output_tokens", 0) or 0)
    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


def _cohere_args(raw) -> dict:
    """OpenAI sends tool arguments as a JSON string; Cohere wants an object.

    Malformed JSON from a model must not fail the request — send {} and let the
    tool report the bad call.
    """
    if isinstance(raw, str):
        try:
            return json.loads(raw) if raw.strip() else {}
        except (ValueError, TypeError):
            return {}
    return raw or {}


def _openai_tool_to_cohere(tool: dict) -> dict:
    """Convert one OpenAI tool spec to Cohere's parameter_definitions shape.

    Cohere describes parameters one-by-one with a type/description/required
    triple rather than taking a JSON Schema object, so the schema's properties
    have to be unrolled.
    """
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    schema = fn.get("parameters") or tool.get("input_schema") or {}
    required = set(schema.get("required") or [])
    definitions = {}
    for name, spec in (schema.get("properties") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        definitions[name] = {
            # Cohere expects Python-ish type names ("str", "int", "list"), not
            # JSON Schema's. Anything unrecognized falls back to str, which the
            # model can still fill in.
            "type": _COHERE_TYPES.get(spec.get("type", "string"), "str"),
            "description": spec.get("description", ""),
            "required": name in required,
        }
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "parameter_definitions": definitions,
    }


_COHERE_TYPES = {
    "string": "str", "integer": "int", "number": "float",
    "boolean": "bool", "array": "list", "object": "dict",
}
