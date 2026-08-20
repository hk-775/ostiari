"""AWS Bedrock provider adapter for the LLM-Router."""

import logging

from src.gateway.adapters.anthropic_style import AnthropicStyleAdapter
from src.gateway.models import ModelInfo

logger = logging.getLogger(__name__)

PROVIDER_NAME = "bedrock"

_BEDROCK_MODELS = [
    ModelInfo(model_id="anthropic.claude-3-sonnet-20240229-v1:0", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="anthropic.claude-3-haiku-20240307-v1:0", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="amazon.titan-text-express-v1", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
]


class BedrockAdapter(AnthropicStyleAdapter):
    """Translates between the unified Gateway format and AWS Bedrock's native API format.

    Bedrock uses an Anthropic-like format for Claude models: system as a separate
    field, messages without system role. Response usage keys may use camelCase.
    """

    PROVIDER_NAME = PROVIDER_NAME
    _MODELS = _BEDROCK_MODELS

    # Bedrock may return camelCase usage keys
    _prompt_tokens_alt = "inputTokens"
    _completion_tokens_alt = "outputTokens"
