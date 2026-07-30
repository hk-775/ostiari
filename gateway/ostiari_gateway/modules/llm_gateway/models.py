"""Data models for the LLM Gateway module."""

from pydantic import BaseModel, Field


class RoutingRule(BaseModel):
    """A rule for selecting which LLM to use."""

    condition: str
    model: str


class ABExperiment(BaseModel):
    """A/B test configuration — split traffic between models.

    An experiment splits its *in-scope* traffic between model_a (control) and
    model_b (treatment) by traffic_pct_b. ``agents`` optionally scopes the
    experiment to specific agent_ids; empty = applies to all agents. Requests
    not in scope fall through to the next experiment, then to rules/smart/default.
    """

    name: str
    enabled: bool = True
    model_a: str
    model_b: str
    traffic_pct_b: int = 10  # percentage of in-scope traffic to model B (0-100)
    agents: list[str] = Field(default_factory=list)  # empty = all agents


class AgentRoutingPolicy(BaseModel):
    """Per-agent model-selection policy — rotate an agent across several LLMs.

    This is *model selection* (which LLM), distinct from AxonLLM's per-model
    backend load-balancing (which replica/region of a chosen model). It answers
    "for this agent, which model does this request use?" by cycling a list.

    - strategy "round_robin": pick the next model in ``models`` each time.
    - scope "request": advance on every call (true round-robin).
    - scope "session": all calls in one X-Session-Id use the same model; rotate
      between sessions (avoids switching models mid-conversation, which is
      jarring for an interactive coding agent).
    """

    strategy: str = "round_robin"      # round_robin (only strategy for now)
    models: list[str] = Field(default_factory=list)
    scope: str = "request"             # request | session


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
    # None = don't send `temperature` at all, which is not the same as 0.7. Newer
    # models reject the parameter rather than ignoring it (Bedrock Mantle's Claude
    # models answer 400 "`temperature` is deprecated for this model"), so a
    # default here failed every call to them — including calls from clients that
    # never mentioned temperature, since this value was substituted for them.
    # Setting it explicitly still forwards it; only "unconfigured" is now silent.
    temperature: float | None = None
    max_tool_rounds: int = 10
    security: dict | None = None
    ab_experiments: list[ABExperiment] = Field(default_factory=list)
    # Per-agent model-rotation policies, keyed by agent_id. A "*" key applies to
    # any agent without a specific entry.
    agent_routing: dict[str, AgentRoutingPolicy] = Field(default_factory=dict)


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
