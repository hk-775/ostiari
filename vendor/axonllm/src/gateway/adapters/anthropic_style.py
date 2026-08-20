"""Shared base for Anthropic-compatible adapters (Anthropic, Bedrock).

Both providers use system as a separate field, content blocks for responses,
and stop_sequences instead of stop. Subclasses override only provider-specific
response field names and streaming nuances.

Tool calling is translated in both directions here, because the two dialects
disagree on shape rather than on capability:

    OpenAI                                  Anthropic
    tools[].function.{name,parameters}      tools[].{name,input_schema}
    assistant.tool_calls[]                  assistant content [{type:tool_use}]
    role:"tool" message                     user content [{type:tool_result}]
    finish_reason:"tool_calls"              stop_reason:"tool_use"

The gateway's unified format is OpenAI's, so every one of those has to be
converted on the way out and converted back on the way in. Handling only the
request half would leave the model's tool call unreadable to the caller.
"""

import json
from datetime import datetime, timezone

from src.gateway.adapters.base import ProviderAdapter
from src.gateway.config import DEFAULT_CONFIG
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    HealthStatus,
    ModelInfo,
    ProviderHealth,
    StreamChunk,
    TokenUsage,
)


class AnthropicStyleAdapter(ProviderAdapter):
    """Base adapter for providers that use the Anthropic request/response format.

    Subclasses must define:
        PROVIDER_NAME: str
        _MODELS: list[ModelInfo]

    Subclasses may override:
        _prompt_tokens_key / _completion_tokens_key for response parsing.
        translate_stream_chunk for provider-specific streaming differences.
    """

    PROVIDER_NAME: str = ""
    _MODELS: list[ModelInfo] = []

    # Response usage field names — Anthropic uses input_tokens/output_tokens,
    # Bedrock may use inputTokens/outputTokens as well.
    _prompt_tokens_key: str = "input_tokens"
    _completion_tokens_key: str = "output_tokens"
    # Bedrock alternate keys (checked as fallback)
    _prompt_tokens_alt: str | None = None
    _completion_tokens_alt: str | None = None

    async def translate_request(
        self, request: ChatCompletionRequest, *, prompt_caching_enabled: bool = False
    ) -> dict:
        warnings: list[str] = []

        system_text = request.system
        messages = []
        for msg in request.messages:
            if msg.get("role") == "system":
                if system_text is None:
                    system_text = msg.get("content", "")
            else:
                messages.append(openai_msg_to_anthropic(msg))

        max_tokens = (
            request.max_tokens
            if request.max_tokens is not None
            else DEFAULT_CONFIG.adapter.default_max_tokens
        )

        payload: dict = {
            "model": request.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }

        if system_text is not None:
            if prompt_caching_enabled:
                # Convert system to content-block array with cache_control on last block
                if isinstance(system_text, str):
                    payload["system"] = [
                        {
                            "type": "text",
                            "text": system_text,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                elif isinstance(system_text, list):
                    # Already a list of content blocks — add cache_control to last block only
                    blocks = []
                    for i, block in enumerate(system_text):
                        new_block = {k: v for k, v in block.items() if k != "cache_control"}
                        if i == len(system_text) - 1:
                            new_block["cache_control"] = {"type": "ephemeral"}
                        blocks.append(new_block)
                    payload["system"] = blocks
            else:
                payload["system"] = system_text
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.stop is not None:
            payload["stop_sequences"] = request.stop
        if request.tools and request.tool_choice != "none":
            payload["tools"] = [openai_tool_to_anthropic(t) for t in request.tools]
            tc = openai_tool_choice_to_anthropic(request.tool_choice)
            if tc is not None:
                payload["tool_choice"] = tc
        if request.stream:
            payload["stream"] = True

        if warnings:
            payload["_warnings"] = warnings

        return payload

    def translate_response(self, provider_response: dict) -> ChatCompletionResponse:
        content_blocks = provider_response.get("content", [])
        text_parts = [
            block.get("text", "")
            for block in content_blocks
            if block.get("type") == "text"
        ]
        combined_text = "".join(text_parts)

        tool_calls = [
            {
                "id": block.get("id", f"call_{i}"),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    # OpenAI carries arguments as a JSON *string*; Anthropic sends
                    # a parsed object. Callers json.loads() this field, so it has
                    # to be re-encoded or they choke on a dict.
                    "arguments": json.dumps(block.get("input", {})),
                },
            }
            for i, block in enumerate(content_blocks)
            if block.get("type") == "tool_use"
        ]

        message: dict = {"role": "assistant", "content": combined_text}
        if tool_calls:
            message["tool_calls"] = tool_calls
            # OpenAI sends content=null (not "") alongside tool_calls. Only in
            # that case — a plain response with no text keeps "" so nothing
            # downstream that assumes a str changes behavior.
            if not combined_text:
                message["content"] = None

        finish_reason = provider_response.get("stop_reason", "stop")
        # A caller driving a tool loop branches on finish_reason; "tool_use"
        # means nothing to an OpenAI-shaped client, so map it to the name it
        # does know. Without this the loop ends and the tool is never run.
        if finish_reason == "tool_use" or (tool_calls and finish_reason in (None, "stop")):
            finish_reason = "tool_calls"

        choices = [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ]

        usage_data = provider_response.get("usage", {})
        prompt_tokens = usage_data.get(self._prompt_tokens_key, 0)
        if not prompt_tokens and self._prompt_tokens_alt:
            prompt_tokens = usage_data.get(self._prompt_tokens_alt, 0)
        completion_tokens = usage_data.get(self._completion_tokens_key, 0)
        if not completion_tokens and self._completion_tokens_alt:
            completion_tokens = usage_data.get(self._completion_tokens_alt, 0)

        cached_tokens = usage_data.get("cache_read_input_tokens", 0)
        cache_creation_tokens = usage_data.get("cache_creation_input_tokens", 0)

        return ChatCompletionResponse(
            id=provider_response.get("id", ""),
            choices=choices,
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                cached_tokens=cached_tokens,
                cache_creation_tokens=cache_creation_tokens,
            ),
            model=provider_response.get("model", ""),
            provider=self.PROVIDER_NAME,
        )

    def translate_stream_chunk(self, chunk: dict) -> StreamChunk:
        chunk_type = chunk.get("type", "")
        delta_content = ""
        is_final = False

        if chunk_type == "content_block_delta":
            delta = chunk.get("delta", {})
            delta_content = delta.get("text", "")
        elif chunk_type == "message_stop":
            is_final = True

        choices = [
            {
                "index": 0,
                "delta": {"content": delta_content} if delta_content else {},
                "finish_reason": "stop" if is_final else None,
            }
        ]

        return StreamChunk(
            id=chunk.get("id", ""),
            choices=choices,
            model=chunk.get("model", ""),
            is_final=is_final,
        )

    async def list_models(self) -> list[ModelInfo]:
        return list(self._MODELS)

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.PROVIDER_NAME,
            status=HealthStatus.HEALTHY,
            last_check=datetime.now(timezone.utc),
        )


# --- OpenAI ⇄ Anthropic tool translation ------------------------------------
#
# Public rather than module-private because ``mantle_provider`` needs the same
# three conversions: Bedrock Mantle exposes a real ``/anthropic/v1/messages``
# route but hand-builds its payload instead of going through this adapter, so it
# speaks the identical dialect. Re-implementing it there would mean two copies of
# one wire format free to drift apart — and the details that are easy to get
# wrong (arguments arriving as a JSON string, malformed arguments that must not
# fail the request, tools declared with no parameters) are exactly the ones a
# second copy would miss.


def openai_tool_to_anthropic(tool: dict) -> dict:
    """Convert one OpenAI tool spec to Anthropic's shape.

    Accepts the nested OpenAI form and a bare Anthropic-shaped dict alike: the
    gateway's own callers are not all OpenAI-native, and rejecting a tool that
    is already correct would be a needless failure.
    """
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
    if fn is None:
        # Already Anthropic-shaped (or close enough) — normalize the schema key.
        return {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "input_schema": tool.get("input_schema") or tool.get("parameters")
                            or {"type": "object", "properties": {}},
        }
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        # Anthropic requires input_schema; a tool declared with no parameters
        # still needs the empty object or the API rejects the whole request.
        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
    }


def openai_tool_choice_to_anthropic(choice: str | dict | None) -> dict | None:
    """Map OpenAI's tool_choice onto Anthropic's.

    OpenAI: "auto" | "none" | "required" | {"type":"function","function":{"name":…}}
    Anthropic: {"type":"auto"} | {"type":"any"} | {"type":"tool","name":…}

    "none" has no Anthropic equivalent. Callers omit the tools collection
    entirely for that mode; returning None also prevents an invalid
    tool_choice value from being sent.
    """
    if choice is None or choice == "none":
        return None
    if choice == "auto":
        return {"type": "auto"}
    if choice in ("required", "any"):
        return {"type": "any"}
    if isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name") or choice.get("name")
        if name:
            return {"type": "tool", "name": name}
    return None


def openai_msg_to_anthropic(msg: dict) -> dict:
    """Convert one OpenAI-shaped message to Anthropic's content-block form.

    Only tool-carrying messages need rewriting; everything else is returned
    unchanged so ordinary traffic takes no new code path.
    """
    role = msg.get("role")

    # role:"tool" → a user message holding a tool_result block.
    if role == "tool":
        content = msg.get("content")
        return {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id", ""),
                "content": content if isinstance(content, str) else json.dumps(content),
            }],
        }

    # An assistant turn that called tools → text blocks + tool_use blocks.
    tool_calls = msg.get("tool_calls")
    if role == "assistant" and tool_calls:
        blocks: list[dict] = []
        text = msg.get("content")
        if isinstance(text, str) and text:
            blocks.append({"type": "text", "text": text})
        elif isinstance(text, list):
            blocks.extend(text)
        for tc in tool_calls:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments", tc.get("arguments", {}))
            # OpenAI sends arguments as a JSON string; Anthropic wants an object.
            # A model can emit malformed JSON here, and that must not take down
            # the request — send {} and let the tool report the bad call.
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except (ValueError, TypeError):
                    args = {}
            else:
                args = raw_args or {}
            blocks.append({
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": fn.get("name", tc.get("name", "")),
                "input": args,
            })
        return {"role": "assistant", "content": blocks}

    return msg
