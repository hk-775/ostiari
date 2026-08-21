"""xAI (Grok) provider adapter — OpenAI-compatible."""

from src.gateway.adapters.openai_style import OpenAIStyleAdapter
from src.gateway.models import ModelInfo

PROVIDER_NAME = "xai"

# Verified against GET /v1/language-models. grok-3 and grok-3-mini were listed
# here and still answer 200, but xAI resolves both to grok-4.3 — they are
# undocumented aliases, so advertising them named a model the caller does not
# get and could not be priced. grok-2-vision-1212 was listed too and is simply
# gone: 400 "Model not found".
_XAI_MODELS = [
    ModelInfo(model_id="grok-4.5", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="grok-4.3", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
]


class XAIAdapter(OpenAIStyleAdapter):
    """xAI Grok API — OpenAI-compatible format."""

    PROVIDER_NAME = PROVIDER_NAME
    _MODELS = _XAI_MODELS
