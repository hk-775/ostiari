"""AI21 Labs provider adapter — OpenAI-compatible (Jamba models)."""

from src.gateway.adapters.openai_style import OpenAIStyleAdapter
from src.gateway.models import ModelInfo

PROVIDER_NAME = "ai21"

_AI21_MODELS = [
    ModelInfo(model_id="jamba-1.6-large", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="jamba-1.6-mini", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
]


class AI21Adapter(OpenAIStyleAdapter):
    """AI21 Labs API — OpenAI-compatible chat completions format."""

    PROVIDER_NAME = PROVIDER_NAME
    _MODELS = _AI21_MODELS
