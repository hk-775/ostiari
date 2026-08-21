"""Governed OpenAI ``POST /v1/responses`` compatibility surface.

Ostiari remains stateless at this boundary: requests that depend on OpenAI-side
stored conversations, prompts, or background execution are rejected explicitly.
Supported text, image-URL, and function-call inputs are translated into the same
authorization, security, quota, AxonLLM routing, cost, and tracing path used by
``/v1/chat/completions``.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from ostiari_gateway.modules.llm_gateway.chat_proxy import ChatProxy, _err


class ResponsesRequestError(ValueError):
    """A caller-correctable Responses request error."""


class _TranslatedRequest:
    """Request view that preserves verified identity while replacing JSON."""

    def __init__(self, request: Request, body: dict[str, Any]) -> None:
        self.headers = request.headers
        self.state = request.state
        self.url = request.url
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body


def _content(content: Any, *, field: str) -> str | list[dict[str, Any]]:
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


def _text_content(content: Any, *, field: str) -> str:
    translated = _content(content, field=field)
    if isinstance(translated, str):
        return translated
    raise ResponsesRequestError(
        "System and developer Responses input must contain text only."
    )


def _input(input_value: Any) -> tuple[list[dict[str, Any]], list[str]]:
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
                    _text_content(content, field=f"{field}.content")
                )
            else:
                messages.append(
                    {
                        "role": role,
                        "content": _content(content, field=f"{field}.content"),
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


def _tools(tools: Any) -> list[dict[str, Any]] | None:
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
                "Only function tools are supported by the embedded AxonLLM router."
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


def _tool_choice(choice: Any) -> str | dict[str, Any] | None:
    if choice is None or isinstance(choice, str):
        return choice
    if not isinstance(choice, dict):
        raise ResponsesRequestError(
            "Field 'tool_choice' must be a string or an object."
        )
    if choice.get("type") != "function":
        raise ResponsesRequestError(
            "Only function tool choices are supported by the embedded AxonLLM router."
        )
    name = choice.get("name")
    if not isinstance(name, str) or not name:
        raise ResponsesRequestError(
            "A function tool choice must include a non-empty name."
        )
    return {"type": "function", "function": {"name": name}}


_ENCRYPTED_REASONING_INCLUDE = ["reasoning.encrypted_content"]


def _supported_reasoning_metadata(value: Any) -> bool:
    if value in (None, {}):
        return True
    if not isinstance(value, dict) or not value:
        return False
    if not set(value) <= {"context", "effort", "summary"}:
        return False
    if value.get("context") not in (None, "all_turns"):
        return False
    return all(value.get(field) in (None, "none") for field in ("effort", "summary"))


def _translate(body: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    for field in ("previous_response_id", "conversation", "prompt"):
        if body.get(field) is not None:
            raise ResponsesRequestError(
                f"Field '{field}' is stateful and is not supported by Ostiari."
            )
    if body.get("store") is True:
        raise ResponsesRequestError(
            "Ostiari's Responses endpoint is stateless; field 'store' must be false or omitted."
        )
    if body.get("background") is True:
        raise ResponsesRequestError("Field 'background' is not supported.")
    reasoning = body.get("reasoning")
    include = body.get("include")
    if include not in (None, [], _ENCRYPTED_REASONING_INCLUDE):
        raise ResponsesRequestError("Field 'include' is not supported.")
    if not _supported_reasoning_metadata(reasoning):
        raise ResponsesRequestError(
            "Responses reasoning is unsupported except for transport-only "
            "context='all_turns' and effort/summary='none'."
        )
    reasoning_context = reasoning.get("context") if isinstance(reasoning, dict) else None
    if reasoning_context == "all_turns" and include != _ENCRYPTED_REASONING_INCLUDE:
        raise ResponsesRequestError(
            "Responses reasoning.context='all_turns' requires "
            "include=['reasoning.encrypted_content']."
        )
    if include == _ENCRYPTED_REASONING_INCLUDE and reasoning_context != "all_turns":
        raise ResponsesRequestError(
            "Field 'include' requests encrypted reasoning content without "
            "reasoning.context='all_turns'."
        )
    if body.get("text") not in (None, {}):
        raise ResponsesRequestError(
            "Responses structured text configuration is not supported yet."
        )
    for field in ("max_tool_calls", "service_tier"):
        if body.get(field) is not None:
            raise ResponsesRequestError(f"Field '{field}' is not supported.")
    parallel_tool_calls = body.get("parallel_tool_calls")
    if parallel_tool_calls is not None and not isinstance(
        parallel_tool_calls,
        bool,
    ):
        raise ResponsesRequestError("Field 'parallel_tool_calls' must be a boolean.")
    if body.get("truncation") not in (None, "disabled"):
        raise ResponsesRequestError("Only truncation='disabled' is supported.")
    if "input" not in body:
        raise ResponsesRequestError("Field 'input' is required.")
    if "model" in body and not isinstance(body["model"], str):
        raise ResponsesRequestError("Field 'model' must be a string.")
    if "stream" in body and not isinstance(body["stream"], bool):
        raise ResponsesRequestError("Field 'stream' must be a boolean.")
    max_output_tokens = body.get("max_output_tokens")
    if max_output_tokens is not None and (
        not isinstance(max_output_tokens, int)
        or isinstance(max_output_tokens, bool)
        or max_output_tokens <= 0
    ):
        raise ResponsesRequestError(
            "Field 'max_output_tokens' must be a positive integer."
        )
    metadata = body.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ResponsesRequestError("Field 'metadata' must be an object.")

    messages, input_instructions = _input(body["input"])
    instructions = body.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        raise ResponsesRequestError("Field 'instructions' must be a string.")
    system_parts = ([instructions] if instructions else []) + input_instructions
    if system_parts:
        messages.insert(
            0,
            {"role": "system", "content": "\n\n".join(system_parts)},
        )

    chat: dict[str, Any] = {
        "model": body.get("model", ""),
        "messages": messages,
        "stream": False,
    }
    for response_key, chat_key in (
        ("temperature", "temperature"),
        ("max_output_tokens", "max_tokens"),
        ("top_p", "top_p"),
    ):
        if body.get(response_key) is not None:
            chat[chat_key] = body[response_key]
    translated_tools = _tools(body.get("tools"))
    translated_choice = _tool_choice(body.get("tool_choice"))
    if translated_tools is not None:
        chat["tools"] = translated_tools
    if translated_choice is not None:
        chat["tool_choice"] = translated_choice
    return chat, bool(body.get("stream", False))


def _output(completion: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    choices = completion.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    if not isinstance(message, dict):
        message = {}
    finish_reason = str(choice.get("finish_reason") or "stop")
    tool_calls = message.get("tool_calls")
    output: list[dict[str, Any]] = []
    content = message.get("content")
    if isinstance(content, str) and (content or not tool_calls):
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
            function = tool_call.get("function")
            if not isinstance(function, dict):
                function = {}
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
    return output, finish_reason


def _usage(completion: dict[str, Any]) -> dict[str, Any]:
    usage = completion.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = int(usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(usage.get("completion_tokens", 0) or 0)
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": int(
            usage.get("total_tokens", input_tokens + output_tokens)
            or input_tokens + output_tokens
        ),
    }


def _envelope(
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


def _response_from_completion(
    body: dict[str, Any],
    completion: dict[str, Any],
) -> dict[str, Any]:
    output, finish_reason = _output(completion)
    status = (
        "incomplete"
        if finish_reason in {"length", "content_filter"}
        else "completed"
    )
    return _envelope(
        response_id=f"resp_{uuid.uuid4().hex}",
        created_at=int(time.time()),
        body=body,
        model=str(completion.get("model") or body.get("model") or ""),
        output=output,
        usage=_usage(completion),
        finish_reason=finish_reason,
        status=status,
    )


def _tool_call_count(completion: dict[str, Any]) -> int:
    choices = completion.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    if not isinstance(message, dict):
        return 0
    tool_calls = message.get("tool_calls")
    return len(tool_calls) if isinstance(tool_calls, list) else 0


def _sse(response: dict[str, Any]):
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

    initial = {
        **response,
        "status": "in_progress",
        "completed_at": None,
        "output": [],
        "usage": None,
    }
    yield event("response.created", response=initial)
    yield event("response.in_progress", response=initial)

    for output_index, item in enumerate(response["output"]):
        if item["type"] == "message":
            part = item["content"][0]
            yield event(
                "response.output_item.added",
                output_index=output_index,
                item={**item, "status": "in_progress", "content": []},
            )
            yield event(
                "response.content_part.added",
                item_id=item["id"],
                output_index=output_index,
                content_index=0,
                part={**part, "text": ""},
            )
            if part["text"]:
                yield event(
                    "response.output_text.delta",
                    item_id=item["id"],
                    output_index=output_index,
                    content_index=0,
                    delta=part["text"],
                    logprobs=[],
                )
            yield event(
                "response.output_text.done",
                item_id=item["id"],
                output_index=output_index,
                content_index=0,
                text=part["text"],
                logprobs=[],
            )
            yield event(
                "response.content_part.done",
                item_id=item["id"],
                output_index=output_index,
                content_index=0,
                part=part,
            )
            yield event(
                "response.output_item.done",
                output_index=output_index,
                item=item,
            )
            continue

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

    event_type = (
        "response.incomplete"
        if response["status"] == "incomplete"
        else "response.completed"
    )
    yield event(event_type, response=response)


class ResponsesProxy:
    """Translate Responses requests onto Ostiari's governed OpenAI route."""

    def __init__(self, chat_proxy: ChatProxy) -> None:
        self._chat = chat_proxy

    def update(self, *, config: Any, axon: Any, security: Any) -> None:
        self._chat._config = config
        self._chat._axon = axon
        self._chat._security = security

    async def handle(self, request: Request) -> Any:
        try:
            body = await request.json()
        except Exception:
            return _err(400, "Malformed JSON body")
        if not isinstance(body, dict):
            return _err(400, "Body must be a JSON object")
        try:
            chat_body, streaming = _translate(body)
        except (ResponsesRequestError, TypeError, ValueError) as exc:
            return _err(400, str(exc))

        chat_response = await self._chat.handle(
            _TranslatedRequest(request, chat_body)
        )
        if not isinstance(chat_response, JSONResponse):
            return _err(502, "Invalid governed router response", "api_error")
        if chat_response.status_code >= 300:
            return chat_response
        try:
            completion = json.loads(chat_response.body)
        except (TypeError, ValueError, json.JSONDecodeError):
            return _err(502, "Invalid governed router response", "api_error")
        if not isinstance(completion, dict):
            return _err(502, "Invalid governed router response", "api_error")
        if (
            body.get("parallel_tool_calls") is False
            and _tool_call_count(completion) > 1
        ):
            return _err(
                502,
                "Upstream returned multiple tool calls while "
                "parallel_tool_calls=false.",
                "api_error",
            )

        response = _response_from_completion(body, completion)
        if not streaming:
            return JSONResponse(status_code=200, content=response)
        return StreamingResponse(_sse(response), media_type="text/event-stream")
