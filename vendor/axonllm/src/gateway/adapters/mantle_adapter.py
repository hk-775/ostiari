"""AWS Bedrock Mantle provider adapter — OpenAI-compatible Chat Completions via bedrock-mantle endpoint."""

from src.gateway.adapters.openai_style import OpenAIStyleAdapter
from src.gateway.models import ModelInfo

PROVIDER_NAME = "bedrock-mantle"

# Verified against GET /v1/models on the Mantle endpoint. Four of the six ids
# previously listed here are not served: anthropic.claude-sonnet-4-6,
# anthropic.claude-opus-4-6-v1 and anthropic.claude-haiku-4-5-20251001-v1:0
# carried Bedrock-style version suffixes that Mantle does not use, and
# meta.llama4-maverick-17b-instruct-v1:0 has no meta.* equivalent in the Mantle
# catalogue at all. Nothing broke, because request routing reads models.yaml
# rather than this list — which is exactly why the drift went unnoticed.
_MANTLE_MODELS = [
    ModelInfo(model_id="anthropic.claude-opus-4-8", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="anthropic.claude-sonnet-5", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="anthropic.claude-haiku-4-5", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="openai.gpt-5.6-sol", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="openai.gpt-5.6-terra", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="openai.gpt-5.6-luna", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="openai.gpt-5.5", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="openai.gpt-5.4", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="openai.gpt-oss-120b", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="deepseek.v3.1", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="qwen.qwen3-32b", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
]


class MantleAdapter(OpenAIStyleAdapter):
    """Translates between the unified Gateway format and Bedrock Mantle's OpenAI-compatible API.

    Mantle uses the standard OpenAI Chat Completions request/response format.
    Auth is handled via SigV4 or Bedrock API key at the HTTP layer.
    """

    PROVIDER_NAME = PROVIDER_NAME
    _MODELS = _MANTLE_MODELS
