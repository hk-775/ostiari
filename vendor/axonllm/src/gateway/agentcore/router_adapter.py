"""Inference-only public contract for the AgentCore Runtime entrypoint."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from src.gateway.chat.openai_routes import (
    ResponsesRequestError,
    _finish_reason,
    _responses_envelope,
    _responses_output,
    _responses_usage,
    _translate_responses_request,
)

from .errors import AgentCoreAdapterError
from .schemas import AUTHORITY_FIELDS, CHAT_FIELDS, EMBEDDING_FIELDS

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
RESPONSES_PATH = "/v1/responses"
EMBEDDINGS_PATH = "/v1/embeddings"
MODELS_PATH = "/v1/models"

_PUBLIC_ROUTES = frozenset(
    {
        ("POST", CHAT_COMPLETIONS_PATH),
        ("POST", RESPONSES_PATH),
        ("POST", EMBEDDINGS_PATH),
        ("GET", MODELS_PATH),
    }
)
_PUBLIC_ENVELOPE_FIELDS = frozenset({"method", "path", "body"})
_RESPONSES_FIELDS = frozenset(
    {
        "background",
        "conversation",
        "include",
        "input",
        "instructions",
        "max_output_tokens",
        "max_tool_calls",
        "metadata",
        "model",
        "parallel_tool_calls",
        "previous_response_id",
        "prompt",
        "reasoning",
        "service_tier",
        "store",
        "stream",
        "temperature",
        "text",
        "tool_choice",
        "tools",
        "top_p",
        "truncation",
        "user",
    }
)


class InternalAgentCoreAdapter(Protocol):
    """Hardened internal operations used by the public router boundary."""

    async def initialize(self) -> None: ...

    async def readiness(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...

    async def invoke(self, payload: Any, context: Any) -> Any: ...


@dataclass(frozen=True)
class RouterInvocation:
    """One strictly validated OpenAI-compatible operation."""

    method: str
    path: str
    body: dict[str, Any]


def _invalid_payload(message: str) -> AgentCoreAdapterError:
    return AgentCoreAdapterError(400, "invalid_payload", message)


def _validate_body_fields(
    body: dict[str, Any],
    allowed_fields: frozenset[str],
) -> None:
    supplied_authority = sorted(AUTHORITY_FIELDS.intersection(body))
    if supplied_authority:
        raise AgentCoreAdapterError(
            400,
            "untrusted_identity_fields",
            "Identity and authorization fields are not accepted in payloads.",
        )
    unexpected = sorted(set(body).difference(allowed_fields))
    if unexpected:
        raise _invalid_payload(
            "Request body contains unsupported fields: "
            + ", ".join(unexpected)
            + "."
        )


def parse_router_invocation(payload: Any) -> RouterInvocation:
    """Validate the public method/path envelope without accepting legacy actions."""
    if type(payload) is not dict:
        raise _invalid_payload("Invocation payload must be a JSON object.")
    if any(not isinstance(key, str) for key in payload):
        raise _invalid_payload("Invocation payload keys must be strings.")

    unexpected = sorted(set(payload).difference(_PUBLIC_ENVELOPE_FIELDS))
    if unexpected:
        raise _invalid_payload(
            "Invocation payload contains unsupported fields: "
            + ", ".join(unexpected)
            + "."
        )

    method = payload.get("method")
    path = payload.get("path")
    if not isinstance(method, str) or method not in {"GET", "POST"}:
        raise _invalid_payload("Field 'method' must be 'GET' or 'POST'.")
    if not isinstance(path, str) or (method, path) not in _PUBLIC_ROUTES:
        raise AgentCoreAdapterError(
            404,
            "route_not_found",
            "The requested AgentCore router path is not available.",
        )

    raw_body = payload.get("body")
    if method == "GET":
        if raw_body not in (None, {}):
            raise _invalid_payload("GET requests must not include a body.")
        body: dict[str, Any] = {}
    else:
        if type(raw_body) is not dict:
            raise _invalid_payload(
                "POST requests must include a JSON object in field 'body'."
            )
        if any(not isinstance(key, str) for key in raw_body):
            raise _invalid_payload("Request body keys must be strings.")
        body = dict(raw_body)

    if path == CHAT_COMPLETIONS_PATH:
        _validate_body_fields(body, CHAT_FIELDS)
    elif path == RESPONSES_PATH:
        _validate_body_fields(body, _RESPONSES_FIELDS)
    elif path == EMBEDDINGS_PATH:
        _validate_body_fields(body, EMBEDDING_FIELDS)
    return RouterInvocation(method=method, path=path, body=body)


def _raise_gateway_error(response: dict[str, Any]) -> None:
    error = response.get("error")
    if error is None:
        return
    status_code = response.get("status_code", 500)
    if not isinstance(status_code, int) or isinstance(status_code, bool):
        status_code = 500
    if isinstance(error, dict):
        code = error.get("code") or error.get("type") or "gateway_error"
        message = error.get("message") or "Gateway request failed."
    else:
        code = "gateway_error"
        message = str(error)
    raise AgentCoreAdapterError(
        status_code,
        str(code),
        str(message),
    )


def _assistant_choice(
    response: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AgentCoreAdapterError(
            502,
            "invalid_gateway_response",
            "Gateway returned an invalid inference response.",
        )
    choice = choices[0]
    if not isinstance(choice, dict):
        raise AgentCoreAdapterError(
            502,
            "invalid_gateway_response",
            "Gateway returned an invalid inference response.",
        )
    message = choice.get("message")
    if not isinstance(message, dict):
        raise AgentCoreAdapterError(
            502,
            "invalid_gateway_response",
            "Gateway returned an invalid inference response.",
        )
    tool_calls = message.get("tool_calls")
    finish_reason = _finish_reason(
        choice.get("finish_reason"),
        isinstance(tool_calls, list) and bool(tool_calls),
    )
    return message, finish_reason


def _chat_completion(
    response: dict[str, Any],
    *,
    requested_model: str,
) -> dict[str, Any]:
    response = dict(response)
    response.pop("_rate_limit_headers", None)
    _raise_gateway_error(response)
    raw_choices = response.get("choices")
    if not isinstance(raw_choices, list) or not raw_choices:
        raise AgentCoreAdapterError(
            502,
            "invalid_gateway_response",
            "Gateway returned an invalid inference response.",
        )

    choices: list[dict[str, Any]] = []
    for position, raw_choice in enumerate(raw_choices):
        if not isinstance(raw_choice, dict):
            continue
        message = raw_choice.get("message")
        if not isinstance(message, dict):
            continue
        tool_calls = message.get("tool_calls")
        choices.append(
            {
                "index": raw_choice.get("index", position),
                "message": message,
                "finish_reason": _finish_reason(
                    raw_choice.get("finish_reason"),
                    isinstance(tool_calls, list) and bool(tool_calls),
                ),
            }
        )
    if not choices:
        raise AgentCoreAdapterError(
            502,
            "invalid_gateway_response",
            "Gateway returned an invalid inference response.",
        )

    usage = response.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    payload: dict[str, Any] = {
        "id": response.get("id") or f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": response.get("model") or requested_model,
        "choices": choices,
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }
    if "smart_routing" in response:
        payload["x_smart_routing"] = response["smart_routing"]
    if response.get("is_cached"):
        payload["x_cached"] = True
        payload["x_cache_type"] = response.get("cache_type", "exact")
    return payload


def _responses_completion(
    response: dict[str, Any],
    *,
    body: dict[str, Any],
    requested_model: str,
) -> dict[str, Any]:
    response = dict(response)
    response.pop("_rate_limit_headers", None)
    _raise_gateway_error(response)
    message, finish_reason = _assistant_choice(response)
    status = (
        "incomplete"
        if finish_reason in {"length", "content_filter"}
        else "completed"
    )
    payload = _responses_envelope(
        response_id=f"resp_{uuid.uuid4().hex}",
        created_at=int(time.time()),
        body=body,
        model=response.get("model") or requested_model,
        output=_responses_output(
            message.get("content"),
            message.get("tool_calls"),
        ),
        usage=_responses_usage(response.get("usage", {}) or {}),
        finish_reason=finish_reason,
        status=status,
    )
    if "smart_routing" in response:
        payload["x_smart_routing"] = response["smart_routing"]
    if response.get("is_cached"):
        payload["x_cached"] = True
        payload["x_cache_type"] = response.get("cache_type", "exact")
    return payload


def _models_response(response: dict[str, Any]) -> dict[str, Any]:
    models = response.get("models")
    if not isinstance(models, list):
        raise AgentCoreAdapterError(
            502,
            "invalid_gateway_response",
            "Gateway returned an invalid model list.",
        )
    created = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": (
                    model.get("name", "")
                    if isinstance(model, dict)
                    else str(model)
                ),
                "object": "model",
                "created": created,
                "owned_by": "axonllm",
            }
            for model in models
        ],
    }


def _stream_data(event: Any) -> Any:
    if isinstance(event, dict) and set(event) == {"data"}:
        return event["data"]
    return event


async def _close_stream(stream: Any) -> None:
    close = getattr(stream, "aclose", None)
    if callable(close):
        await close()


async def _chat_completion_stream(
    stream: AsyncIterator[dict[str, Any]],
    *,
    requested_model: str,
) -> AsyncIterator[dict[str, Any]]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    resolved_model = requested_model
    first = True
    done = False
    try:
        async for event in stream:
            if isinstance(event, dict) and "_rate_limit_headers" in event:
                continue
            data = _stream_data(event)
            if data == "[DONE]":
                done = True
                yield {"event": "done", "data": "[DONE]"}
                break
            if not isinstance(data, dict):
                continue
            if "error" in data:
                error = data["error"]
                message = (
                    error.get("message", "stream failed")
                    if isinstance(error, dict)
                    else str(error)
                )
                yield {
                    "event": "error",
                    "data": {
                        "error": {
                            "message": message,
                            "type": "server_error",
                        }
                    },
                }
                done = True
                yield {"event": "done", "data": "[DONE]"}
                break

            resolved_model = data.get("model") or resolved_model
            raw_choices = data.get("choices")
            if not isinstance(raw_choices, list):
                continue
            choices: list[dict[str, Any]] = []
            for position, raw_choice in enumerate(raw_choices):
                if not isinstance(raw_choice, dict):
                    continue
                delta = raw_choice.get("delta")
                if not isinstance(delta, dict):
                    delta = {}
                else:
                    delta = dict(delta)
                if first and "role" not in delta:
                    delta["role"] = "assistant"
                tool_calls = delta.get("tool_calls")
                raw_finish = raw_choice.get("finish_reason")
                choices.append(
                    {
                        "index": raw_choice.get("index", position),
                        "delta": delta,
                        "finish_reason": (
                            _finish_reason(
                                raw_finish,
                                isinstance(tool_calls, list)
                                and bool(tool_calls),
                            )
                            if raw_finish is not None
                            else None
                        ),
                    }
                )
            if not choices:
                continue
            first = False
            yield {
                "event": "data",
                "data": {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": resolved_model,
                    "choices": choices,
                },
            }
    finally:
        await _close_stream(stream)
    if not done:
        yield {"event": "done", "data": "[DONE]"}


def _merge_tool_call(
    calls: dict[int, dict[str, Any]],
    raw_call: dict[str, Any],
    position: int,
) -> None:
    index = raw_call.get("index", position)
    if not isinstance(index, int) or isinstance(index, bool):
        index = position
    call = calls.setdefault(
        index,
        {
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        },
    )
    if isinstance(raw_call.get("id"), str):
        call["id"] = raw_call["id"]
    function = raw_call.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        arguments = function.get("arguments")
        if isinstance(name, str):
            call["function"]["name"] += name
        if isinstance(arguments, str):
            call["function"]["arguments"] += arguments


async def _responses_stream(
    stream: AsyncIterator[dict[str, Any]],
    *,
    body: dict[str, Any],
    requested_model: str,
) -> AsyncIterator[dict[str, Any]]:
    response_id = f"resp_{uuid.uuid4().hex}"
    created_at = int(time.time())
    resolved_model = requested_model
    sequence_number = 0
    message_id = f"msg_{uuid.uuid4().hex}"
    message_started = False
    text = ""
    observed_finish: str | None = None
    tool_calls: dict[int, dict[str, Any]] = {}

    def event(event_type: str, **fields: Any) -> dict[str, Any]:
        nonlocal sequence_number
        payload = {
            "type": event_type,
            "sequence_number": sequence_number,
            **fields,
        }
        sequence_number += 1
        return {"event": event_type, "data": payload}

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

    try:
        async for raw_event in stream:
            if (
                isinstance(raw_event, dict)
                and "_rate_limit_headers" in raw_event
            ):
                continue
            data = _stream_data(raw_event)
            if data == "[DONE]":
                break
            if not isinstance(data, dict):
                continue
            if "error" in data:
                error = data["error"]
                message = (
                    error.get("message", "stream failed")
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

            resolved_model = data.get("model") or resolved_model
            choices = data.get("choices")
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    delta = {}
                content = delta.get("content")
                if isinstance(content, str) and content:
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
                    text += content
                    yield event(
                        "response.output_text.delta",
                        item_id=message_id,
                        output_index=0,
                        content_index=0,
                        delta=content,
                        logprobs=[],
                    )
                raw_calls = delta.get("tool_calls")
                if isinstance(raw_calls, list):
                    for position, raw_call in enumerate(raw_calls):
                        if isinstance(raw_call, dict):
                            _merge_tool_call(
                                tool_calls,
                                raw_call,
                                position,
                            )
                if choice.get("finish_reason") is not None:
                    observed_finish = choice["finish_reason"]
    finally:
        await _close_stream(stream)

    output: list[dict[str, Any]] = []
    output_index = 0
    if message_started or not tool_calls:
        message = {
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
                    **message,
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
            part=message["content"][0],
        )
        yield event(
            "response.output_item.done",
            output_index=output_index,
            item=message,
        )
        output.append(message)
        output_index += 1

    for tool_call in tool_calls.values():
        function = tool_call["function"]
        item = {
            "id": f"fc_{uuid.uuid4().hex}",
            "call_id": tool_call["id"] or f"call_{output_index}",
            "type": "function_call",
            "name": function["name"],
            "arguments": function["arguments"] or "{}",
            "status": "completed",
        }
        yield event(
            "response.output_item.added",
            output_index=output_index,
            item={**item, "arguments": "", "status": "in_progress"},
        )
        if item["arguments"]:
            yield event(
                "response.function_call_arguments.delta",
                item_id=item["id"],
                output_index=output_index,
                delta=item["arguments"],
            )
        yield event(
            "response.function_call_arguments.done",
            item_id=item["id"],
            output_index=output_index,
            arguments=item["arguments"],
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


class AgentCoreRouterAdapter:
    """Expose only the supported OpenAI-compatible router paths."""

    def __init__(self, internal_adapter: InternalAgentCoreAdapter) -> None:
        self._internal_adapter = internal_adapter

    async def initialize(self) -> None:
        await self._internal_adapter.initialize()

    async def readiness(self) -> dict[str, Any]:
        return await self._internal_adapter.readiness()

    async def close(self) -> None:
        await self._internal_adapter.close()

    async def invoke(self, payload: Any, context: Any) -> Any:
        invocation = parse_router_invocation(payload)
        body = invocation.body

        if invocation.path == MODELS_PATH:
            response = await self._internal_adapter.invoke(
                {"action": "list_models"},
                context,
            )
            if not isinstance(response, dict):
                raise AgentCoreAdapterError(
                    502,
                    "invalid_gateway_response",
                    "Gateway returned an invalid model list.",
                )
            return _models_response(response)

        if invocation.path == EMBEDDINGS_PATH:
            response = await self._internal_adapter.invoke(
                {"action": "embeddings", **body},
                context,
            )
            if not isinstance(response, dict):
                raise AgentCoreAdapterError(
                    502,
                    "invalid_gateway_response",
                    "Gateway returned an invalid embeddings response.",
                )
            response = dict(response)
            response.pop("_rate_limit_headers", None)
            _raise_gateway_error(response)
            return response

        if invocation.path == RESPONSES_PATH:
            try:
                translated = _translate_responses_request(body)
            except (ResponsesRequestError, TypeError, ValueError) as exc:
                raise _invalid_payload(str(exc)) from exc
            internal_payload = {
                "action": "chat",
                **{
                    field: translated[field]
                    for field in CHAT_FIELDS
                    if field in translated
                },
            }
            response = await self._internal_adapter.invoke(
                internal_payload,
                context,
            )
            requested_model = translated.get("model", "")
            if not isinstance(requested_model, str):
                requested_model = ""
            if hasattr(response, "__aiter__"):
                return _responses_stream(
                    response,
                    body=body,
                    requested_model=requested_model,
                )
            if not isinstance(response, dict):
                raise AgentCoreAdapterError(
                    502,
                    "invalid_gateway_response",
                    "Gateway returned an invalid inference response.",
                )
            return _responses_completion(
                response,
                body=body,
                requested_model=requested_model,
            )

        response = await self._internal_adapter.invoke(
            {"action": "chat", **body},
            context,
        )
        requested_model = body.get("model", "")
        if not isinstance(requested_model, str):
            requested_model = ""
        if hasattr(response, "__aiter__"):
            return _chat_completion_stream(
                response,
                requested_model=requested_model,
            )
        if not isinstance(response, dict):
            raise AgentCoreAdapterError(
                502,
                "invalid_gateway_response",
                "Gateway returned an invalid inference response.",
            )
        return _chat_completion(
            response,
            requested_model=requested_model,
        )


__all__ = [
    "AgentCoreRouterAdapter",
    "CHAT_COMPLETIONS_PATH",
    "EMBEDDINGS_PATH",
    "MODELS_PATH",
    "RESPONSES_PATH",
    "RouterInvocation",
    "parse_router_invocation",
]
