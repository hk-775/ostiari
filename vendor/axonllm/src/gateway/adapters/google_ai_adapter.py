"""Google AI Studio (Generative Language API) provider adapter."""

import logging

from src.gateway.adapters.base import ProviderAdapter
from src.gateway.adapters.gemini_tools import (
    gemini_token_usage,
    gemini_parts_to_tool_calls,
    openai_msg_to_gemini,
    openai_tool_call_names,
    openai_tool_choice_to_gemini,
    openai_tools_to_gemini,
)
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ModelInfo,
    StreamChunk,
    TokenUsage,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "google_ai"
_FINISH_REASONS = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
}

_GOOGLE_AI_MODELS = [
    ModelInfo(model_id="gemini-3.5-flash", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="gemini-3.1-pro-preview", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
]


class GoogleAIAdapter(ProviderAdapter):
    """Translates between the unified Gateway format and Google AI Studio's Generative Language API.

    Uses the same request/response format as Vertex AI (contents + generationConfig)
    but authenticates with a simple API key passed in the x-goog-api-key header.
    """

    PROVIDER_NAME = PROVIDER_NAME
    _MODELS = _GOOGLE_AI_MODELS

    async def translate_request(
        self, request: ChatCompletionRequest, *, prompt_caching_enabled: bool = False
    ) -> dict:
        contents = []
        system_text = request.system
        tool_call_names = openai_tool_call_names(request.messages)
        for msg in request.messages:
            role = msg.get("role", "user")
            if role == "system":
                if system_text is None:
                    system_text = msg.get("content", "")
                continue
            entry = openai_msg_to_gemini(msg, tool_call_names)
            if entry is not None:
                contents.append(entry)

        payload: dict = {
            "contents": contents,
        }

        if system_text is not None:
            payload["systemInstruction"] = {
                "parts": [{"text": system_text}],
            }

        if request.tools:
            payload["tools"] = openai_tools_to_gemini(request.tools)
            tool_config = openai_tool_choice_to_gemini(request.tool_choice)
            if tool_config is not None:
                payload["toolConfig"] = tool_config

        gen_config: dict = {}
        if request.temperature is not None:
            gen_config["temperature"] = request.temperature
        if request.max_tokens is not None:
            gen_config["maxOutputTokens"] = request.max_tokens
        if request.top_p is not None:
            gen_config["topP"] = request.top_p
        if request.stop is not None:
            gen_config["stopSequences"] = request.stop

        if gen_config:
            payload["generationConfig"] = gen_config

        return payload

    def translate_response(self, provider_response: dict) -> ChatCompletionResponse:
        candidates = provider_response.get("candidates", [])
        combined_text = ""
        finish_reason = "stop"
        tool_calls: list[dict] = []
        if candidates:
            first = candidates[0]
            parts = first.get("content", {}).get("parts", [])
            text_parts = [p.get("text", "") for p in parts if "text" in p]
            combined_text = "".join(text_parts)
            raw_finish_reason = first.get("finishReason", "STOP")
            finish_reason = _FINISH_REASONS.get(
                raw_finish_reason,
                raw_finish_reason,
            )
            tool_calls = gemini_parts_to_tool_calls(parts)

        message: dict = {"role": "assistant", "content": combined_text}
        if tool_calls:
            message["tool_calls"] = tool_calls
            if not combined_text:
                message["content"] = None
            # Gemini reports finishReason STOP even when it calls a function —
            # the functionCall part is the only signal. Callers driving a tool
            # loop branch on finish_reason, so synthesize the value they expect.
            finish_reason = "tool_calls"

        choices = [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ]

        return ChatCompletionResponse(
            id=(
                provider_response.get("responseId")
                or provider_response.get("id", "")
            ),
            choices=choices,
            usage=gemini_token_usage(
                provider_response.get("usageMetadata")
            ),
            model=(
                provider_response.get("modelVersion")
                or provider_response.get("model", "")
            ),
            provider=PROVIDER_NAME,
        )

    def translate_stream_chunk(self, chunk: dict) -> StreamChunk:
        candidates = chunk.get("candidates", [])
        choices: list[dict] = []
        is_final = False

        if candidates:
            first = candidates[0]
            parts = first.get("content", {}).get("parts", [])
            text = "".join(
                part.get("text", "")
                for part in parts
                if "text" in part
            )
            tool_calls = gemini_parts_to_tool_calls(parts)
            delta: dict = {}
            if text:
                delta["content"] = text
            if tool_calls:
                delta["tool_calls"] = [
                    {"index": index, **tool_call}
                    for index, tool_call in enumerate(tool_calls)
                ]
            raw_finish_reason = first.get("finishReason")
            is_final = raw_finish_reason is not None
            finish_reason = (
                "tool_calls"
                if tool_calls and is_final
                else _FINISH_REASONS.get(
                    raw_finish_reason,
                    raw_finish_reason,
                )
            )
            choices = [{
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }]

        return StreamChunk(
            id=chunk.get("responseId") or chunk.get("id", ""),
            choices=choices,
            model=chunk.get("modelVersion") or chunk.get("model", ""),
            is_final=is_final,
            usage=_google_stream_usage(chunk),
        )


def _google_stream_usage(chunk: dict) -> TokenUsage | None:
    usage = chunk.get("usageMetadata")
    if not isinstance(usage, dict) or not usage:
        return None
    return gemini_token_usage(usage)
