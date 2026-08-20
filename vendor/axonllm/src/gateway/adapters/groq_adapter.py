"""Groq provider adapter — OpenAI-compatible."""

from src.gateway.adapters.openai_style import OpenAIStyleAdapter
from src.gateway.models import ModelInfo

PROVIDER_NAME = "groq"

_GROQ_MODELS = [
    ModelInfo(model_id="llama-3.3-70b-versatile", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="llama-3.1-8b-instant", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="mixtral-8x7b-32768", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="gemma2-9b-it", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
]


class GroqAdapter(OpenAIStyleAdapter):
    """Groq API — OpenAI-compatible format with ultra-fast inference."""

    PROVIDER_NAME = PROVIDER_NAME
    _MODELS = _GROQ_MODELS
