"""OpenAI-compatible ingress for AxonLLM.

Exposes ``POST /v1/chat/completions``, ``POST /v1/responses``,
``POST /v1/embeddings``, and ``GET /v1/models`` in the shape the OpenAI SDK
(and the many tools built on it) expect, so a client can point at AxonLLM with
nothing more than a ``base_url`` swap:

    from openai import OpenAI
    client = OpenAI(base_url="https://<gateway>/v1", api_key="axon_...")

This reuses the internal GatewayAgent pipeline (routing, quotas, guardrails,
cost tracking) — it is a thin translation layer over ``handle_chat_completion``,
not a second implementation. Identity for attribution comes from the
authenticated request context (see AuthMiddleware / task #3), never the body.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from src.gateway.chat.request_body import (
    DEFAULT_CHAT_REQUEST_MAX_BYTES,
    JSONBodyError,
    read_json_object,
)
from src.gateway.request_validator import RequestValidator

if TYPE_CHECKING:
    from src.gateway.chat.client_agent import ClientAgent

logger = logging.getLogger("gateway.openai")


class ResponsesRequestError(ValueError):
    """A client-correctable Responses API translation error."""


def _identity(
    request: Request,
) -> tuple[str | None, str | None, str | None]:
    """Trustworthy user, project, and tenant from authenticated context.

    Mirrors chat/routes.py::_identity_from_context — identity comes from the
    token, not the request body. ANONYMOUS (dev/LOG_ONLY) returns three ``None``
    values so ClientAgent falls back to its configured defaults.
    """
    ctx = getattr(request.state, "context", None)
    if ctx is None:
        return None, None, None
    if getattr(ctx.auth_method, "value", None) == "anonymous":
        return None, None, None
    return (
        ctx.user_id or None,
        ctx.project_id or None,
        getattr(ctx, "tenant_id", None) or None,
    )


def _authorized_project(request: Request):
    """Return the tenant-qualified project established by middleware."""
    context = getattr(request.state, "context", None)
    return getattr(context, "authorized_project", None)


def _allow_legacy_project_lookup(request: Request) -> bool:
    """Allow the global project map only outside canonical principal mode."""
    return (
        getattr(request.state, "principal", None) is None
        and _authorized_project(request) is None
    )


def _error(status_code: int, message: str, err_type: str = "invalid_request_error") -> JSONResponse:
    """OpenAI-shaped error envelope."""
    return JSONResponse(
        {"error": {"message": message, "type": err_type, "param": None, "code": None}},
        status_code=status_code,
    )


def _resolve_request_validator(client_agent: ClientAgent) -> RequestValidator:
    gateway_agent = getattr(client_agent, "gateway_agent", None)
    validator = getattr(gateway_agent, "request_validator", None)
    if isinstance(validator, RequestValidator):
        return validator
    return RequestValidator()


def _responses_content(content: Any, *, field: str) -> str | list[dict[str, Any]]:
    """Translate Responses message content into Chat Completions content."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or not content:
        raise ResponsesRequestError(
            f"Field '{field}' must be a string or a non-empty list of content parts."
        )

    translated: list[dict[str, Any]] = []
    text_parts: list[str] = []
    only_text = True
    for index, part in enumerate(content):
        part_field = f"{field}[{index}]"
        if not isinstance(part, dict):
            raise ResponsesRequestError(f"Field '{part_field}' must be an object.")
        part_type = part.get("type")
        if part_type in {"input_text", "output_text", "text"}:
            text = part.get("text")
            if not isinstance(text, str):
                raise ResponsesRequestError(
                    f"Field '{part_field}.text' must be a string."
                )
            text_parts.append(text)
            translated.append({"type": "text", "text": text})
            continue
        if part_type == "input_image":
            only_text = False
            if part.get("file_id") is not None:
                raise ResponsesRequestError(
                    "Responses input images referenced by file_id are not supported."
                )
            image_url = part.get("image_url")
            if not isinstance(image_url, str) or not image_url:
                raise ResponsesRequestError(
                    f"Field '{part_field}.image_url' must be a non-empty string."
                )
            image: dict[str, Any] = {"url": image_url}
            detail = part.get("detail")
            if detail is not None:
                if detail not in {"auto", "low", "high"}:
                    raise ResponsesRequestError(
                        f"Field '{part_field}.detail' must be auto, low, or high."
                    )
                image["detail"] = detail
            translated.append({"type": "image_url", "image_url": image})
            continue
        raise ResponsesRequestError(
            f"Responses content type '{part_type}' is not supported."
        )

    if only_text:
        return "".join(text_parts)
    return translated


def _responses_text_content(content: Any, *, field: str) -> str:
    """Return text-only content for system and developer instructions."""
    translated = _responses_content(content, field=field)
    if isinstance(translated, str):
        return translated
    raise ResponsesRequestError(
        "System and developer Responses input must contain text only."
    )


def _responses_input(input_value: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Translate Responses input items into chat messages and instructions."""
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}], []
    if not isinstance(input_value, list) or not input_value:
        raise ResponsesRequestError(
            "Field 'input' is required and must be a string or a non-empty list."
        )

    messages: list[dict[str, Any]] = []
    instructions: list[str] = []
    for index, item in enumerate(input_value):
        field = f"input[{index}]"
        if not isinstance(item, dict):
            raise ResponsesRequestError(f"Field '{field}' must be an object.")

        item_type = item.get("type")
        if item_type in {None, "message"}:
            role = item.get("role")
            if role not in {"user", "assistant", "system", "developer"}:
                raise ResponsesRequestError(
                    f"Field '{field}.role' must be user, assistant, system, or developer."
                )
            content = item.get("content")
            if role in {"system", "developer"}:
                instructions.append(
                    _responses_text_content(content, field=f"{field}.content")
                )
            else:
                messages.append(
                    {
                        "role": role,
                        "content": _responses_content(
                            content,
                            field=f"{field}.content",
                        ),
                    }
                )
            continue

        if item_type == "function_call":
            call_id = item.get("call_id") or item.get("id")
            name = item.get("name")
            arguments = item.get("arguments", "{}")
            if not isinstance(call_id, str) or not call_id:
                raise ResponsesRequestError(
                    f"Field '{field}.call_id' must be a non-empty string."
                )
            if not isinstance(name, str) or not name:
                raise ResponsesRequestError(
                    f"Field '{field}.name' must be a non-empty string."
                )
            if not isinstance(arguments, str):
                arguments = json.dumps(
                    arguments,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            tool_call = {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
            if (
                messages
                and messages[-1].get("role") == "assistant"
                and messages[-1].get("content") is None
                and isinstance(messages[-1].get("tool_calls"), list)
            ):
                messages[-1]["tool_calls"].append(tool_call)
            else:
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [tool_call],
                    }
                )
            continue

        if item_type == "function_call_output":
            call_id = item.get("call_id")
            output = item.get("output")
            if not isinstance(call_id, str) or not call_id:
                raise ResponsesRequestError(
                    f"Field '{field}.call_id' must be a non-empty string."
                )
            if not isinstance(output, str):
                output = json.dumps(
                    output,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": output,
                }
            )
            continue

        raise ResponsesRequestError(
            f"Responses input item type '{item_type}' is not supported."
        )

    if not messages:
        raise ResponsesRequestError(
            "Field 'input' must include at least one user, assistant, or tool item."
        )
    return messages, instructions


def _responses_tools(tools: Any) -> list[dict[str, Any]] | None:
    """Translate flat Responses function tools into Chat Completions tools."""
    if tools is None:
        return None
    if not isinstance(tools, list):
        raise ResponsesRequestError("Field 'tools' must be a list.")

    translated: list[dict[str, Any]] = []
    for index, tool in enumerate(tools):
        field = f"tools[{index}]"
        if not isinstance(tool, dict):
            raise ResponsesRequestError(f"Field '{field}' must be an object.")
        if tool.get("type") != "function":
            raise ResponsesRequestError(
                "Only function tools are supported by the AxonLLM router."
            )
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise ResponsesRequestError(
                f"Field '{field}.name' must be a non-empty string."
            )
        function: dict[str, Any] = {
            "name": name,
            "parameters": tool.get("parameters")
            or {"type": "object", "properties": {}},
        }
        for key in ("description", "strict"):
            if key in tool:
                function[key] = tool[key]
        translated.append({"type": "function", "function": function})
    return translated


def _responses_tool_choice(choice: Any) -> str | dict[str, Any] | None:
    """Translate the Responses API's flat function choice into chat shape."""
    if choice is None or isinstance(choice, str):
        return choice
    if not isinstance(choice, dict):
        raise ResponsesRequestError(
            "Field 'tool_choice' must be a string or an object."
        )
    if choice.get("type") != "function":
        raise ResponsesRequestError(
            "Only function tool choices are supported by the AxonLLM router."
        )
    name = choice.get("name")
    if not isinstance(name, str) or not name:
        raise ResponsesRequestError(
            "A function tool choice must include a non-empty name."
        )
    return {"type": "function", "function": {"name": name}}


def _translate_responses_request(body: dict[str, Any]) -> dict[str, Any]:
    """Return the governed chat request represented by a Responses payload."""
    for field in ("previous_response_id", "conversation", "prompt"):
        if body.get(field) is not None:
            raise ResponsesRequestError(
                f"Field '{field}' is stateful and is not supported by AxonLLM."
            )
    if body.get("store") is True:
        raise ResponsesRequestError(
            "AxonLLM is stateless; field 'store' must be false or omitted."
        )
    if body.get("background") is True:
        raise ResponsesRequestError("Background Responses are not supported.")
    if body.get("reasoning") not in (None, {}):
        raise ResponsesRequestError(
            "Responses reasoning configuration is not supported yet."
        )
    if body.get("text") not in (None, {}):
        raise ResponsesRequestError(
            "Responses structured text configuration is not supported yet."
        )
    for field in ("include", "max_tool_calls", "service_tier"):
        if body.get(field) is not None:
            raise ResponsesRequestError(
                f"Field '{field}' is not supported by AxonLLM."
            )
    if body.get("parallel_tool_calls") is False:
        raise ResponsesRequestError(
            "Disabling parallel tool calls is not supported yet."
        )
    truncation = body.get("truncation")
    if truncation not in (None, "disabled"):
        raise ResponsesRequestError(
            "Only truncation='disabled' is supported."
        )

    if "input" not in body:
        raise ResponsesRequestError("Field 'input' is required.")
    messages, input_instructions = _responses_input(body["input"])

    instructions = body.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        raise ResponsesRequestError("Field 'instructions' must be a string.")
    system_parts = ([instructions] if instructions else []) + input_instructions

    metadata = body.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ResponsesRequestError("Field 'metadata' must be an object.")

    return {
        "model": body.get("model", ""),
        "messages": messages,
        "temperature": body.get("temperature"),
        "max_tokens": body.get("max_output_tokens"),
        "top_p": body.get("top_p"),
        "system": "\n\n".join(system_parts) if system_parts else None,
        "stream": body.get("stream", False),
        "tools": _responses_tools(body.get("tools")),
        "tool_choice": _responses_tool_choice(body.get("tool_choice")),
        "metadata": metadata or {},
    }


def _responses_usage(usage: dict[str, Any]) -> dict[str, Any]:
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": usage.get("total_tokens", input_tokens + output_tokens),
    }


def _responses_output(
    content: str | None,
    tool_calls: Any,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if content is not None and (content or not tool_calls):
        output.append(
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": content,
                        "annotations": [],
                        "logprobs": [],
                    }
                ],
            }
        )
    if isinstance(tool_calls, list):
        for index, tool_call in enumerate(tool_calls):
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function") or {}
            output.append(
                {
                    "id": f"fc_{uuid.uuid4().hex}",
                    "call_id": tool_call.get("id") or f"call_{index}",
                    "type": "function_call",
                    "name": function.get("name", ""),
                    "arguments": function.get("arguments", "{}") or "{}",
                    "status": "completed",
                }
            )
    return output


def _responses_envelope(
    *,
    response_id: str,
    created_at: int,
    body: dict[str, Any],
    model: str,
    output: list[dict[str, Any]],
    usage: dict[str, Any] | None,
    finish_reason: str | None,
    status: str,
) -> dict[str, Any]:
    incomplete_details = None
    if finish_reason == "length":
        incomplete_details = {"reason": "max_output_tokens"}
    elif finish_reason == "content_filter":
        incomplete_details = {"reason": "content_filter"}
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "completed_at": int(time.time()) if status != "in_progress" else None,
        "status": status,
        "error": None,
        "incomplete_details": incomplete_details,
        "instructions": body.get("instructions"),
        "max_output_tokens": body.get("max_output_tokens"),
        "model": model,
        "output": output,
        "parallel_tool_calls": body.get("parallel_tool_calls", True),
        "previous_response_id": None,
        "prompt": None,
        "reasoning": None,
        "service_tier": "default",
        "store": False,
        "temperature": body.get("temperature"),
        "text": {"format": {"type": "text"}},
        "tool_choice": body.get(
            "tool_choice",
            "auto" if body.get("tools") else "none",
        ),
        "tools": body.get("tools") or [],
        "top_p": body.get("top_p"),
        "truncation": "disabled",
        "usage": usage,
        "user": body.get("user"),
        "metadata": body.get("metadata") or {},
    }


# OpenAI defines exactly four finish_reason values, and typed SDK clients
# deserialize the field into an enum — an unrecognized string is a validation
# error, not a curiosity. The adapters pass their provider's own stop reason
# through (Anthropic "end_turn", Gemini "MAX_TOKENS", Cohere "COMPLETE", …), so
# this boundary is where it has to become one of the four. Normalizing here
# rather than in each adapter keeps the internal API honest about what the
# provider actually said, while the OpenAI-compatible surface stays in spec.
_FINISH_REASONS = {
    # Anthropic / Bedrock Converse
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "content_filtered": "content_filter",
    "guardrail_intervened": "content_filter",
    # Gemini (Google AI / Vertex) — uppercase
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    # Cohere
    "COMPLETE": "stop",
    "MAX_TOKENS_REACHED": "length",
    "ERROR_TOXIC": "content_filter",
    # Mantle's /openai/v1/responses route reports lifecycle status rather than a
    # stop reason (see mantle_provider). "incomplete" there most often means the
    # token cap was hit.
    "completed": "stop",
    "incomplete": "length",
}

_VALID_FINISH_REASONS = {"stop", "length", "tool_calls", "content_filter"}


def _finish_reason(raw: Any, has_tool_calls: bool) -> str:
    """Map a provider stop reason onto OpenAI's four legal values.

    ``has_tool_calls`` wins over everything: a response carrying tool calls is
    a tool call regardless of what the provider labeled the stop, and a client
    that reads anything else here ends its tool loop without running the tool.
    """
    if has_tool_calls:
        return "tool_calls"
    if not isinstance(raw, str) or not raw:
        return "stop"
    if raw in _VALID_FINISH_REASONS:
        return raw
    mapped = _FINISH_REASONS.get(raw)
    if mapped:
        return mapped
    # Unknown reason: "stop" is the safe default — it ends the turn cleanly
    # rather than making a client retry or reject the response outright.
    logger.debug("unmapped finish_reason %r from provider; reporting 'stop'", raw)
    return "stop"


class OpenAICompatAPI:
    """OpenAI-compatible route handlers backed by the internal ClientAgent."""

    def __init__(
        self,
        client_agent: ClientAgent,
        *,
        max_request_bytes: int = DEFAULT_CHAT_REQUEST_MAX_BYTES,
        request_validator: RequestValidator | None = None,
    ) -> None:
        self.client_agent = client_agent
        self.max_request_bytes = max_request_bytes
        self.request_validator = (
            request_validator
            if request_validator is not None
            else _resolve_request_validator(client_agent)
        )

    # ------------------------------------------------------------------
    # POST /v1/chat/completions
    # ------------------------------------------------------------------

    async def chat_completions(self, request: Request):
        try:
            body = await read_json_object(
                request,
                max_bytes=self.max_request_bytes,
            )
        except JSONBodyError as exc:
            return _error(exc.status_code, exc.message)

        raw_model = body.get("model")
        # Smart routing (auto model selection): model == "auto" or empty/missing.
        # Otherwise a concrete model string is required. Lets standard OpenAI
        # clients opt into task-aware routing via `model: "auto"`.
        smart_routing = "model" not in body or (
            isinstance(raw_model, str)
            and raw_model.strip().lower() in ("", "auto")
        )
        errors = self.request_validator.validate_payload(
            body,
            allow_empty_model=smart_routing,
            check_model=False,
        )
        if errors:
            return _error(400, errors[0].message)

        model = body.get("model", "")
        assert isinstance(model, str)
        smart_routing = model.strip().lower() in ("", "auto")
        if smart_routing:
            model = ""
        messages = body.get("messages")
        assert isinstance(messages, list)

        temperature = body.get("temperature")
        max_tokens = body.get("max_tokens")
        top_p = body.get("top_p")
        stop = body.get("stop")
        system = body.get("system")
        stream = body.get("stream", False)
        # The pipeline translates tools per-provider, but this route never read
        # them off the body — so an OpenAI SDK client got a fluent 200 in which
        # the model states it has no such tool, with no error to notice it by.
        # The one failure mode worse than a 400.
        tools = body.get("tools")
        tool_choice = body.get("tool_choice")
        user_id, project_id, tenant_id = _identity(request)
        authorized_project = _authorized_project(request)
        allow_legacy_project_lookup = _allow_legacy_project_lookup(request)

        if stream:
            return await self._stream(model, messages, temperature, max_tokens,
                                      top_p, stop, system,
                                      user_id, project_id, smart_routing,
                                      tools, tool_choice, authorized_project,
                                      tenant_id, allow_legacy_project_lookup)
        return await self._complete(model, messages, temperature, max_tokens,
                                    top_p, stop, system,
                                    user_id, project_id, smart_routing,
                                    tools, tool_choice, authorized_project,
                                    tenant_id, allow_legacy_project_lookup)

    async def _complete(self, model, messages, temperature, max_tokens, top_p,
                        stop, system, user_id, project_id, smart_routing=False,
                        tools=None, tool_choice=None, authorized_project=None,
                        tenant_id=None, allow_legacy_project_lookup=False):
        try:
            kwargs = {
                "temperature": temperature,
                "max_tokens": max_tokens,
                "top_p": top_p,
                "stop": stop,
                "system": system,
                "user_id": user_id,
                "project_id": project_id,
                "smart_routing": smart_routing,
                "tools": tools,
                "tool_choice": tool_choice,
            }
            if tenant_id is not None:
                kwargs["tenant_id"] = tenant_id
            if authorized_project is not None:
                kwargs["authorized_project"] = authorized_project
            if allow_legacy_project_lookup:
                kwargs["allow_legacy_project_lookup"] = True
            resp = await self.client_agent.chat(model, messages, **kwargs)
        except Exception:
            logger.exception("chat completion failed")
            return _error(500, "Internal server error", err_type="server_error")

        resp.pop("_rate_limit_headers", None)
        if "error" in resp:
            err = resp["error"]
            msg = err.get("message", "request failed") if isinstance(err, dict) else str(err)
            return _error(resp.get("status_code", 500), msg, err_type="server_error")

        usage = resp.get("usage", {}) or {}
        tool_calls = resp.get("tool_calls")
        message: dict[str, Any] = {
            "role": "assistant",
            # "" not None when there are no tool_calls: a plain response has
            # always sent a string here and clients rely on it.
            "content": resp.get("content") if tool_calls else (resp.get("content") or ""),
        }
        if tool_calls:
            message["tool_calls"] = tool_calls

        # Was hardcoded "stop", which is what an OpenAI client reads as "the turn
        # is over" — so even once tool_calls were forwarded, a tool loop would
        # stop before running the tool.
        finish_reason = _finish_reason(resp.get("finish_reason"), bool(tool_calls))

        completion = {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": resp.get("model", model),
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }
        # Surface the smart-routing decision (task_type + selected model) as an
        # extension field. Standard OpenAI clients ignore unknown keys.
        if "smart_routing" in resp:
            completion["x_smart_routing"] = resp["smart_routing"]
        # Same for the cache. This route rebuilds the response rather than
        # passing the pipeline dict through, so without this a cached response
        # is indistinguishable from a fresh one: the id is a new uuid either
        # way. It went unnoticed because nothing ever wrote to the cache, so
        # is_cached was unreachable — now that it isn't, a caller comparing two
        # responses needs to be able to tell a hit from a provider call, and a
        # semantic hit (an answer to a question judged equivalent) from an exact
        # one.
        if resp.get("is_cached"):
            completion["x_cached"] = True
            completion["x_cache_type"] = resp.get("cache_type", "exact")
        return JSONResponse(completion)

    async def _stream(self, model, messages, temperature, max_tokens, top_p,
                      stop, system, user_id, project_id, smart_routing=False,
                      tools=None, tool_choice=None, authorized_project=None,
                      tenant_id=None, allow_legacy_project_lookup=False):
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())

        try:
            kwargs = {
                "temperature": temperature,
                "max_tokens": max_tokens,
                "top_p": top_p,
                "stop": stop,
                "system": system,
                "user_id": user_id,
                "project_id": project_id,
                "smart_routing": smart_routing,
                "tools": tools,
                "tool_choice": tool_choice,
            }
            if tenant_id is not None:
                kwargs["tenant_id"] = tenant_id
            if authorized_project is not None:
                kwargs["authorized_project"] = authorized_project
            if allow_legacy_project_lookup:
                kwargs["allow_legacy_project_lookup"] = True
            chunks = self.client_agent.chat_stream(model, messages, **kwargs)
        except Exception:
            logger.exception("stream setup failed")
            return _error(500, "Internal server error", err_type="server_error")

        async def event_generator():
            resolved_model = model
            first = True
            observed_finish: str | None = None
            saw_tool_call = False
            try:
                async for chunk in chunks:
                    if "_rate_limit_headers" in chunk:
                        continue
                    if "error" in chunk:
                        err = chunk["error"]
                        msg = err.get("message", "stream error") if isinstance(err, dict) else str(err)
                        yield f"data: {json.dumps({'error': {'message': msg, 'type': 'server_error'}})}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    if chunk.get("done"):
                        break
                    resolved_model = chunk.get("model") or resolved_model
                    delta: dict[str, Any] = {"content": chunk.get("content", "")}
                    if chunk.get("tool_calls"):
                        delta["tool_calls"] = chunk["tool_calls"]
                        saw_tool_call = True
                        # A tool-call delta carries no text. OpenAI sends
                        # content: null there, and clients accumulating
                        # `content or ""` would otherwise see "" and treat the
                        # turn as plain prose.
                        delta["content"] = chunk.get("content") or None
                    if chunk.get("finish_reason"):
                        observed_finish = chunk["finish_reason"]
                    if first:
                        delta["role"] = "assistant"
                        first = False
                    payload = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": resolved_model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                # Final chunk with finish_reason, then the [DONE] sentinel.
                # Carry the provider's reason when it gave one: a client driving
                # a tool loop branches on this, and a hardcoded "stop" ends the
                # loop before the tool ever runs.
                final = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": resolved_model,
                    "choices": [{"index": 0, "delta": {},
                                 "finish_reason": _finish_reason(observed_finish,
                                                                 saw_tool_call)}],
                }
                yield f"data: {json.dumps(final)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception:
                logger.exception("error during stream")
                yield f"data: {json.dumps({'error': {'message': 'stream failed', 'type': 'server_error'}})}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # ------------------------------------------------------------------
    # POST /v1/responses
    # ------------------------------------------------------------------

    async def responses(self, request: Request):
        try:
            body = await read_json_object(
                request,
                max_bytes=self.max_request_bytes,
            )
        except JSONBodyError as exc:
            return _error(exc.status_code, exc.message)

        try:
            translated = _translate_responses_request(body)
        except (ResponsesRequestError, TypeError, ValueError) as exc:
            return _error(400, str(exc))

        raw_model = translated["model"]
        smart_routing = (
            not isinstance(raw_model, str)
            or raw_model.strip().lower() in ("", "auto")
        )
        errors = self.request_validator.validate_payload(
            translated,
            allow_empty_model=smart_routing,
            check_model=False,
        )
        if errors:
            return _error(400, errors[0].message)

        model = raw_model
        assert isinstance(model, str)
        if smart_routing:
            model = ""

        user_id, project_id, tenant_id = _identity(request)
        authorized_project = _authorized_project(request)
        allow_legacy_project_lookup = _allow_legacy_project_lookup(request)
        if translated["stream"]:
            return await self._responses_stream(
                body=body,
                translated=translated,
                model=model,
                smart_routing=smart_routing,
                user_id=user_id,
                project_id=project_id,
                tenant_id=tenant_id,
                authorized_project=authorized_project,
                allow_legacy_project_lookup=allow_legacy_project_lookup,
            )
        return await self._responses_complete(
            body=body,
            translated=translated,
            model=model,
            smart_routing=smart_routing,
            user_id=user_id,
            project_id=project_id,
            tenant_id=tenant_id,
            authorized_project=authorized_project,
            allow_legacy_project_lookup=allow_legacy_project_lookup,
        )

    @staticmethod
    def _responses_chat_kwargs(
        *,
        translated: dict[str, Any],
        smart_routing: bool,
        user_id: str | None,
        project_id: str | None,
        tenant_id: str | None,
        authorized_project: Any,
        allow_legacy_project_lookup: bool,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "temperature": translated["temperature"],
            "max_tokens": translated["max_tokens"],
            "top_p": translated["top_p"],
            "system": translated["system"],
            "user_id": user_id,
            "project_id": project_id,
            "smart_routing": smart_routing,
            "tools": translated["tools"],
            "tool_choice": translated["tool_choice"],
        }
        if tenant_id is not None:
            kwargs["tenant_id"] = tenant_id
        if authorized_project is not None:
            kwargs["authorized_project"] = authorized_project
        if allow_legacy_project_lookup:
            kwargs["allow_legacy_project_lookup"] = True
        return kwargs

    async def _responses_complete(
        self,
        *,
        body: dict[str, Any],
        translated: dict[str, Any],
        model: str,
        smart_routing: bool,
        user_id: str | None,
        project_id: str | None,
        tenant_id: str | None,
        authorized_project: Any,
        allow_legacy_project_lookup: bool,
    ) -> JSONResponse:
        try:
            response = await self.client_agent.chat(
                model,
                translated["messages"],
                **self._responses_chat_kwargs(
                    translated=translated,
                    smart_routing=smart_routing,
                    user_id=user_id,
                    project_id=project_id,
                    tenant_id=tenant_id,
                    authorized_project=authorized_project,
                    allow_legacy_project_lookup=allow_legacy_project_lookup,
                ),
            )
        except Exception:
            logger.exception("Responses request failed")
            return _error(500, "Internal server error", err_type="server_error")

        response.pop("_rate_limit_headers", None)
        if "error" in response:
            error = response["error"]
            message = (
                error.get("message", "request failed")
                if isinstance(error, dict)
                else str(error)
            )
            status_code = response.get("status_code", 500)
            error_type = (
                "invalid_request_error"
                if isinstance(status_code, int) and status_code < 500
                else "server_error"
            )
            return _error(status_code, message, err_type=error_type)

        tool_calls = response.get("tool_calls")
        finish_reason = _finish_reason(
            response.get("finish_reason"),
            bool(tool_calls),
        )
        status = (
            "incomplete"
            if finish_reason in {"length", "content_filter"}
            else "completed"
        )
        created_at = int(time.time())
        payload = _responses_envelope(
            response_id=f"resp_{uuid.uuid4().hex}",
            created_at=created_at,
            body=body,
            model=response.get("model", model),
            output=_responses_output(response.get("content"), tool_calls),
            usage=_responses_usage(response.get("usage", {}) or {}),
            finish_reason=finish_reason,
            status=status,
        )
        if "smart_routing" in response:
            payload["x_smart_routing"] = response["smart_routing"]
        if response.get("is_cached"):
            payload["x_cached"] = True
            payload["x_cache_type"] = response.get("cache_type", "exact")
        return JSONResponse(payload)

    async def _responses_stream(
        self,
        *,
        body: dict[str, Any],
        translated: dict[str, Any],
        model: str,
        smart_routing: bool,
        user_id: str | None,
        project_id: str | None,
        tenant_id: str | None,
        authorized_project: Any,
        allow_legacy_project_lookup: bool,
    ) -> StreamingResponse:
        response_id = f"resp_{uuid.uuid4().hex}"
        created_at = int(time.time())
        try:
            chunks = self.client_agent.chat_stream(
                model,
                translated["messages"],
                **self._responses_chat_kwargs(
                    translated=translated,
                    smart_routing=smart_routing,
                    user_id=user_id,
                    project_id=project_id,
                    tenant_id=tenant_id,
                    authorized_project=authorized_project,
                    allow_legacy_project_lookup=allow_legacy_project_lookup,
                ),
            )
        except Exception:
            logger.exception("Responses stream setup failed")
            return StreamingResponse(
                iter(
                    [
                        "event: error\n"
                        f"data: {json.dumps({'type': 'error', 'code': 'server_error', 'message': 'Internal server error', 'param': None, 'sequence_number': 0})}\n\n"
                    ]
                ),
                media_type="text/event-stream",
            )

        async def event_generator():
            sequence_number = 0

            def event(event_type: str, **fields: Any) -> str:
                nonlocal sequence_number
                payload = {
                    "type": event_type,
                    "sequence_number": sequence_number,
                    **fields,
                }
                sequence_number += 1
                return (
                    f"event: {event_type}\n"
                    f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                )

            resolved_model = model
            initial = _responses_envelope(
                response_id=response_id,
                created_at=created_at,
                body=body,
                model=resolved_model,
                output=[],
                usage=None,
                finish_reason=None,
                status="in_progress",
            )
            yield event("response.created", response=initial)
            yield event("response.in_progress", response=initial)

            message_id = f"msg_{uuid.uuid4().hex}"
            message_started = False
            text = ""
            observed_finish: str | None = None
            tool_calls: dict[str, dict[str, Any]] = {}
            try:
                async for chunk in chunks:
                    if "_rate_limit_headers" in chunk:
                        continue
                    if "error" in chunk:
                        error = chunk["error"]
                        message = (
                            error.get("message", "stream error")
                            if isinstance(error, dict)
                            else str(error)
                        )
                        yield event(
                            "error",
                            code="server_error",
                            message=message,
                            param=None,
                        )
                        return
                    if chunk.get("done"):
                        break

                    resolved_model = chunk.get("model") or resolved_model
                    delta = chunk.get("content")
                    if isinstance(delta, str) and delta:
                        if not message_started:
                            message_started = True
                            yield event(
                                "response.output_item.added",
                                output_index=0,
                                item={
                                    "id": message_id,
                                    "type": "message",
                                    "status": "in_progress",
                                    "role": "assistant",
                                    "content": [],
                                },
                            )
                            yield event(
                                "response.content_part.added",
                                item_id=message_id,
                                output_index=0,
                                content_index=0,
                                part={
                                    "type": "output_text",
                                    "text": "",
                                    "annotations": [],
                                    "logprobs": [],
                                },
                            )
                        text += delta
                        yield event(
                            "response.output_text.delta",
                            item_id=message_id,
                            output_index=0,
                            content_index=0,
                            delta=delta,
                            logprobs=[],
                        )

                    streamed_calls = chunk.get("tool_calls")
                    if isinstance(streamed_calls, list):
                        for index, tool_call in enumerate(streamed_calls):
                            if not isinstance(tool_call, dict):
                                continue
                            call_id = (
                                tool_call.get("id")
                                or f"call_{len(tool_calls) + index}"
                            )
                            function = tool_call.get("function") or {}
                            tool_calls[call_id] = {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": function.get("name", ""),
                                    "arguments": function.get("arguments", "{}")
                                    or "{}",
                                },
                            }
                    if chunk.get("finish_reason"):
                        observed_finish = chunk["finish_reason"]
            except Exception:
                logger.exception("error during Responses stream")
                yield event(
                    "error",
                    code="server_error",
                    message="stream failed",
                    param=None,
                )
                return

            output: list[dict[str, Any]] = []
            output_index = 0
            if message_started or not tool_calls:
                message_item = {
                    "id": message_id,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": text,
                            "annotations": [],
                            "logprobs": [],
                        }
                    ],
                }
                if not message_started:
                    yield event(
                        "response.output_item.added",
                        output_index=output_index,
                        item={
                            **message_item,
                            "status": "in_progress",
                            "content": [],
                        },
                    )
                    yield event(
                        "response.content_part.added",
                        item_id=message_id,
                        output_index=output_index,
                        content_index=0,
                        part={
                            "type": "output_text",
                            "text": "",
                            "annotations": [],
                            "logprobs": [],
                        },
                    )
                yield event(
                    "response.output_text.done",
                    item_id=message_id,
                    output_index=output_index,
                    content_index=0,
                    text=text,
                    logprobs=[],
                )
                yield event(
                    "response.content_part.done",
                    item_id=message_id,
                    output_index=output_index,
                    content_index=0,
                    part=message_item["content"][0],
                )
                yield event(
                    "response.output_item.done",
                    output_index=output_index,
                    item=message_item,
                )
                output.append(message_item)
                output_index += 1

            for tool_call in tool_calls.values():
                function = tool_call["function"]
                item_id = f"fc_{uuid.uuid4().hex}"
                item = {
                    "id": item_id,
                    "call_id": tool_call["id"],
                    "type": "function_call",
                    "name": function["name"],
                    "arguments": function["arguments"],
                    "status": "completed",
                }
                yield event(
                    "response.output_item.added",
                    output_index=output_index,
                    item={**item, "arguments": "", "status": "in_progress"},
                )
                if function["arguments"]:
                    yield event(
                        "response.function_call_arguments.delta",
                        item_id=item_id,
                        output_index=output_index,
                        delta=function["arguments"],
                    )
                yield event(
                    "response.function_call_arguments.done",
                    item_id=item_id,
                    output_index=output_index,
                    arguments=function["arguments"],
                )
                yield event(
                    "response.output_item.done",
                    output_index=output_index,
                    item=item,
                )
                output.append(item)
                output_index += 1

            finish_reason = _finish_reason(
                observed_finish,
                bool(tool_calls),
            )
            status = (
                "incomplete"
                if finish_reason in {"length", "content_filter"}
                else "completed"
            )
            completed = _responses_envelope(
                response_id=response_id,
                created_at=created_at,
                body=body,
                model=resolved_model,
                output=output,
                usage=None,
                finish_reason=finish_reason,
                status=status,
            )
            event_type = (
                "response.incomplete"
                if status == "incomplete"
                else "response.completed"
            )
            yield event(event_type, response=completed)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # ------------------------------------------------------------------
    # POST /v1/embeddings
    # ------------------------------------------------------------------

    async def embeddings(self, request: Request) -> JSONResponse:
        try:
            body = await read_json_object(
                request,
                max_bytes=self.max_request_bytes,
            )
        except JSONBodyError as exc:
            return _error(exc.status_code, exc.message)

        model = body.get("model")
        input_value = body.get("input")
        encoding_format = body.get("encoding_format", "float")
        dimensions = body.get("dimensions")
        user = body.get("user")
        if not isinstance(model, str) or not model.strip():
            return _error(400, "Field 'model' is required.")
        if not isinstance(input_value, (str, list)):
            return _error(
                400,
                "Field 'input' must be a string or a list of strings.",
            )
        if encoding_format not in {"float", "base64"}:
            return _error(
                400,
                "Field 'encoding_format' must be 'float' or 'base64'.",
            )

        user_id, project_id, tenant_id = _identity(request)
        try:
            response = await self.client_agent.embeddings(
                model,
                input_value,
                encoding_format=encoding_format,
                dimensions=dimensions,
                user=user,
                user_id=user_id,
                project_id=project_id,
                tenant_id=tenant_id,
                authorized_project=_authorized_project(request),
                allow_legacy_project_lookup=(
                    _allow_legacy_project_lookup(request)
                ),
            )
        except Exception:
            logger.exception("embeddings request failed")
            return _error(
                500,
                "Internal server error",
                err_type="server_error",
            )

        response.pop("_rate_limit_headers", None)
        if "error" in response:
            error = response["error"]
            message = (
                error.get("message", "request failed")
                if isinstance(error, dict)
                else str(error)
            )
            status_code = response.get("status_code", 500)
            error_type = (
                "invalid_request_error"
                if isinstance(status_code, int) and status_code < 500
                else "server_error"
            )
            return _error(status_code, message, err_type=error_type)
        return JSONResponse(response)

    # ------------------------------------------------------------------
    # GET /v1/models
    # ------------------------------------------------------------------

    async def list_models(self, request: Request) -> JSONResponse:
        user_id, project_id, tenant_id = _identity(request)
        try:
            kwargs = {"project_id": project_id, "user_id": user_id}
            if tenant_id is not None:
                kwargs["tenant_id"] = tenant_id
            project = _authorized_project(request)
            if project is not None:
                kwargs["authorized_project"] = project
            elif _allow_legacy_project_lookup(request):
                kwargs["allow_legacy_project_lookup"] = True
            models = await self.client_agent.list_models(**kwargs)
        except Exception:
            logger.exception("list models failed")
            return _error(500, "Internal server error", err_type="server_error")

        created = int(time.time())
        data = [
            {
                "id": m["name"] if isinstance(m, dict) else str(m),
                "object": "model",
                "created": created,
                "owned_by": "axonllm",
            }
            for m in models
        ]
        return JSONResponse({"object": "list", "data": data})


def create_openai_routes(api: OpenAICompatAPI) -> list[Route]:
    """Return Starlette routes for the OpenAI-compatible surface."""
    return [
        Route("/v1/chat/completions", api.chat_completions, methods=["POST"]),
        Route("/v1/responses", api.responses, methods=["POST"]),
        Route("/v1/embeddings", api.embeddings, methods=["POST"]),
        Route("/v1/models", api.list_models, methods=["GET"]),
    ]
