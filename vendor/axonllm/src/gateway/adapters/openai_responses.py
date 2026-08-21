"""OpenAI Responses API translation, for models that reject Chat Completions.

``gpt-5.5-pro`` was configured to route over ``/v1/chat/completions`` and could
never work there: OpenAI answers 400 ``This is not a chat model and thus not
supported in the v1/chat/completions endpoint``. This is not a one-off typo in
the model list — the ``-pro`` tier is a *class* of models served only by
``/v1/responses`` (``gpt-5-pro`` reports ``This model is only supported in
v1/responses``), so deleting the config entry would just defer the same failure
to the next one added.

Two properties of the Responses API drive the shape of this module:

* **Tools are flat.** Chat Completions nests the definition under ``function``;
  here ``name``/``parameters`` sit at the top level beside ``type``, and tool
  traffic is top-level ``function_call`` / ``function_call_output`` *items*
  rather than messages. The nested form is rejected outright.
* **Sampling parameters are rejected, not ignored.** ``gpt-5.5-pro`` answers 400
  ``Unsupported parameter: 'temperature' is not supported with this model``. A
  gateway whose callers set a default temperature would therefore 400 on every
  request and trip the provider's circuit breaker, so these are dropped rather
  than forwarded.

The request/response translation duplicates no logic with ``mantle_provider``:
the four helpers there are imported, since Mantle's ``/openai/v1/responses``
route speaks the same dialect and two copies would drift.
"""

from __future__ import annotations

import re
from typing import Any

from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    StreamChunk,
    TokenUsage,
)

# Models served only by /v1/responses. The "-pro" suffix is the marker: gpt-5-pro
# and gpt-5.5-pro both reject Chat Completions, while plain gpt-5 and gpt-5.1
# accept it — so this keys on the tier suffix rather than the family prefix,
# which would wrongly capture the chat-capable models.
_RESPONSES_ONLY_RE = re.compile(r"(^|[-.])(o[134]|gpt-[\d.]+)-pro([-.]|$)")

# The Responses API reports lifecycle status, not a stop reason. "incomplete"
# most often means the output cap was hit; incomplete_details.reason says so
# precisely and is preferred when present.
_INCOMPLETE_REASONS = {
    "max_output_tokens": "length",
    "content_filter": "content_filter",
}


def is_responses_only_model(model_id: str) -> bool:
    """True when the model must be called on /v1/responses.

    Keyed on the ``-pro`` tier suffix: ``gpt-5.5-pro`` and ``gpt-5-pro`` reject
    Chat Completions while ``gpt-5`` and ``gpt-5.1`` accept it, so matching the
    family prefix would wrongly divert working models onto a second code path.
    """
    return bool(_RESPONSES_ONLY_RE.search((model_id or "").strip().lower()))


def build_responses_payload(request: ChatCompletionRequest, model_id: str) -> dict:
    """Translate a unified request into a Responses API payload."""
    # Imported here rather than at module scope: mantle_provider imports boto3,
    # and the adapter layer is exercised in tests that have no AWS dependency.
    from src.gateway.mantle_provider import (
        _openai_msgs_to_responses_input,
        _openai_tool_choice_to_responses,
        _openai_tool_to_responses,
    )

    instructions = request.system or None
    non_system = []
    for msg in request.messages:
        if msg.get("role") == "system":
            # A later system message wins, matching how the adapters flatten
            # system turns elsewhere in the gateway.
            instructions = msg.get("content", "")
        else:
            non_system.append(msg)

    input_items = _openai_msgs_to_responses_input(non_system)

    # A lone plain user turn can be sent as a bare string; a function_call_output
    # has no such shorthand, so check the item really is a role message.
    if (
        len(input_items) == 1
        and input_items[0].get("role") == "user"
        and "type" not in input_items[0]
    ):
        input_val: str | list[dict] = input_items[0]["content"]
    else:
        input_val = input_items

    payload: dict[str, Any] = {"model": model_id, "input": input_val}
    if instructions:
        payload["instructions"] = instructions
    if request.max_tokens is not None:
        payload["max_output_tokens"] = request.max_tokens

    # temperature/top_p are deliberately omitted — see the module docstring.
    # These models reject them with a 400 rather than ignoring them, so
    # forwarding a caller's default would fail every request.

    if request.tools:
        payload["tools"] = [_openai_tool_to_responses(t) for t in request.tools]
        tool_choice = _openai_tool_choice_to_responses(request.tool_choice)
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
    if request.stream:
        payload["stream"] = True
    return payload


def _finish_reason_from_response(response: dict, has_tool_calls: bool) -> str:
    """Derive a stop reason from the Responses API's lifecycle status.

    ``status`` says whether the *request* finished, not why generation stopped, so
    a tool call is invisible in it — and a caller driving a tool loop reads
    "completed" as "nothing left to do" and never runs the tool.
    """
    if has_tool_calls:
        return "tool_calls"
    if response.get("status") == "incomplete":
        reason = (response.get("incomplete_details") or {}).get("reason", "")
        return _INCOMPLETE_REASONS.get(reason, "length")
    return "stop"


def _usage_from_response(usage_data: dict) -> TokenUsage:
    """Map Responses API token counts onto the unified shape.

    The field names differ from Chat Completions (``input_tokens`` /
    ``output_tokens``), and ``output_tokens`` already includes the reasoning
    tokens these models spend before emitting any text — so no separate
    reasoning count needs adding, which would double-bill.
    """
    prompt_tokens = usage_data.get("input_tokens", 0)
    completion_tokens = usage_data.get("output_tokens", 0)
    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=usage_data.get("total_tokens", prompt_tokens + completion_tokens),
    )


def translate_responses_reply(response: dict, provider: str) -> ChatCompletionResponse:
    """Translate a Responses API reply into the unified response shape."""
    from src.gateway.mantle_provider import _responses_output_to_tool_calls

    output = response.get("output", []) or []

    # Reasoning items carry no user-visible text (their content is encrypted);
    # only output_text blocks inside message items do.
    text = "".join(
        block.get("text", "")
        for item in output
        if item.get("type") == "message"
        for block in item.get("content", []) or []
        if block.get("type") == "output_text"
    )

    tool_calls = _responses_output_to_tool_calls(output)
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
        # OpenAI sends content: null alongside tool_calls, but only when there is
        # no text — a plain response still gets "" so clients reading it
        # unconditionally keep working.
        if not text:
            message["content"] = None

    return ChatCompletionResponse(
        id=response.get("id", ""),
        choices=[{
            "index": 0,
            "message": message,
            "finish_reason": _finish_reason_from_response(response, bool(tool_calls)),
        }],
        usage=_usage_from_response(response.get("usage", {}) or {}),
        model=response.get("model", ""),
        provider=provider,
    )


def translate_responses_stream_event(event: dict) -> StreamChunk | None:
    """Translate one Responses API SSE event into a ``StreamChunk``.

    Returns ``None`` for events that carry nothing a client needs — lifecycle
    notifications, reasoning items, and the per-delta duplicates of data that
    also arrives whole.

    Unlike the four hand-built translators (Anthropic, Vertex, Google AI,
    Cohere), a tool call here needs **no cross-chunk accumulation**:
    ``response.output_item.done`` carries the finished ``function_call`` with its
    ``call_id``, ``name`` and complete ``arguments`` string. So the stateless
    translator signature is sufficient, and the arguments are never split into
    fragments a client would have to reassemble before parsing.
    """
    event_type = event.get("type", "")

    if event_type == "response.output_text.delta":
        delta = event.get("delta", "")
        if not delta:
            return None
        return StreamChunk(
            id=event.get("item_id", ""),
            choices=[{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
            model="",
            is_final=False,
        )

    if event_type == "response.output_item.done":
        item = event.get("item", {}) or {}
        if item.get("type") != "function_call":
            # Reasoning and message items add nothing here: the message text
            # already arrived as deltas, and re-emitting it would duplicate the
            # whole response.
            return None
        return StreamChunk(
            id=item.get("id", ""),
            choices=[{
                "index": 0,
                # content: null, matching what OpenAI sends on a tool-call delta —
                # "" would read as a turn of empty prose.
                "delta": {
                    "content": None,
                    "tool_calls": [{
                        "id": item.get("call_id") or item.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": item.get("name", ""),
                            "arguments": item.get("arguments", "") or "{}",
                        },
                    }],
                },
                "finish_reason": None,
            }],
            model="",
            is_final=False,
        )

    if event_type in ("response.completed", "response.incomplete"):
        response = event.get("response", {}) or {}
        output = response.get("output", []) or []
        has_tool_calls = any(item.get("type") == "function_call" for item in output)
        return StreamChunk(
            id=response.get("id", ""),
            choices=[{
                "index": 0,
                "delta": {},
                "finish_reason": _finish_reason_from_response(response, has_tool_calls),
            }],
            model=response.get("model", ""),
            is_final=True,
            usage=_usage_from_response(response.get("usage", {}) or {}),
        )

    if event_type == "response.failed":
        # Surface the provider's own message rather than a silent truncation: an
        # empty stream that ends cleanly is indistinguishable from a short answer.
        response = event.get("response", {}) or {}
        error = response.get("error") or {}
        raise ResponsesStreamError(error.get("message") or "Responses API stream failed")

    return None


class ResponsesStreamError(RuntimeError):
    """Raised when the Responses API reports a failed generation mid-stream."""
