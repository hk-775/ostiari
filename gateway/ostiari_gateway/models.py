"""Data models for sidecar configuration."""

from pydantic import BaseModel, Field


class ToolDefinition(BaseModel):
    """A tool that the sidecar can proxy to."""

    name: str
    endpoint: str
    method: str = "POST"
    description: str = ""
    timeout_seconds: float = 30.0
    headers: dict[str, str] = Field(default_factory=dict)
    schema_: dict | None = Field(default=None, alias="schema")
    # REST param placement (populated by the OpenAPI importer). When both are
    # empty (the default), every param is sent as a JSON body — preserving the
    # original behavior for hand-registered tools.
    path_params: list[str] = Field(default_factory=list)
    query_params: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class PolicyConfig(BaseModel):
    """Policy configuration pushed from control plane."""

    allow: list[str] = Field(default_factory=list)
    block: list[str] = Field(default_factory=list)
    rules: list[dict] = Field(default_factory=list)
    thresholds: dict[str, dict[str, int]] = Field(default_factory=dict)


class ModulesConfig(BaseModel):
    """Which modules are active."""

    core: bool = True
    llm_gateway: bool = False
    audit: bool = False


class SidecarConfig(BaseModel):
    """Full sidecar configuration from control plane."""

    tools: list[ToolDefinition] = Field(default_factory=list)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    modules: ModulesConfig = Field(default_factory=ModulesConfig)
    llm: dict = Field(default_factory=dict)
    mcp_servers: list[dict] = Field(default_factory=list)
    quota: dict = Field(default_factory=dict)
    agent_auth: dict = Field(default_factory=dict)
    cross_agent: dict = Field(default_factory=dict)
    payments: dict = Field(default_factory=dict)
    sidecar_id: str = ""
    control_plane_url: str = ""
    # URL the control plane uses to reach THIS gateway for config pushes.
    callback_url: str = ""
    poll_interval_seconds: int = 60
    # Enforcement mode: "enforce" applies policy decisions (block/deny);
    # "shadow" evaluates everything but never blocks and never executes real
    # tool side effects — it records what *would* have happened.
    mode: str = "enforce"
