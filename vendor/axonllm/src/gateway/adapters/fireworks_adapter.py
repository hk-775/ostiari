"""Fireworks AI provider adapter — OpenAI-compatible."""

from src.gateway.adapters.openai_style import OpenAIStyleAdapter
from src.gateway.models import ModelInfo

PROVIDER_NAME = "fireworks"

_FIREWORKS_MODELS = [
    ModelInfo(model_id="accounts/fireworks/models/llama-v3p3-70b-instruct", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="accounts/fireworks/models/llama-v3p1-405b-instruct", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="accounts/fireworks/models/deepseek-r1", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="accounts/fireworks/models/qwen2p5-72b-instruct", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
]


class FireworksAdapter(OpenAIStyleAdapter):
    """Fireworks AI API — OpenAI-compatible format."""

    PROVIDER_NAME = PROVIDER_NAME
    _MODELS = _FIREWORKS_MODELS
