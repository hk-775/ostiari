"""Bedrock Mantle provider — routes to the right Mantle API by model via SigV4.

Uses the bedrock-mantle.{region}.api.aws endpoint, which exposes three
inference APIs, each serving a different subset of models:
- Anthropic Messages API (/anthropic/v1/messages) — Claude models (anthropic.*)
- OpenAI Responses API (/openai/v1/responses) — frontier GPT models (gpt-5.x)
- Chat Completions API (/v1/chat/completions) — everything else, including
  gpt-oss, DeepSeek, Qwen, and other open-weight families

Model IDs are prefixed by family (anthropic.*, openai.*, deepseek.*, qwen.*,
...). The prefix does NOT uniquely determine the API: openai.gpt-5.6-* uses
the Responses API while openai.gpt-oss-* uses Chat Completions. We therefore
pick a preferred API by heuristic and fall back to Chat Completions when the
provider reports the model is not supported on the chosen route.

Tool calling means three dialects, not one. This module hand-builds each payload
rather than going through the adapter layer, so it also has to do its own tool
translation — and the three routes disagree on shape:

    route              tools[]                          arguments   history
    /anthropic/…       {name, input_schema}             object      content blocks
    /openai/v1/resp…   {type, name, parameters} (flat)  JSON str    flat input items
    /v1/chat/compl…    {type, function:{name, …}}       JSON str    OpenAI messages

Only Chat Completions takes the gateway's own OpenAI-shaped tool traffic
unchanged. The other two reject it outright rather than degrading: the Messages
API answers 400 ``Unexpected role "tool"``, and the Responses API answers 400
``Invalid 'input'``. The Responses API is the odd one — its tool spec is *flat*
(``name``/``parameters`` at the top level, no ``function`` wrapper), and sending
the nested Chat Completions form gets 400 ``Invalid 'tools': missing field
`name```, with nested ``tool_choice`` likewise rejected. That shape is easy to
assume and wrong, which is why it is written down here.

Auth: SigV4 (bedrock service). All billing flows through the AWS account.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import math
import ssl
from typing import Awaitable, Callable

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import urllib3

from src.gateway.adapters.anthropic_style import (
    openai_msg_to_anthropic,
    openai_tool_choice_to_anthropic,
    openai_tool_to_anthropic,
)
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ProviderModelMapping,
    TokenUsage,
)
from src.gateway.router import ProviderError

_MANTLE_SERVICE = "bedrock"
_MANTLE_HTTP = urllib3.PoolManager(
    num_pools=10,
    maxsize=100,
    block=True,
    ssl_context=ssl.create_default_context(),
)

_ANTHROPIC_PREFIXES = ("anthropic.",)
# openai.* models split across two APIs: the frontier gpt-5.x line uses the
# Responses API; open-weight openai.gpt-oss-* uses Chat Completions.
_RESPONSES_PREFIXES = ("openai.gpt-5", "openai.gpt-4", "openai.o1", "openai.o3", "openai.o4")
_MAX_MANTLE_RESPONSE_BYTES = 10 * 1024 * 1024
_MANTLE_READ_CHUNK_BYTES = 64 * 1024
_UNSUPPORTED_ROUTE_MESSAGE = "Mantle model is not supported on this route"


def _is_anthropic_model(model_id: str) -> bool:
    return any(model_id.startswith(p) for p in _ANTHROPIC_PREFIXES)


def _prefers_responses_api(model_id: str) -> bool:
    return any(model_id.startswith(p) for p in _RESPONSES_PREFIXES)


def _is_unsupported_route_error(exc: ProviderError) -> bool:
    """True when Mantle rejects a model for the chosen API path (not a real failure)."""
    msg = exc.message.lower()
    return exc.status_code == 400 and (
        msg == _UNSUPPORTED_ROUTE_MESSAGE.lower()
        or "does not support" in msg
        or "isn't supported on this route" in msg
    )


def _mantle_http_error(status: int, data: object) -> ProviderError:
    raw = data if isinstance(data, bytes) else str(data).encode(
        "utf-8",
        errors="replace",
    )
    bounded = raw[:64 * 1024].decode("utf-8", errors="replace").casefold()
    if status == 400 and (
        "does not support" in bounded
        or "isn't supported on this route" in bounded
    ):
        message = _UNSUPPORTED_ROUTE_MESSAGE
    else:
        message = f"Mantle HTTP request failed with status {status}"
    return ProviderError(status, "bedrock-mantle", message)


def _validate_timeout(value: float, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be finite and greater than zero")
    return float(value)


# --- OpenAI ⇄ Responses API tool translation --------------------------------
#
# The Responses API is the only dialect here with no implementation elsewhere in
# the gateway (the Anthropic one is shared with the adapters, and Chat
# Completions needs none), so these live in this module.


def _openai_tool_to_responses(tool: dict) -> dict:
    """Flatten one OpenAI tool spec into the Responses API's shape.

    Chat Completions nests the definition under ``function``; the Responses API
    puts ``name``/``parameters`` at the top level next to ``type``. Passing the
    nested form through is rejected — 400 ``Invalid 'tools': missing field
    `name``` — so the wrapper has to be unwrapped, not merely forwarded.

    Accepts an already-flat or Anthropic-shaped tool too, for the same reason
    the Anthropic converter does: not every caller of this gateway is
    OpenAI-native, and failing a tool that is already correct is a pure loss.
    """
    nested = tool.get("function")
    fn: dict = nested if isinstance(nested, dict) else tool
    spec: dict = {
        "type": "function",
        "name": fn.get("name", ""),
        # A tool with no parameters still needs the empty object; omitting the
        # key entirely is rejected.
        "parameters": fn.get("parameters") or fn.get("input_schema")
                      or {"type": "object", "properties": {}},
    }
    description = fn.get("description")
    if description:
        spec["description"] = description
    return spec


def _openai_tool_choice_to_responses(choice: str | dict | None) -> str | dict | None:
    """Map OpenAI's tool_choice onto the Responses API's.

    The string forms ("auto"/"none"/"required") are shared, so they pass
    through. The dict form is *flat* here — ``{"type":"function","name":…}`` —
    and the nested Chat Completions spelling is rejected with 400
    ``Invalid 'tool_choice': value did not match any expected variant``.
    """
    if choice is None:
        return None
    if isinstance(choice, str):
        return choice
    if isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name") or choice.get("name")
        if name:
            return {"type": "function", "name": name}
    return None


def _openai_msgs_to_responses_input(messages: list[dict]) -> list[dict]:
    """Convert OpenAI-shaped messages into Responses API ``input`` items.

    Tool traffic is not a message here: a tool call is a top-level
    ``function_call`` item and its result is a ``function_call_output`` item,
    both siblings of the role messages rather than nested inside them. Handing
    the API raw OpenAI history instead gets 400 ``Invalid 'input': value did not
    match any expected variant`` — including for an assistant turn with
    ``content: null``, which the schema rejects even with no tools involved.
    """
    items: list[dict] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")

        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id", ""),
                "output": content if isinstance(content, str) else json.dumps(content),
            })
            continue

        tool_calls = msg.get("tool_calls")
        if role == "assistant" and tool_calls:
            # Any text alongside the calls is still its own message item; the
            # calls become siblings, not fields.
            if isinstance(content, str) and content:
                items.append({"role": "assistant", "content": content})
            for tc in tool_calls:
                fn = tc.get("function") or {}
                raw_args = fn.get("arguments", tc.get("arguments", {}))
                # Unlike Anthropic, this API wants the JSON *string* — so a dict
                # from a non-OpenAI-native caller gets encoded, and a string is
                # forwarded verbatim rather than parsed and re-encoded (which
                # would turn malformed arguments into a request-level failure).
                args = raw_args if isinstance(raw_args, str) else json.dumps(raw_args or {})
                items.append({
                    "type": "function_call",
                    "call_id": tc.get("id", ""),
                    "name": fn.get("name", tc.get("name", "")),
                    "arguments": args,
                })
            continue

        # content=None is rejected by the schema, so normalize it away.
        items.append({"role": role, "content": content if content is not None else ""})
    return items


def _responses_output_to_tool_calls(output: list[dict]) -> list[dict]:
    """Extract OpenAI-shaped tool_calls from Responses API ``output`` items.

    Calls arrive as top-level ``function_call`` items carrying ``call_id`` and
    an ``arguments`` JSON string — already the encoding OpenAI callers expect,
    so it is forwarded as-is rather than re-serialized.
    """
    return [
        {
            "id": item.get("call_id") or item.get("id", f"call_{i}"),
            "type": "function",
            "function": {
                "name": item.get("name", ""),
                "arguments": item.get("arguments", "") or "{}",
            },
        }
        for i, item in enumerate(output)
        if item.get("type") == "function_call"
    ]


def create_mantle_provider_fn(
    region: str = "us-east-1",
    *,
    endpoint_url: str = "",
    credentials_config: dict[str, str] | None = None,
    connect_timeout: float = 30.0,
    read_timeout: float = 120.0,
) -> Callable[[ChatCompletionRequest], Callable[[ProviderModelMapping], Awaitable[ChatCompletionResponse]]]:
    """Return a factory that creates provider_fn callables for Bedrock Mantle."""
    connect_timeout = _validate_timeout(connect_timeout, "connect_timeout")
    read_timeout = _validate_timeout(read_timeout, "read_timeout")
    request_deadline = connect_timeout + read_timeout
    credentials_config = credentials_config or {}
    session_kwargs: dict = {}
    if credentials_config.get("access_key") and credentials_config.get("secret_key"):
        session_kwargs.update(
            {
                "aws_access_key_id": credentials_config["access_key"],
                "aws_secret_access_key": credentials_config["secret_key"],
                "aws_session_token": credentials_config.get("session_token") or None,
            }
        )
    session = boto3.Session(**session_kwargs) if session_kwargs else boto3.Session()
    credentials = session.get_credentials()
    endpoint = endpoint_url.rstrip("/") or f"https://bedrock-mantle.{region}.api.aws"

    def create(
        request: ChatCompletionRequest, prompt_caching_enabled: bool = False
    ) -> Callable[[ProviderModelMapping], Awaitable[ChatCompletionResponse]]:
        async def _invoke(
            mapping: ProviderModelMapping,
        ) -> ChatCompletionResponse:
            model_id = mapping.model_id
            # Anthropic models always use the Messages API.
            if _is_anthropic_model(model_id):
                return await _invoke_messages_api(
                    credentials,
                    endpoint,
                    region,
                    request,
                    mapping,
                    connect_timeout=connect_timeout,
                    read_timeout=read_timeout,
                )
            # Frontier GPT models use the Responses API; if Mantle reports
            # the model isn't supported there, fall back to Chat Completions.
            if _prefers_responses_api(model_id):
                try:
                    return await _invoke_responses_api(
                        credentials,
                        endpoint,
                        region,
                        request,
                        mapping,
                        connect_timeout=connect_timeout,
                        read_timeout=read_timeout,
                    )
                except ProviderError as exc:
                    if not _is_unsupported_route_error(exc):
                        raise
            # Everything else uses Chat Completions.
            return await _invoke_chat_completions_api(
                credentials,
                endpoint,
                region,
                request,
                mapping,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
            )

        async def provider_fn(mapping: ProviderModelMapping) -> ChatCompletionResponse:
            try:
                async with asyncio.timeout(request_deadline):
                    return await _invoke(mapping)
            except ProviderError:
                raise
            except TimeoutError as exc:
                raise ProviderError(
                    504,
                    mapping.provider,
                    "Bedrock Mantle request timed out",
                ) from exc
            except (
                urllib3.exceptions.TimeoutError,
                urllib3.exceptions.EmptyPoolError,
            ) as exc:
                raise ProviderError(
                    504,
                    mapping.provider,
                    "Bedrock Mantle request timed out",
                ) from exc
            except Exception as exc:
                raise ProviderError(
                    502,
                    mapping.provider,
                    "Bedrock Mantle request failed",
                ) from exc

        return provider_fn

    return create


def _read_mantle_body(response: object) -> bytes:
    body = bytearray()
    read = getattr(response, "read", None)
    if not callable(read):
        raise ProviderError(
            502,
            "bedrock-mantle",
            "Mantle returned an unreadable response",
        )
    while True:
        chunk = read(
            amt=_MANTLE_READ_CHUNK_BYTES,
            decode_content=True,
        )
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise ProviderError(
                502,
                "bedrock-mantle",
                "Mantle returned an unreadable response",
            )
        if len(body) + len(chunk) > _MAX_MANTLE_RESPONSE_BYTES:
            raise ProviderError(
                502,
                "bedrock-mantle",
                "Mantle response exceeded the maximum size",
            )
        body.extend(chunk)
    return bytes(body)


def _sigv4_request(
    credentials,
    region: str,
    url: str,
    body: str,
    *,
    connect_timeout: float = 30.0,
    read_timeout: float = 120.0,
) -> dict:
    """Make a SigV4-signed POST request and return parsed JSON."""
    aws_request = AWSRequest(method="POST", url=url, data=body, headers={
        "Content-Type": "application/json",
    })
    resolved_creds = credentials.get_frozen_credentials()
    SigV4Auth(resolved_creds, _MANTLE_SERVICE, region).add_auth(aws_request)

    response = _MANTLE_HTTP.request(
        "POST",
        url,
        body=body.encode(),
        headers=dict(aws_request.headers),
        timeout=urllib3.Timeout(
            connect=connect_timeout,
            read=read_timeout,
        ),
        pool_timeout=connect_timeout,
        retries=False,
        preload_content=False,
    )
    try:
        response_data = _read_mantle_body(response)
        if not 200 <= response.status < 300:
            raise _mantle_http_error(response.status, response_data)
        try:
            value = json.loads(response_data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError(
                502,
                "bedrock-mantle",
                "Mantle returned malformed JSON",
            ) from exc
    finally:
        release = getattr(response, "release_conn", None)
        if callable(release):
            with suppress(Exception):
                release()
        close = getattr(response, "close", None)
        if callable(close):
            with suppress(Exception):
                close()
    if not isinstance(value, dict):
        raise ProviderError(
            502,
            "bedrock-mantle",
            "Mantle returned a non-object response",
        )
    return value


async def _invoke_responses_api(
    credentials,
    endpoint: str,
    region: str,
    request: ChatCompletionRequest,
    mapping: ProviderModelMapping,
    *,
    connect_timeout: float = 30.0,
    read_timeout: float = 120.0,
) -> ChatCompletionResponse:
    """Call the OpenAI Responses API on Mantle for GPT models."""
    messages = list(request.messages)
    if request.system:
        instructions = request.system
    else:
        instructions = None

    non_system = []
    for msg in messages:
        if msg.get("role") == "system":
            instructions = msg.get("content", "")
        else:
            non_system.append(msg)

    input_items = _openai_msgs_to_responses_input(non_system)

    # A lone user turn can be sent as a bare string. Only when it really is one
    # plain message — a single function_call_output has no such shorthand.
    if (len(input_items) == 1 and input_items[0].get("role") == "user"
            and "type" not in input_items[0]):
        input_val: str | list[dict] = input_items[0]["content"]
    else:
        input_val = input_items

    payload: dict = {
        "model": mapping.model_id,
        "input": input_val,
    }
    if instructions:
        payload["instructions"] = instructions
    if request.max_tokens is not None:
        payload["max_output_tokens"] = request.max_tokens
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.top_p is not None:
        payload["top_p"] = request.top_p
    if request.tools:
        payload["tools"] = [_openai_tool_to_responses(t) for t in request.tools]
        tc = _openai_tool_choice_to_responses(request.tool_choice)
        if tc is not None:
            payload["tool_choice"] = tc

    url = f"{endpoint}/openai/v1/responses"
    body = json.dumps(payload)

    response_data = await asyncio.to_thread(
        _sigv4_request,
        credentials,
        region,
        url,
        body,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
    )

    output = response_data.get("output", [])
    text = ""
    for item in output:
        if item.get("type") == "message":
            for content_block in item.get("content", []):
                if content_block.get("type") == "output_text":
                    text += content_block.get("text", "")

    tool_calls = _responses_output_to_tool_calls(output)
    message: dict = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
        # OpenAI sends content=null alongside tool_calls; only when there is no
        # text, so a plain response still gets "" rather than None.
        if not text:
            message["content"] = None

    # This API reports lifecycle status ("completed"), not why generation
    # stopped, so a tool call is invisible in it. A caller driving a tool loop
    # branches on finish_reason, and "completed" reads as "nothing left to do" —
    # it would return the model's tool call to the client and never run it.
    finish_reason = response_data.get("status", "completed")
    if tool_calls:
        finish_reason = "tool_calls"

    usage_data = response_data.get("usage", {})
    prompt_tokens = usage_data.get("input_tokens", 0)
    completion_tokens = usage_data.get("output_tokens", 0)

    return ChatCompletionResponse(
        id=response_data.get("id", "mantle-response"),
        choices=[{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        model=mapping.model_id,
        provider=mapping.provider,
    )


async def _invoke_messages_api(
    credentials,
    endpoint: str,
    region: str,
    request: ChatCompletionRequest,
    mapping: ProviderModelMapping,
    *,
    connect_timeout: float = 30.0,
    read_timeout: float = 120.0,
) -> ChatCompletionResponse:
    """Call the Anthropic Messages API on Mantle for Claude models."""
    messages = []
    system_text = request.system or ""

    for msg in request.messages:
        if msg.get("role") == "system":
            system_text = msg.get("content", "")
        else:
            # Shared with the Anthropic adapters: this is the same wire format,
            # and tool traffic has to be reshaped into content blocks. Forwarding
            # OpenAI's shape gets 400 Unexpected role "tool".
            messages.append(openai_msg_to_anthropic(msg))

    payload: dict = {
        "model": mapping.model_id,
        "messages": messages,
        "max_tokens": request.max_tokens or 4096,
    }
    if system_text:
        payload["system"] = system_text
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.top_p is not None:
        payload["top_p"] = request.top_p
    if request.tools and request.tool_choice != "none":
        payload["tools"] = [openai_tool_to_anthropic(t) for t in request.tools]
        tc = openai_tool_choice_to_anthropic(request.tool_choice)
        if tc is not None:
            payload["tool_choice"] = tc

    payload["anthropic_version"] = "2023-06-01"

    url = f"{endpoint}/anthropic/v1/messages"
    body = json.dumps(payload)

    response_data = await asyncio.to_thread(
        _sigv4_request,
        credentials,
        region,
        url,
        body,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
    )

    content_blocks = response_data.get("content", [])
    text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")

    tool_calls = [
        {
            "id": b.get("id", f"call_{i}"),
            "type": "function",
            # Anthropic sends parsed input; OpenAI callers json.loads() this
            # field, so it has to be re-encoded as a string.
            "function": {"name": b.get("name", ""),
                         "arguments": json.dumps(b.get("input", {}))},
        }
        for i, b in enumerate(content_blocks)
        if b.get("type") == "tool_use"
    ]

    message: dict = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
        if not text:
            message["content"] = None

    finish_reason = response_data.get("stop_reason", "end_turn")
    # "tool_use" means nothing to an OpenAI-shaped caller driving a tool loop.
    if finish_reason == "tool_use" or (tool_calls and finish_reason in (None, "stop", "end_turn")):
        finish_reason = "tool_calls"

    usage_data = response_data.get("usage", {})
    prompt_tokens = usage_data.get("input_tokens", 0)
    completion_tokens = usage_data.get("output_tokens", 0)

    return ChatCompletionResponse(
        id=response_data.get("id", "mantle-response"),
        choices=[{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        model=mapping.model_id,
        provider=mapping.provider,
    )


async def _invoke_chat_completions_api(
    credentials,
    endpoint: str,
    region: str,
    request: ChatCompletionRequest,
    mapping: ProviderModelMapping,
    *,
    connect_timeout: float = 30.0,
    read_timeout: float = 120.0,
) -> ChatCompletionResponse:
    """Call the OpenAI-compatible Chat Completions API on Mantle.

    Serves open-weight families (gpt-oss, DeepSeek, Qwen, etc.) at the
    top-level /v1/chat/completions path and returns standard OpenAI
    chat.completion JSON.
    """
    messages = list(request.messages)
    if request.system and not any(m.get("role") == "system" for m in messages):
        messages = [{"role": "system", "content": request.system}, *messages]

    payload: dict = {
        "model": mapping.model_id,
        "messages": messages,
    }
    if request.max_tokens is not None:
        payload["max_tokens"] = request.max_tokens
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.top_p is not None:
        payload["top_p"] = request.top_p
    # This route speaks the gateway's own dialect, so tools (and the tool
    # traffic already in `messages`) pass through untouched.
    if request.tools:
        payload["tools"] = request.tools
        if request.tool_choice is not None:
            payload["tool_choice"] = request.tool_choice

    url = f"{endpoint}/v1/chat/completions"
    body = json.dumps(payload)

    response_data = await asyncio.to_thread(
        _sigv4_request,
        credentials,
        region,
        url,
        body,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
    )

    choices = response_data.get("choices", [])
    first = choices[0] if choices else {}
    provider_message = first.get("message", {})
    text = provider_message.get("content") or ""

    # Already OpenAI-shaped, but the response is rebuilt rather than forwarded,
    # so tool_calls have to be carried across explicitly or they are dropped on
    # a route that needed no translation at all.
    tool_calls = provider_message.get("tool_calls") or []
    message: dict = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
        if not text:
            message["content"] = None

    finish_reason = first.get("finish_reason", "stop")
    if tool_calls and finish_reason in (None, "stop"):
        finish_reason = "tool_calls"

    usage_data = response_data.get("usage", {})
    prompt_tokens = usage_data.get("prompt_tokens", 0)
    completion_tokens = usage_data.get("completion_tokens", 0)

    return ChatCompletionResponse(
        id=response_data.get("id", "mantle-response"),
        choices=[{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=usage_data.get("total_tokens", prompt_tokens + completion_tokens),
        ),
        model=mapping.model_id,
        provider=mapping.provider,
    )
