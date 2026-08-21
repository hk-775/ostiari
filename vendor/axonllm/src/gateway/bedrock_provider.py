"""Bedrock provider function using boto3 — bypasses the generic HTTP client.

Uses the bedrock-runtime invoke_model / converse API with proper SigV4 signing
handled automatically by boto3. Supports both Anthropic-style (Claude) and
OpenAI-style (Nova, DeepSeek) models.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import math
from typing import Awaitable, Callable

import boto3
from botocore.config import Config
from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError

from src.gateway.adapters.bedrock_adapter import BedrockAdapter
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ProviderModelMapping,
    TokenUsage,
)
from src.gateway.router import ProviderError

# Models that use Anthropic Messages API format
_ANTHROPIC_PREFIXES = ("anthropic.",)

# Models that use the Bedrock Converse API (Nova, DeepSeek, etc.)
_CONVERSE_MODELS = True  # default for non-Anthropic models
_MAX_BEDROCK_RESPONSE_BYTES = 10 * 1024 * 1024
_BEDROCK_READ_CHUNK_BYTES = 64 * 1024


def _validate_timeout(value: float, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be finite and greater than zero")
    return float(value)


def _read_bounded_body(body: object, provider: str) -> dict:
    read = getattr(body, "read", None)
    if not callable(read):
        raise ProviderError(
            502,
            provider,
            "Bedrock returned an unreadable response",
        )
    payload = bytearray()
    try:
        while True:
            chunk = read(_BEDROCK_READ_CHUNK_BYTES)
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise ProviderError(
                    502,
                    provider,
                    "Bedrock returned an unreadable response",
                )
            if len(payload) + len(chunk) > _MAX_BEDROCK_RESPONSE_BYTES:
                raise ProviderError(
                    502,
                    provider,
                    "Bedrock response exceeded the maximum size",
                )
            payload.extend(chunk)
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            with suppress(Exception):
                close()
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError(
            502,
            provider,
            "Bedrock returned malformed JSON",
        ) from exc
    return _bounded_response(value, provider)


def _bounded_response(value: object, provider: str) -> dict:
    if not isinstance(value, dict):
        raise ProviderError(
            502,
            provider,
            "Bedrock returned a non-object response",
        )
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProviderError(
            502,
            provider,
            "Bedrock returned an invalid response",
        ) from exc
    if len(encoded) > _MAX_BEDROCK_RESPONSE_BYTES:
        raise ProviderError(
            502,
            provider,
            "Bedrock response exceeded the maximum size",
        )
    return value


def _is_anthropic_model(model_id: str) -> bool:
    return any(model_id.startswith(p) or f".{p}" in model_id for p in _ANTHROPIC_PREFIXES)


def _converse_tool_choice(choice) -> dict | None:
    """Map OpenAI's tool_choice onto the Converse API's toolChoice.

    Converse: {"auto":{}} | {"any":{}} | {"tool":{"name":…}}. "none" has no
    equivalent, so the caller omits toolConfig entirely.
    """
    if choice is None or choice == "none":
        return None
    if choice == "auto":
        return {"auto": {}}
    if choice in ("required", "any"):
        return {"any": {}}
    if isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name") or choice.get("name")
        if name:
            return {"tool": {"name": name}}
    return None


def _converse_message(role: str, msg: dict, content) -> dict:
    """Build one Converse message, translating tool traffic into its block shape.

    Converse uses toolUse/toolResult content blocks rather than OpenAI's
    tool_calls field and role:"tool" message, so both have to be reshaped —
    otherwise the model never sees that it called a tool, or what came back.
    """
    if role == "tool":
        raw = content if isinstance(content, str) else json.dumps(content)
        return {"role": "user", "content": [{"toolResult": {
            "toolUseId": msg.get("tool_call_id", ""),
            "content": [{"text": raw}],
        }}]}

    tool_calls = msg.get("tool_calls")
    if role == "assistant" and tool_calls:
        blocks: list[dict] = []
        if isinstance(content, str) and content:
            blocks.append({"text": content})
        for tc in tool_calls:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments", tc.get("arguments", {}))
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except (ValueError, TypeError):
                    args = {}
            else:
                args = raw_args or {}
            blocks.append({"toolUse": {
                "toolUseId": tc.get("id", ""),
                "name": fn.get("name", tc.get("name", "")),
                "input": args,
            }})
        return {"role": "assistant", "content": blocks}

    return {"role": role, "content": [{"text": content if isinstance(content, str) else str(content)}]}


def create_bedrock_provider_fn(
    region: str = "us-east-1",
    *,
    endpoint_url: str = "",
    credentials: dict[str, str] | None = None,
    connect_timeout: float = 30.0,
    read_timeout: float = 120.0,
) -> Callable[[ChatCompletionRequest], Callable[[ProviderModelMapping], Awaitable[ChatCompletionResponse]]]:
    """Return a factory that creates provider_fn callables for Bedrock."""
    connect_timeout = _validate_timeout(connect_timeout, "connect_timeout")
    read_timeout = _validate_timeout(read_timeout, "read_timeout")
    request_deadline = connect_timeout + read_timeout
    credentials = credentials or {}
    client_kwargs: dict = {
        "region_name": region,
        "config": Config(
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        ),
    }
    if endpoint_url:
        client_kwargs["endpoint_url"] = endpoint_url
    if credentials.get("access_key") and credentials.get("secret_key"):
        client_kwargs.update(
            {
                "aws_access_key_id": credentials["access_key"],
                "aws_secret_access_key": credentials["secret_key"],
            }
        )
        if credentials.get("session_token"):
            client_kwargs["aws_session_token"] = credentials["session_token"]
    client = boto3.client("bedrock-runtime", **client_kwargs)
    anthropic_adapter = BedrockAdapter()

    def create(request: ChatCompletionRequest, prompt_caching_enabled: bool = False) -> Callable[[ProviderModelMapping], Awaitable[ChatCompletionResponse]]:
        async def provider_fn(mapping: ProviderModelMapping) -> ChatCompletionResponse:
            try:
                async with asyncio.timeout(request_deadline):
                    if _is_anthropic_model(mapping.model_id):
                        return await _invoke_anthropic(client, anthropic_adapter, request, mapping, prompt_caching_enabled=prompt_caching_enabled)
                    return await _invoke_converse(client, request, mapping, prompt_caching_enabled=prompt_caching_enabled)
            except ProviderError:
                raise
            except (TimeoutError, ConnectTimeoutError, ReadTimeoutError) as exc:
                raise ProviderError(
                    504,
                    mapping.provider,
                    "Bedrock request timed out",
                ) from exc
            except client.exceptions.AccessDeniedException as exc:
                raise ProviderError(
                    403,
                    mapping.provider,
                    "Bedrock access denied",
                ) from exc
            except client.exceptions.ResourceNotFoundException as exc:
                raise ProviderError(
                    404,
                    mapping.provider,
                    "Bedrock model was not found",
                ) from exc
            except client.exceptions.ThrottlingException as exc:
                raise ProviderError(
                    429,
                    mapping.provider,
                    "Bedrock request was throttled",
                ) from exc
            except Exception as exc:
                raise ProviderError(
                    502,
                    mapping.provider,
                    "Bedrock request failed",
                ) from exc

        return provider_fn

    return create


async def _invoke_anthropic(
    client, adapter: BedrockAdapter, request: ChatCompletionRequest, mapping: ProviderModelMapping,
    prompt_caching_enabled: bool = False,
) -> ChatCompletionResponse:
    """Invoke Anthropic-style models (Claude) via invoke_model."""
    payload = await adapter.translate_request(request, prompt_caching_enabled=prompt_caching_enabled)
    payload.pop("stream", None)
    payload.pop("model", None)
    payload.pop("_warnings", None)
    payload["anthropic_version"] = "bedrock-2023-05-31"

    body = json.dumps(payload)

    def _call():
        response = client.invoke_model(
            modelId=mapping.model_id,
            contentType="application/json",
            accept="application/json",
            body=body,
        )
        response_body = (
            response.get("body")
            if isinstance(response, dict)
            else None
        )
        return _read_bounded_body(response_body, mapping.provider)

    response_body = await asyncio.to_thread(_call)
    result = adapter.translate_response(response_body)
    return ChatCompletionResponse(
        id=result.id or "bedrock-response",
        choices=result.choices,
        usage=result.usage,
        model=mapping.model_id,
        provider=mapping.provider,
    )


async def _invoke_converse(
    client, request: ChatCompletionRequest, mapping: ProviderModelMapping,
    prompt_caching_enabled: bool = False,
) -> ChatCompletionResponse:
    """Invoke non-Anthropic models (Nova, DeepSeek) via the Converse API."""
    messages = []
    system_parts = []

    for msg in request.messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            system_parts.append({"text": content})
        else:
            messages.append(_converse_message(role, msg, content))

    if request.system:
        system_parts.insert(0, {"text": request.system})

    kwargs: dict = {
        "modelId": mapping.model_id,
        "messages": messages,
    }
    if system_parts:
        # Add cache_control to last system block for Anthropic models when caching is enabled
        if prompt_caching_enabled and _is_anthropic_model(mapping.model_id) and system_parts:
            system_parts[-1]["cache_control"] = {"type": "ephemeral"}
        kwargs["system"] = system_parts

    if request.tools and request.tool_choice != "none":
        kwargs["toolConfig"] = {
            "tools": [
                {"toolSpec": {
                    "name": (t.get("function") or t).get("name", ""),
                    "description": (t.get("function") or t).get("description", ""),
                    "inputSchema": {"json": (t.get("function") or {}).get("parameters")
                                            or t.get("input_schema")
                                            or {"type": "object", "properties": {}}},
                }}
                for t in request.tools
            ]
        }
        tc = _converse_tool_choice(request.tool_choice)
        if tc is not None:
            kwargs["toolConfig"]["toolChoice"] = tc

    inference_config: dict = {}
    if request.max_tokens is not None:
        inference_config["maxTokens"] = request.max_tokens
    else:
        inference_config["maxTokens"] = 4096
    if request.temperature is not None:
        inference_config["temperature"] = request.temperature
    if request.top_p is not None:
        inference_config["topP"] = request.top_p
    if inference_config:
        kwargs["inferenceConfig"] = inference_config

    def _call():
        return _bounded_response(
            client.converse(**kwargs),
            mapping.provider,
        )

    response = await asyncio.to_thread(_call)

    # Parse Converse API response
    output = response.get("output", {})
    message = output.get("message", {})
    content_blocks = message.get("content", [])
    text = "".join(b.get("text", "") for b in content_blocks if "text" in b)

    tool_calls = [
        {
            "id": tu.get("toolUseId", f"call_{i}"),
            "type": "function",
            "function": {"name": tu.get("name", ""),
                         "arguments": json.dumps(tu.get("input", {}))},
        }
        for i, b in enumerate(content_blocks)
        if (tu := b.get("toolUse"))
    ]

    out_message: dict = {"role": "assistant", "content": text}
    if tool_calls:
        out_message["tool_calls"] = tool_calls
        if not text:
            out_message["content"] = None

    finish_reason = response.get("stopReason", "stop")
    # Converse says "tool_use"; OpenAI-shaped callers branch on "tool_calls".
    if finish_reason == "tool_use" or (tool_calls and finish_reason in (None, "stop", "end_turn")):
        finish_reason = "tool_calls"

    usage_data = response.get("usage", {})
    prompt_tokens = usage_data.get("inputTokens", 0)
    completion_tokens = usage_data.get("outputTokens", 0)

    return ChatCompletionResponse(
        id=response.get("ResponseMetadata", {}).get("RequestId", "bedrock-response"),
        choices=[{
            "index": 0,
            "message": out_message,
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
