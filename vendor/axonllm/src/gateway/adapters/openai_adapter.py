"""OpenAI provider adapter for the LLM-Router."""

from src.gateway.adapters.openai_style import OpenAIStyleAdapter
from src.gateway.models import ModelInfo

PROVIDER_NAME = "openai"

_OPENAI_MODELS = [
    ModelInfo(model_id="gpt-4o", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="gpt-4o-mini", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="gpt-4-turbo", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="gpt-3.5-turbo", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
]


class OpenAIAdapter(OpenAIStyleAdapter):
    """Translates between the unified Gateway format and OpenAI's native API format."""

    PROVIDER_NAME = PROVIDER_NAME
    _MODELS = _OPENAI_MODELS
    # Genuine OpenAI serves /v1/responses, which the "-pro" tier requires. The
    # OpenAI-compatible providers sharing this base do not.
    _SUPPORTS_RESPONSES_API = True
    supports_embeddings = True
