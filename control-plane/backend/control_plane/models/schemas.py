"""Pydantic API schemas."""

from datetime import datetime

from pydantic import BaseModel, Field

# ─── Gateway schemas ─────────────────────────────────────────────────────

class GatewayCreate(BaseModel):
    id: str
    name: str
    endpoint: str
    description: str = ""


class GatewayUpdate(BaseModel):
    name: str | None = None
    endpoint: str | None = None
    description: str | None = None


class GatewayResponse(BaseModel):
    id: str
    name: str
    endpoint: str
    description: str
    status: str
    last_heartbeat: datetime | None
    tools_count: int = 0
    mode: str = "enforce"
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ─── Tool schemas ────────────────────────────────────────────────────────

class ToolCreate(BaseModel):
    name: str
    endpoint: str
    method: str = "POST"
    description: str = ""
    timeout_seconds: float = 30.0
    schema_json: dict | None = None


class ToolResponse(BaseModel):
    id: int
    name: str
    endpoint: str
    method: str
    description: str
    timeout_seconds: float
    schema_json: dict | None
    path_params: list[str] | None = None
    query_params: list[str] | None = None
    gateway_id: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Policy schemas ──────────────────────────────────────────────────────

class PolicyCreate(BaseModel):
    name: str
    description: str = ""
    content: dict
    gateway_id: str | None = None


class PolicyUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    content: dict | None = None
    is_active: bool | None = None


class PolicyResponse(BaseModel):
    id: int
    name: str
    description: str
    content: dict
    is_active: bool
    gateway_id: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ─── LLM Config schemas ─────────────────────────────────────────────────

class LLMConfigUpdate(BaseModel):
    default_model: str = "claude-sonnet-4-6"
    routing_rules: list[dict] = Field(default_factory=list)
    fallback_chain: list[str] = Field(default_factory=list)
    credentials: dict = Field(default_factory=dict)
    security: dict | None = None
    max_tokens: int = 4096
    temperature: float = 0.7


# ─── Usage / Cost schemas ────────────────────────────────────────────────

class UsageRecordCreate(BaseModel):
    gateway_id: str
    event_id: str | None = Field(default=None, max_length=64)
    agent_id: str = "unknown"
    model: str
    experiment_name: str = Field(default="", max_length=128)
    experiment_variant: str = Field(default="", max_length=8)
    provider: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    action: str = ""


class UsageRecordResponse(BaseModel):
    id: int
    gateway_id: str
    event_id: str | None = None
    agent_id: str
    model: str
    experiment_name: str = ""
    experiment_variant: str = ""
    provider: str = ""
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float
    action: str
    broker_cost_usd: float = 0.0
    broker_charge_usd: float = 0.0
    billing_status: str = "not_applicable"
    billing_ref: str = ""
    billing_error: str = ""
    timestamp: datetime

    model_config = {"from_attributes": True}


class CostSummary(BaseModel):
    total_cost_usd: float
    total_tokens: int
    total_requests: int
    by_model: list[dict] = Field(default_factory=list)
    by_gateway: list[dict] = Field(default_factory=list)
    by_agent: list[dict] = Field(default_factory=list)
    daily_costs: list[dict] = Field(default_factory=list)


# ─── MCP Server schemas ──────────────────────────────────────────────────

class McpServerCreate(BaseModel):
    name: str
    mode: str = "embedded"  # embedded | remote | stdio
    package: str = ""
    module: str = ""
    url: str = ""
    command: list[str] = Field(default_factory=list)
    config: dict = Field(default_factory=dict)
    allowed_tools: list[str] | None = None
    blocked_tools: list[str] = Field(default_factory=list)
    prefix: str = ""


class McpServerResponse(BaseModel):
    id: int
    name: str
    mode: str
    package: str
    module: str
    url: str
    command: list[str]
    config: dict
    allowed_tools: list[str] | None
    blocked_tools: list[str]
    prefix: str
    gateway_id: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Push / Sync schemas ────────────────────────────────────────────────

class PushResult(BaseModel):
    gateway_id: str
    status: str
    message: str = ""


class PushResponse(BaseModel):
    results: list[PushResult]
    total: int
    succeeded: int
    failed: int


# ─── Audit schemas ───────────────────────────────────────────────────────

class AuditLogResponse(BaseModel):
    id: int
    actor: str
    action: str
    resource_type: str
    resource_id: str
    details: dict
    timestamp: datetime

    model_config = {"from_attributes": True}
