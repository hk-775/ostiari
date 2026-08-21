"""Together AI provider adapter — OpenAI-compatible."""

from src.gateway.adapters.openai_style import OpenAIStyleAdapter
from src.gateway.models import ModelInfo

PROVIDER_NAME = "together"

# Only models Together serves over the *shared serverless* endpoint, each
# confirmed with a live completion. Appearing in GET /v1/models is not enough:
# most of the ~163 chat models there need a dedicated endpoint provisioned per
# account and otherwise return 400 "Unable to access non-serverless model".
# Four of the five ids previously listed here were in that state
# (Llama-4-Maverick-FP8, DeepSeek-R1, Qwen2.5-72B-Instruct-Turbo,
# Mistral-Small-24B), so advertising them promised capacity this account has
# never had.
_TOGETHER_MODELS = [
    ModelInfo(model_id="meta-llama/Llama-3.3-70B-Instruct-Turbo", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="deepseek-ai/DeepSeek-V4-Pro", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="openai/gpt-oss-120b", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="Qwen/Qwen3.5-9B", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
]


class TogetherAdapter(OpenAIStyleAdapter):
    """Together AI API — OpenAI-compatible format."""

    PROVIDER_NAME = PROVIDER_NAME
    _MODELS = _TOGETHER_MODELS
