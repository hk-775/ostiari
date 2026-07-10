"""Data models for the LLM Gateway module."""

from pydantic import BaseModel, Field


class RoutingRule(BaseModel):
    """A rule for selecting which LLM to use."""

    condition: str
    model: str


class ABExperiment(BaseModel):
    """A/B test configuration — split traffic between models."""

    name: str
    enabled: bool = True
    model_a: str
    model_b: str
    traffic_pct_b: int = 10  # percentage of traffic to model B (0-100)


class LLMCredentials(BaseModel):
    """Credentials for all supported LLM providers."""

    anthropic: str = ""
    openai: str = ""
    azure_endpoint: str = ""
    azure_api_key: str = ""
    azure_api_version: str = "2024-02-01"
    bedrock_region: str = "us-east-1"
    cohere_api_key: str = ""
    vertex_project: str = ""
    vertex_location: str = "us-central1"


class SecurityConfig(BaseModel):
    """Security settings for the LLM Gateway."""

    pii_redaction: bool = False
    injection_detection: bool = False
    injection_threshold: float = 0.7


class LLMConfig(BaseModel):
    """LLM Gateway configuration pushed from control plane."""

    default_model: str = "claude-sonnet-4-6"
    routing_rules: list[RoutingRule] = Field(default_factory=list)
    fallback_chain: list[str] = Field(default_factory=list)
    credentials: LLMCredentials = Field(default_factory=LLMCredentials)
    max_tokens: int = 4096
    temperature: float = 0.7
    max_tool_rounds: int = 10
    security: dict | None = None
    ab_experiments: list[ABExperiment] = Field(default_factory=list)


class InvokeRequest(BaseModel):
    """Request body for POST /invoke."""

    messages: list[dict] = Field(..., min_length=1)
    tools: list[str] | None = None
    model_override: str | None = None
    context: dict = Field(default_factory=dict)
    intent_template: str | None = None
    intent_variables: dict[str, str] = Field(default_factory=dict)


class InvokeResponse(BaseModel):
    """Response from POST /invoke."""

    response: str
    model_used: str
    tool_calls: list[dict] = Field(default_factory=list)
    blocked_actions: list[dict] = Field(default_factory=list)
    total_tokens: int = 0
    rounds: int = 0
    cache_hit: bool = False
    ab_experiment: str | None = None
    ab_variant: str | None = None
