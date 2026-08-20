"""Shared base for OpenAI-compatible adapters (OpenAI, Azure OpenAI).

Both providers use the same request/response/streaming format.
Subclasses only need to set PROVIDER_NAME and _MODELS.
"""

import re
from datetime import datetime, timezone

from src.gateway.adapters.base import ProviderAdapter
from src.gateway.adapters.openai_responses import (
    build_responses_payload,
    is_responses_only_model,
    translate_responses_reply,
    translate_responses_stream_event,
)
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    EmbeddingData,
    EmbeddingRequest,
    EmbeddingResponse,
    HealthStatus,
    ModelInfo,
    ProviderHealth,
    StreamChunk,
    TokenUsage,
)

# OpenAI reasoning models: o1, o3, o4 families (incl. -mini/-preview and dated
# variants like o3-2025-...). They require max_completion_tokens (not
# max_tokens) and only accept the default temperature.
_OPENAI_REASONING_RE = re.compile(r"^o[134]([.-]|$)")


def _is_openai_reasoning_model(model_id: str) -> bool:
    return bool(_OPENAI_REASONING_RE.match((model_id or "").strip().lower()))


class OpenAIStyleAdapter(ProviderAdapter):
    """Base adapter for providers that use the OpenAI request/response format.

    Subclasses must define:
        PROVIDER_NAME: str
        _MODELS: list[ModelInfo]
    """

    PROVIDER_NAME: str = ""
    _MODELS: list[ModelInfo] = []

    # Only OpenAI itself serves /v1/responses. The other subclasses of this base
    # (Azure, xAI, Groq, Together, Fireworks, AI21) are OpenAI-*compatible* and
    # expose Chat Completions only, so routing a "-pro"-looking model id there
    # would 404 a request that would otherwise have worked.
    _SUPPORTS_RESPONSES_API = False

    @classmethod
    def _prefers_responses_api(cls, model_id: str) -> bool:
        return cls._SUPPORTS_RESPONSES_API and is_responses_only_model(model_id)

    async def translate_embedding_request(
        self,
        request: EmbeddingRequest,
    ) -> dict:
        if not self.supports_embeddings:
            return await super().translate_embedding_request(request)
        payload: dict = {
            "model": request.model,
            "input": request.input,
            "encoding_format": request.encoding_format,
        }
        if request.dimensions is not None:
            payload["dimensions"] = request.dimensions
        if request.user is not None:
            payload["user"] = request.user
        return payload

    def translate_embedding_response(
        self,
        provider_response: dict,
    ) -> EmbeddingResponse:
        if not self.supports_embeddings:
            return super().translate_embedding_response(provider_response)
        raw_data = provider_response.get("data")
        if not isinstance(raw_data, list):
            raise ValueError("provider embeddings response is missing data")
        data: list[EmbeddingData] = []
        for position, item in enumerate(raw_data):
            if not isinstance(item, dict):
                raise ValueError("provider embedding item must be an object")
            embedding = item.get("embedding")
            if not isinstance(embedding, (list, str)):
                raise ValueError(
                    "provider embedding must be a vector or base64 string"
                )
            data.append(
                EmbeddingData(
                    index=item.get("index", position),
                    embedding=embedding,
                )
            )
        usage_data = provider_response.get("usage") or {}
        prompt_tokens = usage_data.get("prompt_tokens", 0)
        total_tokens = usage_data.get("total_tokens", prompt_tokens)
        return EmbeddingResponse(
            id=provider_response.get("id", ""),
            data=data,
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=0,
                total_tokens=total_tokens,
            ),
            model=provider_response.get("model", ""),
            provider=self.PROVIDER_NAME,
        )

    async def translate_request(
        self, request: ChatCompletionRequest, *, prompt_caching_enabled: bool = False
    ) -> dict:
        # The "-pro" tier is served only by /v1/responses and answers 400 on Chat
        # Completions, so it needs a different payload shape as well as a
        # different URL (see openai_responses and _openai_url). Keyed on
        # request.model here; http_client resolves it to the mapping's provider
        # model id before translation and enforces that id on the payload.
        if self._prefers_responses_api(request.model):
            return build_responses_payload(request, request.model)

        messages = list(request.messages)

        if request.system:
            messages = [{"role": "system", "content": request.system}, *messages]

        payload: dict = {
            "messages": messages,
            "model": request.model,
        }

        # OpenAI reasoning models (o1/o3/o4 families) reject 'max_tokens' (they
        # require 'max_completion_tokens') and only accept temperature=1. Detect
        # by model id and adjust, so smart routing to these models doesn't 400
        # and trip the provider's circuit breaker.
        is_reasoning = _is_openai_reasoning_model(request.model)

        if request.temperature is not None and not is_reasoning:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            if is_reasoning:
                payload["max_completion_tokens"] = request.max_tokens
            else:
                payload["max_tokens"] = request.max_tokens
        if request.top_p is not None and not is_reasoning:
            payload["top_p"] = request.top_p
        if request.stop is not None:
            payload["stop"] = request.stop
        # Tools are already in OpenAI's own dialect — pass them straight through.
        if request.tools:
            payload["tools"] = request.tools
            if request.tool_choice is not None:
                payload["tool_choice"] = request.tool_choice
        if request.stream:
            payload["stream"] = True
            # Ask the provider to include a final usage chunk so end-of-stream
            # cost accounting uses real token counts (else we estimate).
            payload["stream_options"] = {"include_usage": True}

        return payload

    def translate_response(self, provider_response: dict) -> ChatCompletionResponse:
        # Detected from the payload rather than the model id, which this method
        # never receives. A Responses reply is unambiguous: object="response"
        # with a top-level output list, where Chat Completions has choices.
        if self._is_responses_payload(provider_response):
            return translate_responses_reply(provider_response, self.PROVIDER_NAME)

        usage_data = provider_response.get("usage", {})
        prompt_tokens = usage_data.get("prompt_tokens", 0)
        completion_tokens = usage_data.get("completion_tokens", 0)

        return ChatCompletionResponse(
            id=provider_response.get("id", ""),
            choices=provider_response.get("choices", []),
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            model=provider_response.get("model", ""),
            provider=self.PROVIDER_NAME,
        )

    @staticmethod
    def _is_responses_payload(payload: dict) -> bool:
        """True for a Responses API reply, false for Chat Completions.

        ``object: "response"`` is the reliable marker; the ``output``/no-``choices``
        check is a belt-and-braces fallback in case the field is absent, since
        mistaking one shape for the other silently empties the response.
        """
        if payload.get("object") == "response":
            return True
        return "output" in payload and "choices" not in payload

    def translate_stream_chunk(self, chunk: dict) -> StreamChunk:
        # Responses API SSE events are typed ("response.output_text.delta", …)
        # rather than being chunk objects with choices. An event that carries
        # nothing a client needs translates to None, which execute_streaming
        # skips — so this returns an empty non-final chunk only as a last resort.
        if isinstance(chunk.get("type"), str) and chunk["type"].startswith("response."):
            translated = translate_responses_stream_event(chunk)
            if translated is not None:
                return translated
            return StreamChunk(id="", choices=[], model="", is_final=False)

        choices = chunk.get("choices", [])
        is_final = bool(choices and choices[-1].get("finish_reason") is not None)

        # With stream_options.include_usage, OpenAI sends a trailing chunk that
        # has empty choices and a populated usage object. Treat it as final and
        # attach the token counts for end-of-stream cost accounting.
        usage = None
        usage_data = chunk.get("usage")
        if usage_data:
            prompt_tokens = usage_data.get("prompt_tokens", 0)
            completion_tokens = usage_data.get("completion_tokens", 0)
            usage = TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=usage_data.get("total_tokens", prompt_tokens + completion_tokens),
            )
            is_final = True

        return StreamChunk(
            id=chunk.get("id", ""),
            choices=choices,
            model=chunk.get("model", ""),
            is_final=is_final,
            usage=usage,
        )

    async def list_models(self) -> list[ModelInfo]:
        return list(self._MODELS)

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.PROVIDER_NAME,
            status=HealthStatus.HEALTHY,
            last_check=datetime.now(timezone.utc),
        )
