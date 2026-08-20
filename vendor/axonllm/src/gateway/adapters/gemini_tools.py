"""OpenAI ⇄ Gemini tool translation, shared by the Google AI and Vertex adapters.

Both providers speak the identical Gemini dialect (``contents`` + ``parts``,
``functionDeclarations``, ``functionCall``/``functionResponse`` parts), so the
translation lives here once rather than being duplicated — and diverging — in
two adapters.

    OpenAI                                Gemini
    tools[].function.{name,parameters}    tools[0].functionDeclarations[]
    assistant.tool_calls[]                parts[{functionCall:{name,args}}]
    role:"tool" message                   role:"user" parts[{functionResponse}]
    finish_reason:"tool_calls"            finishReason:"STOP" + a functionCall part

Note the last row: Gemini does *not* signal tool use in ``finishReason`` — it
returns STOP and puts a functionCall part in the content. So the presence of the
part is the only signal, and callers branching on ``finish_reason`` need it
synthesized for them.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from src.gateway.models import TokenUsage

# Gemini rejects unknown JSON Schema keys outright rather than ignoring them, so
# a schema written for OpenAI has to be filtered rather than passed through.
_ALLOWED_SCHEMA_KEYS = frozenset({
    "type", "format", "description", "nullable", "enum", "maxItems", "minItems",
    "properties", "required", "items", "example",
})
_GEMINI_CALL_ID_PREFIX = "call_gemini_"
_MAX_GEMINI_CALL_ID_PAYLOAD_BYTES = 16_384


def _clean_schema(node: Any) -> Any:
    """Strip JSON Schema keys Gemini doesn't accept, recursively.

    ``additionalProperties``, ``$schema``, ``title``, ``default`` and friends are
    ordinary in a tool schema written for OpenAI and cause a 400 here. Dropping
    them keeps the tool usable; keeping them fails the whole request.
    """
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k not in _ALLOWED_SCHEMA_KEYS:
                continue
            if k == "properties" and isinstance(v, dict):
                out[k] = {pk: _clean_schema(pv) for pk, pv in v.items()}
            elif k == "items":
                out[k] = _clean_schema(v)
            else:
                out[k] = v
        return out
    return node


def openai_tools_to_gemini(tools: list[dict]) -> list[dict]:
    """Convert OpenAI tool specs into Gemini's single-entry tools list."""
    declarations = []
    for t in tools:
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        schema = fn.get("parameters") or t.get("input_schema") or {"type": "object", "properties": {}}
        declarations.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": _clean_schema(schema),
        })
    # Gemini takes one tools entry holding all declarations, not one per tool.
    return [{"functionDeclarations": declarations}]


def openai_tool_choice_to_gemini(choice: str | dict | None) -> dict | None:
    """Map OpenAI's tool_choice onto Gemini's toolConfig.

    Gemini: AUTO | ANY | NONE, with an optional allowlist for a named function.
    """
    if choice is None:
        return None
    if choice == "none":
        return {"functionCallingConfig": {"mode": "NONE"}}
    if choice == "auto":
        return {"functionCallingConfig": {"mode": "AUTO"}}
    if choice in ("required", "any"):
        return {"functionCallingConfig": {"mode": "ANY"}}
    if isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name") or choice.get("name")
        if name:
            return {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [name]}}
    return None


def _parse_args(raw: Any) -> dict:
    """OpenAI sends tool arguments as a JSON string; Gemini wants an object.

    A model can emit malformed JSON, which must not fail the request — send an
    empty object and let the tool report the bad call.
    """
    if isinstance(raw, str):
        try:
            return json.loads(raw) if raw.strip() else {}
        except (ValueError, TypeError):
            return {}
    return raw or {}


def _gemini_tool_call_id(
    function_call: dict,
    part: dict,
    index: int,
) -> str:
    """Carry Gemini 3 continuation metadata in the standard tool-call id."""
    raw_provider_id = function_call.get("id")
    provider_id = raw_provider_id if isinstance(raw_provider_id, str) else ""
    raw_signature = part.get("thoughtSignature")
    if not isinstance(raw_signature, str) or not raw_signature:
        return provider_id or (
            f"call_{function_call.get('name', 'fn')}_{index}"
        )

    metadata = json.dumps(
        [provider_id, raw_signature],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(metadata).rstrip(b"=").decode("ascii")
    return f"{_GEMINI_CALL_ID_PREFIX}{encoded}"


def _gemini_call_metadata(call_id: object) -> tuple[str | None, str | None]:
    """Recover a bounded Gemini call id and thought signature capsule."""
    if not isinstance(call_id, str) or not call_id.startswith(
        _GEMINI_CALL_ID_PREFIX
    ):
        return None, None
    encoded = call_id.removeprefix(_GEMINI_CALL_ID_PREFIX)
    if not encoded or len(encoded) > _MAX_GEMINI_CALL_ID_PAYLOAD_BYTES:
        return None, None
    padded = encoded + ("=" * (-len(encoded) % 4))
    try:
        decoded = base64.b64decode(
            padded,
            altchars=b"-_",
            validate=True,
        )
        metadata = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if (
        not isinstance(metadata, list)
        or len(metadata) != 2
        or not all(isinstance(value, str) for value in metadata)
        or not metadata[1]
    ):
        return None, None
    return metadata[0] or None, metadata[1]


def openai_tool_call_names(messages: list[dict]) -> dict[str, str]:
    """Index assistant tool calls so standard tool results can recover names."""
    names: dict[str, str] = {}
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
                names[call_id] = name
    return names


def openai_msg_to_gemini(
    msg: dict,
    tool_call_names: dict[str, str] | None = None,
) -> dict | None:
    """Convert one OpenAI-shaped message to a Gemini ``contents`` entry.

    Returns None for a message the caller should skip (a system message, which
    Gemini carries in ``systemInstruction`` instead).
    """
    role = msg.get("role", "user")
    if role == "system":
        return None

    # A tool result comes back as a user-role functionResponse part. Gemini keys
    # it by function *name*, not by call id — so a parallel call to two different
    # tools stays unambiguous, but two calls to the same tool do not. That's
    # Gemini's model, not something the adapter can fix.
    if role == "tool":
        content = msg.get("content")
        name = msg.get("name")
        if not name and tool_call_names is not None:
            name = tool_call_names.get(msg.get("tool_call_id", ""), "")
        return {
            "role": "user",
            "parts": [{"functionResponse": {
                "name": name or "",
                "response": {"content": content if isinstance(content, str)
                             else json.dumps(content)},
            }}],
        }

    tool_calls = msg.get("tool_calls")
    if role == "assistant" and tool_calls:
        parts: list[dict] = []
        text = msg.get("content")
        if isinstance(text, str) and text:
            parts.append({"text": text})
        for tc in tool_calls:
            fn = tc.get("function") or {}
            function_call = {
                "name": fn.get("name", tc.get("name", "")),
                "args": _parse_args(fn.get("arguments", tc.get("arguments", {}))),
            }
            provider_id, thought_signature = _gemini_call_metadata(
                tc.get("id")
            )
            if provider_id is not None:
                function_call["id"] = provider_id
            part = {"functionCall": function_call}
            if thought_signature is not None:
                part["thoughtSignature"] = thought_signature
            parts.append(part)
        return {"role": "model", "parts": parts}

    content = msg.get("content")
    return {
        "role": "model" if role == "assistant" else "user",
        "parts": [{"text": content if isinstance(content, str) else str(content or "")}],
    }


def gemini_token_usage(metadata: object) -> TokenUsage:
    """Map Gemini usage, charging hidden thinking tokens as provider output."""
    usage = metadata if isinstance(metadata, dict) else {}
    prompt_tokens = int(usage.get("promptTokenCount", 0) or 0)
    completion_tokens = (
        int(usage.get("candidatesTokenCount", 0) or 0)
        + int(usage.get("thoughtsTokenCount", 0) or 0)
    )
    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cached_tokens=int(usage.get("cachedContentTokenCount", 0) or 0),
    )


def gemini_parts_to_tool_calls(parts: list[dict]) -> list[dict]:
    """Extract OpenAI-shaped tool_calls from Gemini response parts.

    Older Gemini responses have no call id, so one is synthesized. Gemini 3
    requires its opaque thought signature on the continuation request. The
    standard OpenAI tool-call id carries that metadata through stateless
    clients, which already must echo the id with the tool result.
    """
    calls = []
    for i, part in enumerate(parts):
        fc = part.get("functionCall")
        if not fc:
            continue
        calls.append({
            "id": _gemini_tool_call_id(fc, part, i),
            "type": "function",
            "function": {
                "name": fc.get("name", ""),
                # OpenAI carries arguments as a JSON string; callers json.loads it.
                "arguments": json.dumps(fc.get("args", {})),
            },
        })
    return calls
