"""Core data models and types for the LLM-Router service."""

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _utcnow() -> datetime:
    """Timezone-aware UTC now — the default factory for created_at fields.

    Aware (not naive datetime.utcnow()) so these timestamps compare and sort
    cleanly against tz-aware timestamps produced elsewhere in the gateway.
    """
    return datetime.now(timezone.utc)


# --- Enums ---


class RoutingStrategy(Enum):
    """Routing strategies for distributing requests across providers."""

    ROUND_ROBIN = "round-robin"
    WEIGHTED = "weighted"
    LEAST_LATENCY = "least-latency"
    COST_OPTIMIZED = "cost-optimized"
    SMART = "smart"
    ENSEMBLE = "ensemble"


class HealthStatus(Enum):
    """Health status for provider health checks."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DEGRADED = "degraded"


# --- Chat Completion ---


@dataclass
class ChatCompletionRequest:
    """OpenAI-compatible chat completion request.

    ``tools``/``tool_choice`` are OpenAI-shaped
    (``{"type": "function", "function": {"name", "description", "parameters"}}``)
    and each adapter translates them into its provider's dialect. They must be
    carried, not dropped: a request whose tools go missing still gets a fluent
    HTTP 200 from a model that was never told the tools exist — it just answers
    that it has no such capability, which reads as success and isn't.
    """

    messages: list[dict]
    model: str
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop: list[str] | None = None
    stream: bool = False
    system: str | None = None
    tools: list[dict] | None = None
    tool_choice: str | dict | None = None


@dataclass
class TokenUsage:
    """Token usage statistics for a completion request."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass
class ChatCompletionResponse:
    """Unified chat completion response in OpenAI-compatible format."""

    id: str
    choices: list[dict]
    usage: TokenUsage
    model: str
    provider: str
    warnings: list[str] = field(default_factory=list)
    # Internal provider-side identifier used for pricing and reconciliation.
    # Public responses continue to expose the logical gateway model in
    # ``model``.
    provider_model: str | None = None


@dataclass
class StreamChunk:
    """A single chunk in a streaming completion response.

    ``usage`` is populated on the final chunk when the provider reports token
    counts in-stream (OpenAI ``stream_options.include_usage``; Anthropic
    ``message_delta``). It drives accurate end-of-stream cost accounting; when
    absent the gateway estimates from the accumulated text.
    """

    id: str
    choices: list[dict]
    model: str
    is_final: bool = False
    usage: TokenUsage | None = None


# --- Embeddings ---


@dataclass
class EmbeddingRequest:
    """Normalized embeddings request routed across provider deployments."""

    input: list[str]
    model: str
    encoding_format: str = "float"
    dimensions: int | None = None
    user: str | None = None


@dataclass
class EmbeddingData:
    """One embedding vector in the same order as the corresponding input."""

    index: int
    embedding: list[float] | str


@dataclass
class EmbeddingResponse:
    """Provider-neutral embeddings result."""

    id: str
    data: list[EmbeddingData]
    usage: TokenUsage
    model: str
    provider: str
    provider_model: str | None = None


# --- Model Registry & Pricing ---


@dataclass
class TokenPricing:
    """Per-token pricing for a provider/model combination.

    Beyond basic input/output token costs, supports:
    - cached_token_cost: discounted rate for cached input tokens (OpenAI, Anthropic)
    - image_token_cost: rate for image/vision tokens (multimodal models)
    - reasoning_token_cost: rate for internal reasoning tokens (o1/o3 models)
    - per_request_cost: flat fee per API call (some providers charge this)
    """

    prompt_token_cost: float
    completion_token_cost: float
    cached_token_cost: float | None = None
    cache_creation_token_cost: float | None = None
    image_token_cost: float | None = None
    reasoning_token_cost: float | None = None
    per_request_cost: float = 0.0

    @property
    def is_billable(self) -> bool:
        """Whether this entry can produce a finite, non-negative charge.

        A pair of ``0.0`` placeholder rates is not pricing. Treating it as one
        would make production routing and the coverage audit say a model is
        safe while every request still records a zero cost.
        """
        rates = [
            self.prompt_token_cost,
            self.completion_token_cost,
            self.cached_token_cost,
            self.cache_creation_token_cost,
            self.image_token_cost,
            self.reasoning_token_cost,
            self.per_request_cost,
        ]
        configured = [rate for rate in rates if rate is not None]
        return (
            bool(configured)
            and all(
                not isinstance(rate, bool)
                and isinstance(rate, (int, float))
                and math.isfinite(rate)
                and rate >= 0
                for rate in configured
            )
            and any(rate > 0 for rate in configured)
        )


@dataclass
class ProviderModelMapping:
    """Maps a model to a specific provider and model identifier."""

    provider: str
    model_id: str
    weight: float = 1.0
    fallback_order: int = 0
    pricing: TokenPricing | None = None


@dataclass
class ModelConfig:
    """Configuration for a model with provider mappings."""

    name: str
    description: str
    providers: list[ProviderModelMapping]
    routing_strategy: RoutingStrategy = RoutingStrategy.ROUND_ROBIN
    capabilities: list[str] | None = None
    max_context_tokens: int | None = None


# --- Guardrails ---


@dataclass
class GuardrailRule:
    """A configurable rule for inspecting requests and responses."""

    name: str
    rule_type: str
    pattern: str | None
    action: str
    applies_to: str


@dataclass
class GuardrailResult:
    """Result of evaluating guardrail rules against a request or response."""

    passed: bool
    violated_rules: list[str]
    message: str | None = None


# --- Project & Configuration ---


@dataclass
class Project:
    """A logical grouping of users, budgets, and configuration."""

    project_id: str
    name: str
    tenant_id: str | None = field(default=None, kw_only=True)
    budget_limit: float | None = None
    alert_threshold: float | None = None
    allowed_models: list[str] | None = None
    guardrail_rules: list[GuardrailRule] = field(default_factory=list)
    cache_enabled: bool = False
    cache_ttl_seconds: int = 300
    # Reuse a cached response for a *reworded* question, not just a byte-identical
    # one. Separate from cache_enabled and defaulting to off: exact matching can
    # only return the answer to the question asked, while semantic matching can
    # return the answer to a different one. Opting into the first must not opt
    # you into the second.
    semantic_cache_enabled: bool = False
    # None means "use the gateway default" (semantic_cache.DEFAULT_SIMILARITY_THRESHOLD)
    # rather than 0.0, which would match everything.
    semantic_cache_threshold: float | None = None
    log_level: str = "INFO"
    log_destination: str | None = None
    prompt_caching_enabled: bool = False
    ltm_enabled: bool = False
    retention_period_hours: int = 24
    rate_limit_rpm: int | None = None
    members: list[str] = field(default_factory=list)
    revision: int = 0
    created_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if self.tenant_id is not None and not self.tenant_id.strip():
            raise ValueError("tenant_id must be None or non-empty")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or self.revision < 0
        ):
            raise ValueError("revision must be a non-negative integer")


@dataclass
class RateLimitConfig:
    """Rate limit configuration for users and projects."""

    user_rpm: int = 60
    project_rpm: int = 600
    window_seconds: int = 60


# --- Provider Health ---


@dataclass
class ProviderHealth:
    """Health status of a provider."""

    provider: str
    status: HealthStatus
    latency_ms: float | None = None
    last_check: datetime | None = None
    error_message: str | None = None


# --- Usage & Cost ---


@dataclass
class UsageRecord:
    """A single usage record for a completed request."""

    # Gateway-generated and unique per request (``req_<uuid>``). Trace/span ids
    # are derived from it and usage rows are de-duped by it, so it must never be
    # replaced with a provider-supplied id: those are not guaranteed unique, and
    # some providers return a constant placeholder for every call.
    request_id: str
    project_id: str
    user_id: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float
    timestamp: datetime
    tenant_id: str | None = None
    cached_tokens: int = 0
    cache_creation_tokens: int = 0
    image_tokens: int = 0
    reasoning_tokens: int = 0
    latency_ms: float = 0.0
    status: str = "success"
    routing_strategy: str = ""
    # Task type from the prompt classifier ("math", "coding", ...). Empty string
    # means "not classified", which is NOT the same as the classifier having
    # returned "general" — records written before this field existed, and records
    # from paths that never classify, carry "" so aggregates can exclude them
    # instead of counting them as a real result. See UserEfficiencyProfile.
    task_type: str = ""
    # The provider's own id for the upstream call, kept for correlating a trace
    # with provider-side logs. Informational only — never used as a key, since
    # it may be absent, repeated, or a fixed placeholder.
    provider_request_id: str = ""


@dataclass
class BudgetStatus:
    """Budget status for a project."""

    project_id: str
    current_spend: float
    budget_limit: float | None
    alert_threshold: float | None
    is_over_budget: bool
    is_alert_triggered: bool


@dataclass
class UsageFilters:
    """Filters for querying aggregated usage data."""

    start_time: datetime | None = None
    end_time: datetime | None = None
    provider: str | None = None
    model: str | None = None
    project_id: str | None = None
    user_id: str | None = None
    tenant_id: str | None = None


@dataclass
class UsageBreakdown:
    """Usage breakdown by a grouping dimension."""

    group_key: str
    group_by: str
    requests: int
    tokens: int
    cost: float


@dataclass
class UsageReport:
    """Aggregated usage report with breakdown."""

    total_requests: int
    total_tokens: int
    total_cost: float
    breakdown: list[UsageBreakdown]


# --- Rate Limiting ---


@dataclass
class RateLimitResult:
    """Result of a rate limit check."""

    allowed: bool
    limit: int
    remaining: int
    reset_at: datetime
    retry_after_seconds: int | None = None


@dataclass
class BudgetReservationResult:
    """Outcome of an atomic multi-scope budget reservation or finalization."""

    allowed: bool
    request_id: str
    reserved_amount: float
    totals: dict[str, float] = field(default_factory=dict)
    epochs: dict[str, int] = field(default_factory=dict)
    state: str = "reserved"
    denied_scope: str | None = None
    idempotent: bool = False
    crossed_thresholds: tuple[float, ...] = ()


@dataclass(frozen=True)
class SpendCounterState:
    """One billing-cycle spend total and its monotonic reset epoch."""

    total: float
    epoch: int


# --- Auth ---


class AuthMethod(Enum):
    """How the request was authenticated."""

    OIDC_JWT = "oidc_jwt"
    API_KEY = "api_key"
    SSO = "sso"          # SAML 2.0 assertion
    ANONYMOUS = "anonymous"


class TenantStatus(Enum):
    """Whether a tenant may authenticate and use the gateway."""

    ACTIVE = "active"
    SUSPENDED = "suspended"


class MembershipStatus(Enum):
    """Lifecycle state for a principal's tenant membership."""

    INVITED = "invited"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DEPROVISIONED = "deprovisioned"


class TenantRole(Enum):
    """Server-assigned roles used by the baseline authorization policy."""

    PLATFORM_ADMIN = "platform_admin"
    TENANT_ADMIN = "tenant_admin"
    TENANT_MEMBER = "tenant_member"
    TENANT_AUDITOR = "tenant_auditor"
    SERVICE = "service"


@dataclass(frozen=True)
class Tenant:
    """An isolation boundary for customer-owned configuration and data."""

    tenant_id: str
    name: str
    status: TenantStatus = TenantStatus.ACTIVE
    created_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.tenant_id.strip():
            raise ValueError("tenant_id must not be empty")
        if not self.name.strip():
            raise ValueError("tenant name must not be empty")


@dataclass(frozen=True)
class TenantMembership:
    """Authoritative role and project grants for one tenant principal."""

    membership_id: str
    tenant_id: str
    principal_id: str
    role: TenantRole
    status: MembershipStatus = MembershipStatus.ACTIVE
    project_ids: frozenset[str] = field(default_factory=frozenset)
    authorization_version: int = 1

    def __post_init__(self) -> None:
        if not self.membership_id.strip():
            raise ValueError("membership_id must not be empty")
        if not self.tenant_id.strip():
            raise ValueError("tenant_id must not be empty")
        if not self.principal_id.strip():
            raise ValueError("principal_id must not be empty")
        if not isinstance(self.role, TenantRole):
            raise TypeError("role must be a TenantRole")
        if self.authorization_version < 1:
            raise ValueError("authorization_version must be positive")
        if any(not project_id.strip() for project_id in self.project_ids):
            raise ValueError("project_ids must not contain empty values")


@dataclass(frozen=True)
class Principal:
    """Canonical identity resolved from server-held membership state.

    Identity-provider claims are inputs to principal resolution, not authority.
    Roles, scopes, project grants, status, and the authorization version here
    must come from the tenant identity repository.
    """

    principal_id: str
    tenant_id: str
    subject: str
    issuer: str
    roles: frozenset[TenantRole]
    auth_method: AuthMethod
    membership_status: MembershipStatus = MembershipStatus.ACTIVE
    project_ids: frozenset[str] = field(default_factory=frozenset)
    scopes: frozenset[str] = field(default_factory=frozenset)
    authorization_version: int = 1
    credential_id: str | None = None
    email: str | None = None

    def __post_init__(self) -> None:
        required = {
            "principal_id": self.principal_id,
            "tenant_id": self.tenant_id,
            "subject": self.subject,
            "issuer": self.issuer,
        }
        for field_name, value in required.items():
            if not value.strip():
                raise ValueError(f"{field_name} must not be empty")
        if self.auth_method is AuthMethod.ANONYMOUS:
            raise ValueError("anonymous requests cannot create a Principal")
        if not self.roles:
            raise ValueError("roles must not be empty")
        if any(not isinstance(role, TenantRole) for role in self.roles):
            raise TypeError("roles must contain only TenantRole values")
        exclusive_roles = {
            TenantRole.PLATFORM_ADMIN,
            TenantRole.SERVICE,
        }
        if len(self.roles) > 1 and not self.roles.isdisjoint(exclusive_roles):
            raise ValueError(
                "platform_admin and service roles cannot be combined with "
                "other roles"
            )
        if self.authorization_version < 1:
            raise ValueError("authorization_version must be positive")
        if any(not value.strip() for value in self.project_ids):
            raise ValueError("project_ids must not contain empty values")
        if any(not value.strip() for value in self.scopes):
            raise ValueError("scopes must not contain empty values")


@dataclass
class RequestContext:
    """Credential claims retained until a canonical Principal is resolved."""

    user_id: str
    project_id: str
    roles: list[str]
    scopes: list[str]
    auth_method: AuthMethod = AuthMethod.ANONYMOUS
    tenant_id: str | None = None
    business_unit: str | None = None
    environment: str | None = None
    api_key_id: str | None = None
    email: str | None = None
    issuer: str | None = None
    subject: str | None = None
    principal_id: str | None = None
    authorization_version: int | None = None
    authorized_project: Project | None = None
    allow_legacy_project_lookup: bool = False


@dataclass
class APIKey:
    """A project-scoped API key."""

    key_id: str
    key_hash: str
    project_id: str
    name: str
    scopes: list[str]
    created_by: str
    tenant_id: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    expires_at: datetime | None = None
    revoked: bool = False
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    last_used_at: datetime | None = None


@dataclass
class PolicyNode:
    """A single node in the hierarchical policy tree."""

    node_id: str
    node_type: str  # "org" | "business_unit" | "project" | "environment"
    parent_id: str | None
    display_name: str
    limits: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class ResolvedPolicy:
    """Flattened effective policy after hierarchy walk."""

    rate_limit_rpm: int | None = None
    budget_limit: float | None = None
    allowed_models: list[str] | None = None
    max_tokens_per_request: int | None = None
    allowed_providers: list[str] | None = None
    pii_redaction_enabled: bool = False
    pii_redact_types: list[str] | None = None
    # When False, redaction is permanent: no reversible mapping is retained and
    # the original PII is NOT re-injected into the response. Strict-regime mode
    # (no plaintext held in memory). Defaults to True to preserve behavior.
    pii_reinject: bool = True
    # Named-entity detection for the PII types regex cannot express (names,
    # addresses, ages). Off by default and separate from pii_redaction_enabled
    # because it calls a paid per-request service — measured at more than the
    # model's own input-token cost for the same text — so enabling redaction
    # must not silently enable it. See security/pii_ner.py.
    pii_ner_enabled: bool = False
    pii_ner_types: list[str] | None = None


# --- Validation ---


@dataclass
class ValidationError:
    """A validation error for configuration or request validation."""

    field: str
    message: str
    severity: str = "error"


# --- Logging ---


@dataclass
class RequestLogEntry:
    """Structured log entry for a processed request."""

    request_id: str
    project_id: str
    user_id: str
    model: str
    provider: str
    latency_ms: float
    status_code: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float
    timestamp: datetime
    trace_id: str | None = None
    is_streaming: bool = False
    is_cached: bool = False
    retry_count: int = 0
    fallback_providers_tried: list[str] = field(default_factory=list)


# --- Model Info ---


@dataclass
class ModelInfo:
    """Information about a provider-specific model."""

    model_id: str
    provider: str
    capabilities: list[str] = field(default_factory=list)


@dataclass
class ModelSummary:
    """Public-facing summary of a model."""

    name: str
    description: str
    providers: list[str]
    capabilities: list[str]
    routing_strategy: str


# --- Smart Routing ---


@dataclass
class ClassificationResult:
    """Result of prompt task classification."""

    task_type: str
    confidence: float
    matched_keywords: list[str] = field(default_factory=list)


@dataclass
class ModelScore:
    """A model's benchmark score for a task type."""

    model_name: str
    score: float


@dataclass
class SmartRoutingDecision:
    """Metadata about a smart routing decision for observability."""

    task_type: str
    confidence: float
    selected_model: str
    benchmark_score: float
    candidates_considered: list[dict]
    used_fallback: bool
    cost_quality_tradeoff: float


@dataclass
class FeedbackRecord:
    """Record of a smart routing decision for feedback tracking."""

    request_id: str
    timestamp: datetime
    task_type: str
    confidence: float
    selected_model: str
    benchmark_score: float


# --- Token Efficiency ---


class EfficiencyGrade(Enum):
    """Token efficiency grades for users and projects."""

    EXCELLENT = "excellent"
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"
    WASTEFUL = "wasteful"


@dataclass
class EfficiencyMetrics:
    """Per-user or per-project token efficiency metrics (Level 1 — ratio-based)."""

    entity_id: str
    entity_type: str
    completion_prompt_ratio: float
    cache_utilization_rate: float
    avg_cost_per_request: float
    expensive_model_ratio: float
    token_velocity_per_hour: float
    duplicate_request_rate: float
    avg_prompt_tokens: float
    avg_completion_tokens: float
    total_requests: int
    total_cost: float
    grade: EfficiencyGrade
    score: float


@dataclass
class EfficiencyAlert:
    """Alert raised when a user or project exceeds efficiency thresholds."""

    entity_id: str
    entity_type: str
    alert_type: str
    severity: str
    message: str
    metric_value: float
    threshold: float
    timestamp: datetime


@dataclass
class ModelRecommendation:
    """Recommendation to use a different model for cost efficiency."""

    current_model: str
    recommended_model: str
    task_type: str
    estimated_savings_pct: float
    quality_impact: str
    reason: str


@dataclass
class EfficiencyReport:
    """Full efficiency report combining metrics, alerts, and recommendations."""

    metrics: EfficiencyMetrics
    alerts: list[EfficiencyAlert]
    recommendations: list[ModelRecommendation]
    peer_comparison: dict


# --- Ensemble Routing ---


@dataclass
class EnsemblePreset:
    """A named ensemble configuration: a panel, a judge, and policy knobs."""

    name: str
    panel: list[str]  # 1..10 model identifiers
    judge: str  # exactly one judge/synthesis model
    quorum: int = 1  # 1..len(panel); default 1
    fallback_policy: str = "error"  # "best-single" | "error"; default "error"
    cost_ceiling: float | None = None  # per-request ceiling in USD; None = no ceiling
    ranking_criteria: str = "length"  # how to rank survivors for best-single fallback


@dataclass
class PanelMemberResult:
    """Outcome of a single panel member call within an ensemble request."""

    model: str
    status: str  # "succeeded" | "failed"
    response: ChatCompletionResponse | None = None
    cost: float = 0.0
    failure_reason: str | None = None  # populated when status == "failed"
    latency_ms: float | None = None


@dataclass
class EnsembleDecision:
    """Observability metadata returned with an ensemble response."""

    preset_name: str
    panel_members: list[str]  # every panel model used
    judge_model: str
    succeeded: list[str]  # survivor model identifiers
    failed: list[dict]  # [{"model": str, "reason": str}, ...]
    quorum_met: bool
    succeeded_count: int
    quorum_threshold: int
    total_cost: float  # sum(survivor costs) + judge cost
    cost_multiplier: float  # N + 1
    fallback_used: bool = False  # True when best-single fallback returned
    judge_invoked: bool = False
    error: str | None = None  # set when quorum not met / synthesis failed


# --- SCIM 2.0 identity (enterprise provisioning) ---


@dataclass
class ScimUser:
    """A SCIM 2.0 User resource (IdP-provisioned identity).

    ``active`` drives joiner/mover/leaver: an IdP deprovision sends
    ``active=false`` (or DELETE), which revokes the user's access. In canonical
    mode, ``groups`` is a read-only projection derived from ``Group.members``.
    """

    id: str
    user_name: str
    tenant_id: str = ""
    issuer: str = ""
    subject: str = ""
    active: bool = True
    external_id: str | None = None
    display_name: str = ""
    emails: list[dict] = field(default_factory=list)  # [{"value","primary"}]
    groups: list[str] = field(default_factory=list)   # group ids
    roles: list[str] = field(default_factory=list)
    project_id: str = ""
    project_ids: list[str] = field(default_factory=list)
    authorization_version: int = 1
    deleted: bool = False
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    @property
    def primary_email(self) -> str:
        for e in self.emails:
            if e.get("primary"):
                return e.get("value", "")
        return self.emails[0].get("value", "") if self.emails else ""


@dataclass
class ScimGroup:
    """A SCIM 2.0 Group resource. ``roles`` are granted to member users."""

    id: str
    display_name: str
    tenant_id: str = ""
    external_id: str | None = None
    members: list[str] = field(default_factory=list)  # user ids
    roles: list[str] = field(default_factory=list)
    authorization_version: int = 1
    deleted: bool = False
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
