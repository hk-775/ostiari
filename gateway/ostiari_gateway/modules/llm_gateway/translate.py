"""Anthropic <-> provider translation for the Claude Code shim.

Claude Code speaks the Anthropic Messages wire format and runs its own tool
loop. When Ostiari routes a request to a *non-Anthropic* model (OpenAI, Azure,
Bedrock, …) we must:

  1. translate the Anthropic request (system, messages, tools — including
     ``tool_use`` / ``tool_result`` round-trip blocks) into the target
     provider's native request, and
  2. translate the provider's response back into an Anthropic Messages object,
     then re-emit it as Anthropic Server-Sent Events so the client's loop is
     unaffected.

Anthropic-target requests never touch this module — they are a raw passthrough
with true end-to-end streaming (see passthrough.py). This translation path is
buffered upstream (single provider call) and then streamed to the client as
well-formed Anthropic SSE; that keeps one correct code path across every
provider rather than a bespoke SSE parser per vendor.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

# ── inbound request parameters ──────────────────────────────────────────────


def opt_float(value: Any) -> float | None:
    """Coerce an inbound numeric request field while preserving *absence*.

    Lives here, rather than in either proxy, because both shims need the identical
    function and neither imports the other — and unlike ``_err``/``_provider_of``,
    which are duplicated across them because each needs genuinely different
    behavior, this has exactly one correct implementation.

    Returns None when the field was omitted (or holds something uncoercible), so
    the caller can leave the parameter off the upstream request rather than
    substituting an invented value. That distinction is load-bearing for
    ``temperature``: newer models *reject* the parameter instead of ignoring it
    (Bedrock Mantle's Claude models answer ``400 "`temperature` is deprecated for
    this model."``), so materializing a default turns "the caller didn't care"
    into a failed request. A default is only safe for a parameter every provider
    still accepts, and which ones those are changes under us.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        # A non-numeric temperature is a malformed request, but rejecting it here
        # would fail calls that previously worked (the old code raised ValueError
        # from float() — an unhandled 500). Dropping it degrades to the provider's
        # own default, which is the same outcome as omitting it.
        return None


# ── content flattening ──────────────────────────────────────────────────────


def flatten_system(system: Any) -> str:
    """Anthropic ``system`` may be a string or a list of text blocks."""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(
            b.get("text", "") for b in system
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def text_of(content: Any) -> str:
    """Concatenate the text blocks of an Anthropic message content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


# ── Anthropic request -> OpenAI Chat Completions request ─────────────────────


def anthropic_to_openai_messages(system: Any, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Anthropic messages (with content blocks) to OpenAI chat messages.

    Handles the tool round-trip that Claude Code relies on:
      - assistant ``tool_use`` block   -> assistant message ``tool_calls``
      - user ``tool_result`` block     -> ``role: "tool"`` message
    Tool names have dots replaced with ``_`` to satisfy OpenAI's name regex.
    """
    out: list[dict[str, Any]] = []
    sys_text = flatten_system(system)
    if sys_text:
        out.append({"role": "system", "content": sys_text})

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        # content is a list of blocks
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name", "")).replace(".", "_"),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })
            elif btype == "tool_result":
                tr_content = block.get("content", "")
                if isinstance(tr_content, list):
                    tr_content = " ".join(
                        b.get("text", "") for b in tr_content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": tr_content if isinstance(tr_content, str) else json.dumps(tr_content),
                })

        if role == "assistant":
            am: dict[str, Any] = {"role": "assistant"}
            am["content"] = "\n".join(text_parts) if text_parts else None
            if tool_calls:
                am["tool_calls"] = tool_calls
            out.append(am)
        else:  # user
            if text_parts:
                out.append({"role": "user", "content": "\n".join(text_parts)})
            # tool_result blocks become their own tool-role messages
            out.extend(tool_results)

    return out


def anthropic_tools_to_openai(tools: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]] | None, dict[str, str]]:
    """Translate Anthropic tool specs to OpenAI function tools.

    Returns (openai_tools, name_map) where name_map maps the sanitized name
    back to the original so response tool calls can be restored.
    """
    if not tools:
        return None, {}
    name_map: dict[str, str] = {}
    oai: list[dict[str, Any]] = []
    for t in tools:
        original = t.get("name", "")
        safe = original.replace(".", "_")
        name_map[safe] = original
        oai.append({
            "type": "function",
            "function": {
                "name": safe,
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return oai, name_map


# ── provider response -> Anthropic Messages object ───────────────────────────

_OPENAI_STOP_TO_ANTHROPIC = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
}


def openai_response_to_anthropic(
    resp: Any, model: str, name_map: dict[str, str] | None = None
) -> dict[str, Any]:
    """Build an Anthropic Messages response dict from an OpenAI SDK response."""
    name_map = name_map or {}
    choice = resp.choices[0]
    msg = choice.message
    finish = choice.finish_reason or "stop"

    content_blocks: list[dict[str, Any]] = []
    if msg.content:
        content_blocks.append({"type": "text", "text": msg.content})
    if getattr(msg, "tool_calls", None):
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments) if isinstance(tc.function.arguments, str) else tc.function.arguments
            except Exception:
                args = {}
            content_blocks.append({
                "type": "tool_use",
                "id": tc.id,
                "name": name_map.get(tc.function.name, tc.function.name),
                "input": args or {},
            })

    usage = getattr(resp, "usage", None)
    in_tok = getattr(usage, "prompt_tokens", 0) or 0
    out_tok = getattr(usage, "completion_tokens", 0) or 0

    return {
        "id": getattr(resp, "id", "msg_ostiari"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": _OPENAI_STOP_TO_ANTHROPIC.get(finish, "end_turn"),
        "stop_sequence": None,
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
    }


def bedrock_converse_to_anthropic(resp: dict[str, Any], model: str) -> dict[str, Any]:
    """Build an Anthropic Messages response from a Bedrock ``converse`` result."""
    out_msg = resp.get("output", {}).get("message", {})
    content_blocks: list[dict[str, Any]] = []
    for block in out_msg.get("content", []):
        if "text" in block:
            content_blocks.append({"type": "text", "text": block["text"]})
        elif "toolUse" in block:
            tu = block["toolUse"]
            content_blocks.append({
                "type": "tool_use",
                "id": tu.get("toolUseId", ""),
                "name": tu.get("name", ""),
                "input": tu.get("input", {}),
            })
    usage = resp.get("usage", {})
    stop_map = {"end_turn": "end_turn", "tool_use": "tool_use", "max_tokens": "max_tokens", "stop_sequence": "stop_sequence"}
    return {
        "id": "msg_ostiari_bedrock",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": stop_map.get(resp.get("stopReason", "end_turn"), "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("inputTokens", 0),
            "output_tokens": usage.get("outputTokens", 0),
        },
    }


# ── Anthropic Messages object -> Anthropic SSE event stream ──────────────────


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def anthropic_message_to_sse(message: dict[str, Any]) -> Iterator[str]:
    """Re-emit a complete Anthropic Messages object as well-formed SSE.

    Produces the exact event sequence the Anthropic SDK / Claude Code expects:
    message_start -> (content_block_start/delta/stop)* -> message_delta -> message_stop.
    Text blocks stream their text as a single delta; tool_use blocks stream
    their JSON input as one input_json_delta. This is buffered-then-chunked —
    correct SSE semantics, not token-by-token from upstream.
    """
    usage = message.get("usage", {"input_tokens": 0, "output_tokens": 0})
    content = message.get("content", [])

    start_msg = {**message, "content": [], "usage": {**usage, "output_tokens": 0}}
    yield _sse("message_start", {"type": "message_start", "message": start_msg})

    for idx, block in enumerate(content):
        btype = block.get("type")
        if btype == "text":
            yield _sse("content_block_start", {
                "type": "content_block_start", "index": idx,
                "content_block": {"type": "text", "text": ""},
            })
            yield _sse("content_block_delta", {
                "type": "content_block_delta", "index": idx,
                "delta": {"type": "text_delta", "text": block.get("text", "")},
            })
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
        elif btype == "tool_use":
            yield _sse("content_block_start", {
                "type": "content_block_start", "index": idx,
                "content_block": {"type": "tool_use", "id": block.get("id", ""),
                                  "name": block.get("name", ""), "input": {}},
            })
            yield _sse("content_block_delta", {
                "type": "content_block_delta", "index": idx,
                "delta": {"type": "input_json_delta",
                          "partial_json": json.dumps(block.get("input", {}))},
            })
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})

    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": message.get("stop_reason", "end_turn"),
                  "stop_sequence": message.get("stop_sequence")},
        "usage": {"output_tokens": usage.get("output_tokens", 0)},
    })
    yield _sse("message_stop", {"type": "message_stop"})
