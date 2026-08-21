"""Azure OpenAI provider adapter for the LLM-Router."""

from src.gateway.adapters.openai_style import OpenAIStyleAdapter
from src.gateway.models import ModelInfo

PROVIDER_NAME = "azure_openai"

_AZURE_MODELS = [
    ModelInfo(model_id="gpt-4-turbo-2024", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="gpt-35-turbo", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="gpt-4o", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
]


class AzureOpenAIAdapter(OpenAIStyleAdapter):
    """Translates between the unified Gateway format and Azure OpenAI's native API format.

    Azure OpenAI uses the same request/response structure as OpenAI.
    """

    PROVIDER_NAME = PROVIDER_NAME
    _MODELS = _AZURE_MODELS
    supports_embeddings = True
