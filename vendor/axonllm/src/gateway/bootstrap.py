"""Centralized bootstrap for AxonLLM gateway components.

Both ``serve_dashboard.py`` and ``agentcore_agent.py`` delegate to this
module instead of duplicating inline wiring.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import cast

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.gateway.admin.audit_routes import AuditAPI, create_audit_routes
from src.gateway.admin.datasource_routes import (
    DatasourceAPI,
    create_datasource_routes,
)
from src.gateway.admin.key_routes import KeyManagementAPI, create_key_routes
from src.gateway.admin.policy_routes import PolicyHierarchyAPI, create_policy_hierarchy_routes
from src.gateway.admin.quota_routes import QuotaAPI, create_quota_routes
from src.gateway.admin.region_routes import RegionAPI, create_region_routes
from src.gateway.admin.routes import (
    AdminAPI,
    PROVIDER_MODEL_CATALOG,
    create_admin_routes,
    create_site_routes,
)
from src.gateway.admin.webhook_routes import WebhookAPI, create_webhook_routes
from src.gateway.agent import GatewayAgent
from src.gateway.auth.api_key_service import APIKeyService
from src.gateway.auth.browser_session import (
    BrowserAuthAPI,
    BrowserSessionConfig,
    BrowserSessionService,
    CONFIG_PATH as BROWSER_AUTH_CONFIG_PATH,
    DynamoBrowserSessionStore,
    create_browser_auth_routes,
)
from src.gateway.auth.cedar_policy import CedarPolicyService
from src.gateway.auth.dynamo_principal_repository import DynamoPrincipalRepository
from src.gateway.auth.oidc_service import OIDCConfig, OIDCService
from src.gateway.auth.policy_hierarchy import PolicyHierarchyResolver
from src.gateway.auth.principal import CanonicalPrincipalResolver, PrincipalResolver
from src.gateway.auth.project_repository import (
    DynamoProjectRepository,
    ProjectResolver,
)
from src.gateway.auth.saml_routes import SamlAPI, create_saml_routes
from src.gateway.auth.saml_service import SamlService, load_saml_config
from src.gateway.auth.scim_routes import ScimAPI, create_scim_routes
from src.gateway.auth.scim_service import ScimStore
from src.gateway.efficiency_analyzer import EfficiencyAnalyzer
from src.gateway.export_jobs import ExportJobService
from src.gateway.middleware.admin_rbac import AdminRBACMiddleware
from src.gateway.middleware.auth import AuthMiddleware
from src.gateway.middleware.tenant_authorization import TenantAuthorizationMiddleware
from src.gateway.multi_region.health_monitor import SpokeHealthMonitor
from src.gateway.multi_region.region_config import (
    HubConfig,
    apply_persisted_topology,
)
from src.gateway.multi_region.region_router import RegionRouter
from src.gateway.multi_region.spoke_loader import load_hub_config
from src.gateway.quota_enforcer import QuotaEnforcer
from src.gateway.middleware.security import (
    ControlPlaneHTTPMiddleware,
    SecurityMiddleware,
)
from src.gateway.observability.otlp_exporter import OTLPSpanExporter
from src.gateway.observability.trace_forwarder import TraceForwarder
from src.gateway.security.audit_trail import AuditEventType, AuditTrail
from src.gateway.security.event_dispatcher import (
    DestinationType,
    EventDestination,
    EventDispatcher,
)
from src.gateway.security.injection_detector import PromptInjectionDetector
from src.gateway.security.pii_ner import build_entity_detector
from src.gateway.security.pii_redactor import PIIRedactor
from src.gateway.cache_manager import CacheManager
from src.gateway.embeddings import build_embedder
from src.gateway.semantic_cache import (
    DEFAULT_SIMILARITY_THRESHOLD,
    SemanticCache,
)
from src.gateway.chat.client_agent import ClientAgent
from src.gateway.chat.routes import ChatAPI, create_chat_routes
from src.gateway.chat.openai_routes import OpenAICompatAPI, create_openai_routes
from src.gateway.config import AppConfig
from src.gateway.config_sync import ConfigSyncService
from src.gateway.config_loader import (
    DemoSeedData,
    load_app_config,
    load_catalog_config,
    load_demo_seed_config,
    load_ensemble_config,
    load_pricing_config,
)
from src.gateway.control_plane_routes import control_route_inventory
from src.gateway.cost_tracker import CostTracker
from src.gateway.feedback_tracker import FeedbackTracker
from src.gateway.guardrail_engine import GuardrailEngine
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.host_assemblies import WorkerAssembly, build_worker
from src.gateway.model_leaderboard import ModelLeaderboard
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import PolicyNode, Project, RateLimitConfig, UsageRecord
from src.gateway.multi_provider_factory import MultiProviderFactory
from src.gateway.persistence import (
    DynamoPersistence,
    PersistenceConflictError,
)
from src.gateway.provider_config import ProviderConfig
from src.gateway.provider_loader import load_provider_routes
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.query.admission import (
    QueryAdmissionController,
    QueryAdmissionLimits,
)
from src.gateway.query.athena import AthenaExecutor, AthenaQueryLimits
from src.gateway.query.models import AthenaRoleBindings
from src.gateway.query.repository import (
    DatasourceRepository,
    DynamoDatasourceRepository,
)
from src.gateway.query.reconciliation import (
    QueryLifecycleReconciler,
    QueryReconciliationWorker,
)
from src.gateway.query.routes import QueryAPI, create_query_routes
from src.gateway.query.service import QueryService
from src.gateway.request_validator import RequestValidator
from src.gateway.router import Router
from src.gateway.routing_runtime import RoutingRuntime
from src.gateway.semantic_efficiency import SemanticEfficiencyEngine
from src.gateway.smart_routing import SmartRoutingStrategy
from src.gateway.task_classifier import TaskClassifier

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GatewayComponents container
# ---------------------------------------------------------------------------


@dataclass
class GatewayComponents:
    """All constructed gateway components returned by the bootstrap."""

    cost_tracker: CostTracker
    health_tracker: ProviderHealthTracker
    registry: ModelRegistry
    router: Router
    rate_limiter: SlidingWindowRateLimiter
    guardrail_engine: GuardrailEngine
    cache_manager: CacheManager
    multi_factory: MultiProviderFactory
    routing_runtime: RoutingRuntime
    request_validator: RequestValidator
    gateway_agent: GatewayAgent
    projects: dict[str, Project]
    user_configs: dict[str, dict]
    policies: list[dict]
    persistence: DynamoPersistence
    catalog: dict
    api_key_service: APIKeyService | None = None
    oidc_service: OIDCService | None = None
    browser_session_service: BrowserSessionService | None = None
    principal_resolver: PrincipalResolver | None = None
    project_resolver: ProjectResolver | None = None
    scim_store: ScimStore | None = None
    saml_service: SamlService | None = None
    policy_resolver: PolicyHierarchyResolver | None = None
    quota_enforcer: QuotaEnforcer | None = None
    pii_redactor: PIIRedactor | None = None
    injection_detector: PromptInjectionDetector | None = None
    audit_trail: AuditTrail | None = None
    event_dispatcher: EventDispatcher | None = None
    region_router: RegionRouter | None = None
    health_monitor: SpokeHealthMonitor | None = None
    efficiency_analyzer: EfficiencyAnalyzer | None = None
    semantic_engine: SemanticEfficiencyEngine | None = None
    # Always present, but ``enabled`` is False unless AXON_SEMANTIC_CACHE=true
    # produced an embedder. Non-optional so the admin surface can report stats
    # and "unavailable" from the same object instead of branching on None.
    semantic_cache: SemanticCache = field(default_factory=SemanticCache)
    # Providers whose credentials loaded. load_provider_configs drops the rest,
    # so this is also the set the readiness checklist can distinguish
    # "configured" from "in models.yaml but unusable".
    provider_configs: dict[str, ProviderConfig] = field(default_factory=dict)
    datasource_repository: DatasourceRepository | None = None
    athena_role_bindings: AthenaRoleBindings = field(
        default_factory=AthenaRoleBindings
    )
    query_service: QueryService | None = None
    query_reconciliation_worker: QueryReconciliationWorker | None = None


@dataclass(frozen=True)
class ControlAPIComponents:
    """Services required by the administration API, excluding inference."""

    cost_tracker: CostTracker
    health_tracker: ProviderHealthTracker
    registry: ModelRegistry
    projects: dict[str, Project]
    user_configs: dict[str, dict]
    policies: list[dict]
    persistence: DynamoPersistence
    catalog: dict
    api_key_service: APIKeyService | None
    oidc_service: OIDCService | None
    browser_session_service: BrowserSessionService | None
    principal_resolver: PrincipalResolver | None
    project_resolver: ProjectResolver | None
    scim_store: ScimStore | None
    saml_service: SamlService | None
    policy_resolver: PolicyHierarchyResolver | None
    quota_enforcer: QuotaEnforcer | None
    audit_trail: AuditTrail | None
    event_dispatcher: EventDispatcher | None
    region_router: RegionRouter | None
    health_monitor: SpokeHealthMonitor | None
    efficiency_analyzer: EfficiencyAnalyzer | None
    semantic_engine: SemanticEfficiencyEngine | None
    semantic_cache: SemanticCache
    provider_configs: dict[str, ProviderConfig]
    datasource_repository: DatasourceRepository | None
    athena_role_bindings: AthenaRoleBindings
    export_jobs: ExportJobService | None = None

    @classmethod
    def from_gateway(
        cls,
        components: GatewayComponents,
    ) -> ControlAPIComponents:
        """Project the legacy combined bundle onto the control contract."""

        return cls(
            cost_tracker=components.cost_tracker,
            health_tracker=components.health_tracker,
            registry=components.registry,
            projects=components.projects,
            user_configs=components.user_configs,
            policies=components.policies,
            persistence=components.persistence,
            catalog=components.catalog,
            api_key_service=components.api_key_service,
            oidc_service=components.oidc_service,
            browser_session_service=components.browser_session_service,
            principal_resolver=components.principal_resolver,
            project_resolver=components.project_resolver,
            scim_store=components.scim_store,
            saml_service=components.saml_service,
            policy_resolver=components.policy_resolver,
            quota_enforcer=components.quota_enforcer,
            audit_trail=components.audit_trail,
            event_dispatcher=components.event_dispatcher,
            region_router=components.region_router,
            health_monitor=components.health_monitor,
            efficiency_analyzer=components.efficiency_analyzer,
            semantic_engine=components.semantic_engine,
            semantic_cache=components.semantic_cache,
            provider_configs=components.provider_configs,
            datasource_repository=components.datasource_repository,
            athena_role_bindings=components.athena_role_bindings,
            export_jobs=None,
        )


@dataclass(frozen=True)
class DataPlaneComponents:
    """Services used only by inference and query execution routes."""

    gateway_agent: GatewayAgent
    query_service: QueryService | None = None


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------


def _load_runtime_model_registry(
    config_path: str,
    persistence: DynamoPersistence,
) -> ModelRegistry:
    """Load file defaults, then replace them with durable state when present."""
    registry = ModelRegistry()
    registry.load(config_path)
    if not persistence.enabled:
        return registry

    # The checked-in file is the bootstrap/fallback registry. Once an
    # administrator commits a durable document it is authoritative, including
    # deletions, so replace rather than merge. A failed read is allowed to abort
    # startup; treating an outage as "no override" could silently reactivate a
    # route that was removed in production.
    snapshot = asyncio.run(
        persistence.load_model_registry_snapshot()
    )
    if snapshot is None:
        signing_mode = getattr(
            persistence,
            "routing_config_signing_mode",
            "disabled",
        )
        if signing_mode == "verify":
            raise RuntimeError(
                "signed routing configuration is not initialized"
            )
        if signing_mode == "sign-verify":
            try:
                snapshot = asyncio.run(
                    persistence.save_model_registry(
                        registry.to_config(),
                        expected_revision=0,
                    )
                )
            except PersistenceConflictError:
                snapshot = asyncio.run(
                    persistence.load_model_registry_snapshot()
                )
                if snapshot is None:
                    raise RuntimeError(
                        "signed routing configuration initialization "
                        "lost its concurrent write"
                    ) from None
    if snapshot is not None:
        snapshot.apply(registry)
    return registry


def build_gateway_components(
    app_config: AppConfig | None = None,
) -> GatewayComponents:
    """Construct all standalone gateway components from configuration."""

    return cast(
        GatewayComponents,
        _build_process_components(
            app_config,
            include_data_plane=True,
        ),
    )


def build_control_components(
    app_config: AppConfig | None = None,
) -> ControlAPIComponents:
    """Construct administration services without inference or workers."""

    return cast(
        ControlAPIComponents,
        _build_process_components(
            app_config,
            include_data_plane=False,
        ),
    )


def _build_process_components(
    app_config: AppConfig | None,
    *,
    include_data_plane: bool,
) -> GatewayComponents | ControlAPIComponents:
    """Construct the selected process assembly from shared state services.

    If *app_config* is ``None``, one is loaded from environment variables.
    """
    if app_config is None:
        app_config = load_app_config()

    # --- Pricing ---
    pricing = load_pricing_config(app_config.pricing_config_path)

    # --- Persistence ---
    persistence = DynamoPersistence(
        region=app_config.aws_region,
        routing_config_signing_mode=(
            app_config.routing_config_signing_mode
        ),
        routing_config_signing_key_arn=(
            app_config.routing_config_signing_key_arn
        ),
    )
    if persistence.enabled and include_data_plane:
        asyncio.run(
            persistence.create_table_if_not_exists()
        )

    # --- Auth services ---
    api_key_service = APIKeyService(persistence=persistence)
    oidc_claim_mappings = OIDCConfig().claim_mappings
    oidc_claim_mappings.update(
        {
            "tenant_id": app_config.oidc_tenant_claim,
            "project_id": app_config.oidc_project_claim,
        }
    )
    oidc_config = OIDCConfig(
        issuer=app_config.oidc_issuer,
        audience=app_config.oidc_audience,
        alb_region=app_config.aws_region,
        alb_signer_arn=app_config.alb_signer_arn,
        alb_client_id=app_config.alb_client_id,
        alb_issuer=app_config.alb_issuer,
        claim_mappings=oidc_claim_mappings,
    )
    oidc_service = OIDCService(config=oidc_config)
    browser_session_service = None
    if app_config.browser_auth_enabled:
        if not persistence.enabled:
            raise RuntimeError(
                "CloudFront browser authentication requires enabled "
                "DynamoDB persistence"
            )
        browser_session_service = BrowserSessionService(
            config=BrowserSessionConfig(
                hosted_ui_url=app_config.cognito_hosted_ui_url,
                client_id=app_config.browser_auth_client_id,
                callback_url=app_config.browser_auth_callback_url,
                signed_out_url=app_config.browser_auth_signed_out_url,
                authorization_endpoint=(
                    app_config.browser_auth_authorization_endpoint
                ),
                token_endpoint=app_config.browser_auth_token_endpoint,
                logout_endpoint=app_config.browser_auth_logout_endpoint,
                session_max_seconds=(
                    app_config.browser_session_max_seconds
                ),
                flow_ttl_seconds=(
                    app_config.browser_auth_flow_ttl_seconds
                ),
            ),
            store=DynamoBrowserSessionStore(persistence),
            oidc_service=oidc_service,
        )
    principal_resolver = None
    project_resolver = None
    if app_config.canonical_identity_required:
        if app_config.auth_mode != "ENFORCE":
            raise RuntimeError(
                "canonical identity requires AXON_AUTH_MODE=ENFORCE"
            )
        if not persistence.enabled:
            raise RuntimeError(
                "AXON_REQUIRE_CANONICAL_IDENTITY=true requires DynamoDB persistence"
            )
        principal_resolver = CanonicalPrincipalResolver(
            DynamoPrincipalRepository(persistence)
        )
        project_resolver = DynamoProjectRepository(persistence)

    # --- Enterprise identity: SCIM + ALB/Cognito-managed SAML federation ---
    scim_store = ScimStore(
        persistence=persistence,
        canonical_identity_required=(
            app_config.canonical_identity_required
        ),
    )
    if persistence.enabled:
        asyncio.run(scim_store.initialize())
    saml_service = SamlService(
        config=load_saml_config(
            deployment_profile=app_config.deployment_profile,
            auth_mode=app_config.auth_mode,
            canonical_identity_required=(
                app_config.canonical_identity_required
            ),
            control_plane_only=app_config.control_plane_only,
            aws_region=app_config.aws_region,
            oidc_issuer=app_config.oidc_issuer,
            oidc_audience=app_config.oidc_audience,
            alb_signer_arn=app_config.alb_signer_arn,
            alb_client_id=app_config.alb_client_id,
            alb_issuer=app_config.alb_issuer,
            endpoint_mode=app_config.control_plane_endpoint_mode,
            browser_auth_client_id=app_config.browser_auth_client_id,
        )
    )

    policy_resolver = PolicyHierarchyResolver(persistence=persistence)
    if persistence.enabled:
        asyncio.run(policy_resolver.load_nodes())

    # --- Quota enforcement ---
    # Persistence-backed so budget enforcement is fleet-wide: this enforcer's
    # check_budget is what blocks requests, and a per-process counter made a
    # single budget limit apply once per instance.
    quota_enforcer = QuotaEnforcer(persistence=persistence)

    # --- Multi-region ---
    # Load a real hub/spoke topology from spokes.yaml when present; otherwise a
    # single-region default (single-region deploys need no config file).
    hub_config = load_hub_config(
        app_config.spokes_config_path, default_region=app_config.aws_region)
    region_router = RegionRouter(hub_config=hub_config)
    health_monitor = SpokeHealthMonitor(hub_config=hub_config)

    # --- Security services ---
    # The entity detector is constructed unconditionally but used only when a
    # policy sets pii_ner_enabled: building it costs nothing (the boto3 client is
    # lazy) and wiring it here keeps the decision in policy rather than in
    # startup config, so one BU can enable name detection without a redeploy.
    pii_redactor = (
        PIIRedactor(
            entity_detector=build_entity_detector(
                region=app_config.aws_region,
            )
        )
        if include_data_plane
        else None
    )
    injection_detector = (
        PromptInjectionDetector()
        if include_data_plane
        else None
    )
    audit_trail = AuditTrail(persistence=persistence)
    # Reload the hash-chain head so audit continuity survives restarts. Loop-safe:
    # runs now when standalone, defers to the running loop when embedded (Ostiari)
    # — never calls asyncio.run inside an active loop.
    audit_trail.initialize_sync()
    athena_role_bindings = AthenaRoleBindings.from_json(
        app_config.athena_query_bindings
    )
    datasource_repository: DatasourceRepository | None = None
    query_service: QueryService | None = None
    query_reconciliation_worker: QueryReconciliationWorker | None = None
    if app_config.athena_query_enabled:
        datasource_repository = DynamoDatasourceRepository(
            persistence,
            max_datasources_per_tenant=(
                app_config.athena_query_max_datasources_per_tenant
            ),
        )
        if include_data_plane:
            query_admission = QueryAdmissionController(
                persistence,
                limits=QueryAdmissionLimits(
                    project_rpm=app_config.athena_query_project_rpm,
                    principal_rpm=app_config.athena_query_principal_rpm,
                    project_concurrency=(
                        app_config.athena_query_project_concurrency
                    ),
                    principal_concurrency=(
                        app_config.athena_query_principal_concurrency
                    ),
                    project_scan_bytes_per_minute=(
                        app_config
                        .athena_query_project_scan_bytes_per_minute
                    ),
                    principal_scan_bytes_per_minute=(
                        app_config
                        .athena_query_principal_scan_bytes_per_minute
                    ),
                    lease_seconds=max(
                        30,
                        math.ceil(
                            app_config.athena_query_timeout_seconds
                        )
                        + 30,
                    ),
                ),
                max_scan_bytes_per_query=(
                    app_config.athena_query_max_bytes_scanned
                ),
            )
            athena_executor = AthenaExecutor(
                limits=AthenaQueryLimits(
                    timeout_seconds=(
                        app_config.athena_query_timeout_seconds
                    ),
                    max_rows=app_config.athena_query_max_rows,
                    max_result_bytes=(
                        app_config.athena_query_max_result_bytes
                    ),
                    max_bytes_scanned=(
                        app_config.athena_query_max_bytes_scanned
                    ),
                    poll_interval_seconds=(
                        app_config.athena_query_poll_interval_seconds
                    ),
                )
            )
            query_service = QueryService(
                repository=datasource_repository,
                bindings=athena_role_bindings,
                executor=athena_executor,
                audit_trail=audit_trail,
                require_durable_audit=True,
                admission=query_admission,
                require_durable_admission=True,
            )
            query_reconciliation_worker = QueryReconciliationWorker(
                QueryLifecycleReconciler(
                    store=persistence,
                    repository=datasource_repository,
                    bindings=athena_role_bindings,
                    executor=athena_executor,
                    audit_trail=audit_trail,
                    claim_seconds=300,
                    page_size=5,
                ),
                interval_seconds=30,
                max_pages=10,
            )
    event_dispatcher = EventDispatcher()
    # Host-specific environment discovery ends here. Embedded hosts inject
    # telemetry sinks directly rather than mutating process-global callbacks.
    trace_forwarder = None
    if include_data_plane:
        try:
            trace_timeout = float(
                os.environ.get("OSTIARI_TRACES_TIMEOUT", "3.0")
            )
        except ValueError:
            logger.warning(
                "OSTIARI_TRACES_TIMEOUT is invalid; using 3 seconds"
            )
            trace_timeout = 3.0
        trace_forwarder = TraceForwarder(
            url=os.environ.get("OSTIARI_TRACES_URL", "").strip() or None,
            gateway_id=(
                os.environ.get("OSTIARI_GATEWAY_ID", "axonllm").strip()
                or "axonllm"
            ),
            ingest_key=(
                os.environ.get("OSTIARI_INGEST_KEY", "").strip()
                or None
            ),
            timeout_seconds=trace_timeout,
        )
    # Native OTLP span export for the STANDALONE deploy (opt-in via
    # OTEL_EXPORTER_OTLP_ENDPOINT). Suppressed by the agent when embedded in
    # Ostiari — Ostiari emits the governance span there. No-op if OTEL SDK absent.
    otlp_exporter = (
        OTLPSpanExporter()
        if include_data_plane
        else None
    )

    # --- Budget threshold alerting ---
    async def _budget_alert(
        project_id,
        threshold_pct,
        current_spend,
        budget_limit,
        tenant_id,
        billing_epoch,
    ):
        from src.gateway.security.event_dispatcher import SecurityEvent
        from src.gateway.security.audit_trail import LEGACY_TENANT_ID
        from datetime import timezone
        event_tenant_id = tenant_id or LEGACY_TENANT_ID
        event = SecurityEvent(
            event_id=(
                f"budget_{event_tenant_id}_{project_id}_"
                f"{billing_epoch}_{int(threshold_pct * 100)}"
            ),
            event_type="budget_threshold",
            timestamp=datetime.now(timezone.utc).isoformat(),
            severity="warning" if threshold_pct < 1.0 else "critical",
            tenant_id=event_tenant_id,
            project_id=project_id,
            data={
                "threshold_pct": threshold_pct * 100,
                "current_spend": current_spend,
                "budget_limit": budget_limit,
                "billing_epoch": billing_epoch,
            },
        )
        await event_dispatcher.dispatch(event)

    quota_enforcer.on_budget_alert(_budget_alert)

    # --- Core components ---
    cost_tracker = CostTracker(pricing_config=pricing, persistence=persistence)
    health_tracker = ProviderHealthTracker()

    registry = _load_runtime_model_registry(
        app_config.models_config_path,
        persistence,
    )
    all_model_names = list(registry.models.keys())

    # --- Demo seed data ---
    projects: dict[str, Project] = {}
    user_configs: dict[str, dict] = {}
    policies: list[dict] = []

    if app_config.load_demo_data:
        seed = load_demo_seed_config(app_config.demo_seed_config_path)
        projects, user_configs, policies = _apply_seed_data(
            seed, cost_tracker, health_tracker, all_model_names,
            quota_enforcer, policy_resolver,
            audit_trail, api_key_service, event_dispatcher,
        )

    # --- DynamoDB persisted state (merges on top of seed data) ---
    loaded_feedback: list = []
    if persistence.enabled:
        (
            loaded_projects, loaded_user_configs, loaded_records, loaded_feedback,
            loaded_policies, loaded_destinations, loaded_topology,
        ) = asyncio.run(_load_persisted_state(persistence))
        projects.update(loaded_projects)
        user_configs.update(loaded_user_configs)
        # Loading the dicts is not the same as arming enforcement. Budget limits
        # live in cost_tracker._budgets / ._user_budgets and in the policy
        # resolver's nodes, none of which a dict update touches — so a limit set
        # through the admin API was written to DynamoDB, read back on restart,
        # displayed correctly on the dashboard, and enforced by nothing.
        # _apply_seed_data does this registration for seeded entities; persisted
        # ones need the same treatment or they are decorative.
        _register_persisted_budgets(
            cost_tracker, policy_resolver, loaded_projects, loaded_user_configs)
        # Merge by name, matching POST /admin/policies' update-by-name identity: a
        # persisted policy replaces the seeded one it shares a name with rather
        # than being evaluated alongside it, which for a permit/forbid pair would
        # otherwise silently resolve to DENY.
        by_name = {p["name"]: p for p in policies}
        by_name.update({p["name"]: p for p in loaded_policies})
        policies = list(by_name.values())
        _apply_persisted_infrastructure(
            event_dispatcher, hub_config, loaded_destinations, loaded_topology)
        # Rehydrate via load_records so the running spend counters (which back
        # budget checks) are seeded from history, not just the record list.
        cost_tracker.load_records(loaded_records)
        # Then overwrite those sums with the shared fleet-wide totals. Summing
        # local records is only right for a single instance; this instance's view
        # of history is whatever load_usage_records returned, while the counter is
        # what every instance has actually charged. Ordered after load_records
        # because it replaces rather than adds.
        #
        # Both trackers are seeded: cost_tracker backs the reported budget status,
        # quota_enforcer backs the block. Without the latter, the first request
        # after a deploy is admitted against a budget the fleet already exhausted.
        spend_projects_by_tenant: dict[str | None, set[str]] = {}
        spend_users_by_tenant: dict[str | None, set[str]] = {}
        for project in projects.values():
            spend_projects_by_tenant.setdefault(
                project.tenant_id,
                set(),
            ).add(project.project_id)
        for record in loaded_records:
            if record.project_id:
                spend_projects_by_tenant.setdefault(
                    record.tenant_id,
                    set(),
                ).add(record.project_id)
            if record.user_id:
                spend_users_by_tenant.setdefault(
                    record.tenant_id,
                    set(),
                ).add(record.user_id)
        for tenant_id, project_ids in spend_projects_by_tenant.items():
            asyncio.run(
                cost_tracker.adopt_fleet_spend(
                    project_ids,
                    spend_users_by_tenant.get(tenant_id, set()),
                    tenant_id=tenant_id,
                )
            )
            asyncio.run(
                quota_enforcer.adopt_fleet_spend(
                    project_ids,
                    tenant_id=tenant_id,
                )
            )

    # --- Smart routing components ---
    leaderboard = ModelLeaderboard()
    leaderboard.load("config/leaderboard.yaml", valid_models=set(all_model_names))

    task_classifier = TaskClassifier()
    feedback_tracker = FeedbackTracker(persistence=persistence)
    if loaded_feedback:
        feedback_tracker._records.extend(loaded_feedback)

    provider_configs: dict[str, ProviderConfig] = {}
    semantic_cache = SemanticCache()
    if include_data_plane:
        smart_strategy = SmartRoutingStrategy(
            classifier=task_classifier,
            leaderboard=leaderboard,
            model_registry=registry,
            health_tracker=health_tracker,
            cost_tracker=cost_tracker,
            feedback_tracker=feedback_tracker,
            confidence_threshold=leaderboard.config.get(
                "confidence_threshold",
                0.3,
            ),
            cost_quality_tradeoff=leaderboard.config.get(
                "cost_quality_tradeoff",
                0.3,
            ),
            default_model=leaderboard.config.get(
                "default_model",
                "claude-sonnet",
            ),
            # The same table CostTracker bills from. models.yaml carries no
            # inline pricing, so without this the cost half of
            # cost_quality_tradeoff has nothing to read.
            pricing_config=pricing,
        )

        # --- Routing / rate limiting / guardrails / cache ---
        ensemble_config = load_ensemble_config(
            app_config.ensemble_config_path
        )
        router = Router(
            model_registry=registry,
            health_tracker=health_tracker,
            smart_strategy=smart_strategy,
            ensemble_config=ensemble_config,
            cost_tracker=cost_tracker,
            available_providers=None,
            require_priced_mappings=(
                app_config.deployment_profile == "production"
            ),
        )
        rate_limiter = SlidingWindowRateLimiter(
            config=RateLimitConfig(),
            persistence=persistence,
        )
        guardrail_engine = GuardrailEngine()
        cache_manager = CacheManager()

        # Constructing the embedder can resolve AWS credentials, so only the
        # inference host is allowed to own it.
        semantic_embedder = None
        if app_config.semantic_cache_enabled:
            semantic_embedder = build_embedder(
                region=app_config.semantic_cache_region,
                model_id=app_config.semantic_cache_model or None,
            )
            if semantic_embedder is None:
                logger.warning(
                    "AXON_SEMANTIC_CACHE=true but no embedder could be built "
                    "— semantic caching is off; exact-match caching is "
                    "unaffected"
                )
        semantic_cache = SemanticCache(
            embedder=semantic_embedder,
            similarity_threshold=(
                app_config.semantic_cache_threshold
                if app_config.semantic_cache_threshold is not None
                else DEFAULT_SIMILARITY_THRESHOLD
            ),
        )

        # --- Multi-provider factory ---
        provider_routes = load_provider_routes(
            app_config.providers_config_path
        )
        for route in provider_routes:
            provider_configs.setdefault(
                route.provider,
                route.to_provider_config(),
            )
        multi_factory = MultiProviderFactory(
            provider_configs=provider_configs,
            bedrock_region=app_config.bedrock_region,
            enabled_providers=app_config.enabled_providers,
            provider_routes=provider_routes,
        )
        router.available_providers = multi_factory.available_providers

        # --- Request validator ---
        request_validator = RequestValidator(model_registry=registry)
        routing_runtime = RoutingRuntime(
            router=router,
            provider_factory=multi_factory,
            model_registry=registry,
            validator=request_validator,
        )

        # --- Gateway agent ---
        gateway_agent = GatewayAgent(
            router=router,
            rate_limiter=rate_limiter,
            guardrail_engine=guardrail_engine,
            cache_manager=cache_manager,
            cost_tracker=cost_tracker,
            projects=projects,
            provider_fn_factory=multi_factory,
            user_configs=user_configs,
            request_validator=request_validator,
            smart_routing_enabled=True,
            quota_enforcer=quota_enforcer,
            policy_resolver=policy_resolver,
            pii_redactor=pii_redactor,
            injection_detector=injection_detector,
            audit_trail=audit_trail,
            event_dispatcher=event_dispatcher,
            region_router=region_router,
            trace_forwarder=trace_forwarder,
            otlp_exporter=otlp_exporter,
            semantic_cache=semantic_cache,
            persistence=persistence,
            routing_runtime=routing_runtime,
        )

    # --- Efficiency analysis ---
    efficiency_analyzer = EfficiencyAnalyzer(cost_tracker=cost_tracker)
    semantic_engine = SemanticEfficiencyEngine(
        task_classifier=task_classifier,
        cost_tracker=cost_tracker,
        model_registry=registry,
        leaderboard=leaderboard,
    )

    # --- Catalog ---
    catalog = load_catalog_config(
        app_config.catalog_config_path, fallback=PROVIDER_MODEL_CATALOG,
    )

    if not include_data_plane:
        return ControlAPIComponents(
            cost_tracker=cost_tracker,
            health_tracker=health_tracker,
            registry=registry,
            projects=projects,
            user_configs=user_configs,
            policies=policies,
            persistence=persistence,
            catalog=catalog,
            api_key_service=api_key_service,
            oidc_service=oidc_service,
            browser_session_service=browser_session_service,
            principal_resolver=principal_resolver,
            project_resolver=project_resolver,
            scim_store=scim_store,
            saml_service=saml_service,
            policy_resolver=policy_resolver,
            quota_enforcer=quota_enforcer,
            audit_trail=audit_trail,
            event_dispatcher=event_dispatcher,
            region_router=region_router,
            health_monitor=health_monitor,
            efficiency_analyzer=efficiency_analyzer,
            semantic_engine=semantic_engine,
            semantic_cache=semantic_cache,
            provider_configs=provider_configs,
            datasource_repository=datasource_repository,
            athena_role_bindings=athena_role_bindings,
            export_jobs=None,
        )

    return GatewayComponents(
        cost_tracker=cost_tracker,
        health_tracker=health_tracker,
        registry=registry,
        router=router,
        rate_limiter=rate_limiter,
        guardrail_engine=guardrail_engine,
        cache_manager=cache_manager,
        multi_factory=multi_factory,
        routing_runtime=routing_runtime,
        request_validator=request_validator,
        gateway_agent=gateway_agent,
        projects=projects,
        user_configs=user_configs,
        policies=policies,
        persistence=persistence,
        catalog=catalog,
        api_key_service=api_key_service,
        oidc_service=oidc_service,
        browser_session_service=browser_session_service,
        principal_resolver=principal_resolver,
        project_resolver=project_resolver,
        scim_store=scim_store,
        saml_service=saml_service,
        policy_resolver=policy_resolver,
        quota_enforcer=quota_enforcer,
        pii_redactor=pii_redactor,
        injection_detector=injection_detector,
        audit_trail=audit_trail,
        event_dispatcher=event_dispatcher,
        region_router=region_router,
        health_monitor=health_monitor,
        efficiency_analyzer=efficiency_analyzer,
        semantic_engine=semantic_engine,
        semantic_cache=semantic_cache,
        provider_configs=provider_configs,
        datasource_repository=datasource_repository,
        athena_role_bindings=athena_role_bindings,
        query_service=query_service,
        query_reconciliation_worker=query_reconciliation_worker,
    )


# ---------------------------------------------------------------------------
# Starlette app builder (used by serve_dashboard.py)
# ---------------------------------------------------------------------------


async def _persistence_readiness(
    persistence: DynamoPersistence,
    timeout_seconds: float = 5.0,
) -> tuple[bool, dict[str, str]]:
    """Return a sanitized readiness result for the required state store."""
    if not persistence.enabled:
        return True, {"persistence": "disabled"}
    try:
        status = await asyncio.wait_for(
            persistence.health_status(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        return False, {"persistence": "timeout"}
    except Exception:
        logger.warning("Persistence readiness check failed", exc_info=True)
        return False, {"persistence": "unavailable"}
    if status.get("enabled") is True and status.get("reachable") is True:
        return True, {"persistence": "ready"}
    return False, {"persistence": "unavailable"}


def _build_http_app(
    app_config: AppConfig,
    control: ControlAPIComponents,
    *,
    data_plane: DataPlaneComponents | None,
    worker: WorkerAssembly | None,
) -> Starlette:
    """Assemble HTTP routes from already constructed process services."""

    # Built before the admin API and wired unchanged into both, so a policy
    # written through POST /admin/policies recompiles the evaluator the auth
    # middleware is already using. Always constructed, even for an empty policy
    # set: the previous `if comp.policies else None` meant a gateway that booted
    # with no policies had no evaluator to recompile, so the first policy an
    # operator added did nothing until a restart. An empty set governs no action,
    # so wiring it changes no decision.
    # Persistence is passed so the evaluator can adopt a policy another instance
    # wrote: without it, POST /admin/policies recompiled only the task that
    # served the write, and behind desired_count=2 the same request was governed
    # or ungoverned depending on the load balancer.
    cedar_service = CedarPolicyService(
        control.policies,
        persistence=control.persistence,
    )
    # Adopt the version those policies were loaded at, so the first request does
    # not re-scan to discover the set startup already has. Read here rather than
    # in _load_persisted_state because it must be the version *after* the load:
    # a bump in between leaves _known_version behind and self-corrects on the
    # next poll, whereas one ahead would skip a change.
    if control.persistence.enabled:
        cedar_service.note_local_version(
            asyncio.run(control.persistence.get_policy_version()))

    # Handed the *same* dicts the agent and the admin API hold, so a config write
    # on another task converges into the objects the request path actually reads.
    # The cost tracker and resolver come along because adopting the dicts is not
    # the same as arming enforcement — limits live in the tracker, not the dicts.
    config_sync = ConfigSyncService(
        projects=control.projects,
        user_configs=control.user_configs,
        cost_tracker=control.cost_tracker,
        persistence=control.persistence,
        model_registry=control.registry,
        policy_resolver=control.policy_resolver,
        region_config=control.region_router.config,
        health_monitor=control.health_monitor,
    )
    if control.persistence.enabled:
        config_sync.note_local_version(
            asyncio.run(control.persistence.get_config_version()))

    admin_api = AdminAPI(
        cost_tracker=control.cost_tracker,
        health_tracker=control.health_tracker,
        model_registry=control.registry,
        projects=control.projects,
        policies=control.policies,
        user_configs=control.user_configs,
        config_path=app_config.models_config_path,
        persistence=control.persistence,
        catalog=control.catalog,
        efficiency_analyzer=control.efficiency_analyzer,
        semantic_engine=control.semantic_engine,
        pricing_path=app_config.pricing_config_path,
        catalog_path=app_config.catalog_config_path,
        # For the production-readiness checklist: the settings this process booted
        # with, the providers whose credentials actually loaded, and the key
        # service, so the checklist can report the scopes and expiry of issued
        # keys rather than assume they are bounded.
        app_config=app_config,
        provider_configs=control.provider_configs,
        api_key_service=control.api_key_service,
        # The same instance the gateway agent holds, so /admin/semantic-cache
        # reports the live counters and DELETE clears the cache requests are
        # actually served from.
        semantic_cache=control.semantic_cache,
        policy_service=cedar_service,
        config_sync=config_sync,
        export_jobs=control.export_jobs,
    )

    # Key, policy, audit, webhook, region, and quota admin APIs
    key_api = KeyManagementAPI(
        api_key_service=control.api_key_service,
        mode=app_config.auth_mode,
        audit_trail=control.audit_trail,
    )
    policy_api = PolicyHierarchyAPI(resolver=control.policy_resolver)
    audit_api = AuditAPI(
        audit_trail=control.audit_trail,
        export_jobs=control.export_jobs,
    )
    webhook_api = WebhookAPI(
        dispatcher=control.event_dispatcher,
        persistence=control.persistence,
    )
    region_api = RegionAPI(
        router=control.region_router,
        monitor=control.health_monitor,
        persistence=control.persistence,
        config_sync=config_sync,
        topology_lock=config_sync.region_lock,
    )
    quota_api = QuotaAPI(
        quota_enforcer=control.quota_enforcer,
        policy_resolver=control.policy_resolver,
        cost_tracker=control.cost_tracker,
    )
    scim_api = ScimAPI(
        store=control.scim_store,
        canonical_identity_required=(
            app_config.canonical_identity_required
        ),
    )
    saml_api = SamlAPI(service=control.saml_service)
    if control.browser_session_service is not None:
        browser_auth_routes = create_browser_auth_routes(
            BrowserAuthAPI(control.browser_session_service)
        )
    else:
        async def disabled_browser_auth_config(
            _request: Request,
        ) -> JSONResponse:
            return JSONResponse(
                {"browser_auth": {"enabled": False}},
                headers={
                    "Cache-Control": "no-store",
                    "Pragma": "no-cache",
                },
            )

        browser_auth_routes = [
            Route(
                BROWSER_AUTH_CONFIG_PATH,
                disabled_browser_auth_config,
                methods=["GET"],
            )
        ]
    datasource_routes: list[Route] = []
    query_routes: list[Route] = []
    if app_config.athena_query_enabled:
        if (
            control.datasource_repository is None
        ):
            raise RuntimeError(
                "Athena query services were not initialized"
            )
        datasource_api = DatasourceAPI(
            repository=control.datasource_repository,
            bindings=control.athena_role_bindings,
            project_resolver=control.project_resolver,
            audit_trail=control.audit_trail,
            require_durable_audit=True,
        )
        datasource_routes = create_datasource_routes(datasource_api)
        if data_plane is not None:
            if data_plane.query_service is None:
                raise RuntimeError(
                    "Athena query execution was not initialized"
                )
            query_routes = create_query_routes(
                QueryAPI(data_plane.query_service)
            )

    async def health_check(request: Request) -> JSONResponse:
        return JSONResponse({"status": "healthy"})

    async def readiness_check(request: Request) -> JSONResponse:
        ready, dependencies = await _persistence_readiness(
            control.persistence,
        )
        dispatcher = control.event_dispatcher
        if dispatcher is None or not dispatcher.outbox_enabled:
            dependencies["security_event_outbox"] = "disabled"
        else:
            try:
                outbox_ready = await asyncio.wait_for(
                    dispatcher.check_readiness(),
                    timeout=5.0,
                )
            except TimeoutError:
                outbox_ready = False
                dependencies["security_event_outbox"] = "timeout"
            except Exception:
                logger.warning(
                    "Security event outbox readiness check failed",
                    exc_info=True,
                )
                outbox_ready = False
                dependencies["security_event_outbox"] = "unavailable"
            else:
                dependencies["security_event_outbox"] = (
                    "ready" if outbox_ready else "unavailable"
                )
            ready = ready and outbox_ready
        await config_sync.refresh_routing_if_stale()
        routing_status = config_sync.routing_config_status
        routing_dependency = (
            "degraded"
            if routing_status["status"] == "degraded"
            else "ready"
        )
        dependencies["routing_configuration"] = routing_dependency
        return JSONResponse(
            {
                "status": (
                    "degraded"
                    if ready and routing_dependency == "degraded"
                    else ("ready" if ready else "not_ready")
                ),
                "ready": ready,
                "dependencies": dependencies,
            },
            status_code=200 if ready else 503,
        )

    control_routes = (
        create_admin_routes(admin_api)
        + create_key_routes(key_api)
        + create_policy_hierarchy_routes(policy_api)
        + create_audit_routes(audit_api)
        + create_webhook_routes(webhook_api)
        + create_region_routes(region_api)
        + create_quota_routes(quota_api)
        + create_scim_routes(scim_api)
        + create_saml_routes(saml_api)
        + browser_auth_routes
        + datasource_routes
    )
    if data_plane is not None:
        default_project = next(iter(control.projects), "default")
        client_agent = ClientAgent(
            data_plane.gateway_agent,
            default_project_id=default_project,
            default_user_id="chat-user",
        )
        chat_api = ChatAPI(client_agent)
        openai_api = OpenAICompatAPI(client_agent)
        data_routes = (
            create_chat_routes(chat_api)
            + create_openai_routes(openai_api)
            + query_routes
        )
    else:
        data_routes = []
    health_routes = [
        Route("/health", health_check),
        Route("/ready", readiness_check),
    ]
    site_routes = create_site_routes(admin_api)
    control_route_inventory(
        health_routes + control_routes + site_routes
    )
    routes = (
        health_routes
        + control_routes
        + data_routes
        # Last: this one is a bare "/{path}" serving site/, and Starlette
        # matches in order, so anything after it would be unreachable.
        + site_routes
    )

    @contextlib.asynccontextmanager
    async def _lifespan(_app):
        if worker is None:
            yield
            return
        async with worker.lifespan():
            yield

    app = Starlette(routes=routes, lifespan=_lifespan)

    # Security middleware (lightweight marker for LLM endpoints)
    app.add_middleware(SecurityMiddleware)

    # Admin RBAC (runs after auth, checks role/scope on /admin/* paths)
    app.add_middleware(
        AdminRBACMiddleware,
        mode=app_config.auth_mode,
        audit_trail=control.audit_trail,
    )

    # Baseline tenant RBAC for the inference and model-list data plane. Added
    # before AuthMiddleware so Starlette wraps it inside auth and a canonical
    # principal is already attached when this executes.
    app.add_middleware(
        TenantAuthorizationMiddleware,
        project_resolver=control.project_resolver,
        require_tenant_project=app_config.canonical_identity_required,
    )

    # Auth middleware (outermost — runs first on every request)
    app.add_middleware(
        AuthMiddleware,
        oidc_service=control.oidc_service,
        api_key_service=control.api_key_service,
        policy_service=cedar_service,
        mode=app_config.auth_mode,
        # Refreshed here, before any handler reads the project or user config, so
        # /api/chat gets the same converged view the admin pages do.
        config_sync=config_sync,
        principal_resolver=control.principal_resolver,
        require_canonical_principal=app_config.canonical_identity_required,
        browser_session_service=control.browser_session_service,
    )

    # Outermost: bounds request bodies before authentication does work,
    # protects browser-cookie sessions from CSRF, and decorates even
    # auth-generated errors.
    app.add_middleware(
        ControlPlaneHTTPMiddleware,
        production=app_config.deployment_profile == "production",
    )

    return app


def build_control_api(
    app_config: AppConfig,
    components: ControlAPIComponents,
) -> Starlette:
    """Build control routes from injected services with no owned workers.

    The caller owns the supplied services. This function does not construct a
    provider factory, inference client, or background loop.
    """

    return _build_http_app(
        app_config,
        components,
        data_plane=None,
        worker=None,
    )


def build_starlette_app(app_config: AppConfig | None = None) -> Starlette:
    """Build the legacy combined standalone application."""

    if app_config is None:
        app_config = load_app_config()
    comp = build_gateway_components(app_config)

    async def _close_provider_factory() -> None:
        await comp.multi_factory.close()

    async def _close_trace_forwarder() -> None:
        forwarder = getattr(comp.gateway_agent, "_trace_forwarder", None)
        close = getattr(forwarder, "close", None)
        if callable(close):
            await close()

    query_reconciliation_worker = (
        comp.query_reconciliation_worker
        if app_config.athena_query_enabled
        and not app_config.control_plane_only
        else None
    )
    worker = build_worker(
        event_worker=comp.event_dispatcher,
        reconciliation_monitor=comp.health_monitor,
        periodic_workers=(
            (query_reconciliation_worker,)
            if query_reconciliation_worker is not None
            else ()
        ),
        close_hooks=(
            _close_provider_factory,
            _close_trace_forwarder,
        ),
    )
    return _build_http_app(
        app_config,
        ControlAPIComponents.from_gateway(comp),
        data_plane=(
            None
            if app_config.control_plane_only
            else DataPlaneComponents(
                gateway_agent=comp.gateway_agent,
                query_service=comp.query_service,
            )
        ),
        worker=worker,
    )


# ---------------------------------------------------------------------------
# Agent-only builder (used by agentcore_agent.py)
# ---------------------------------------------------------------------------


def build_gateway_agent(app_config: AppConfig | None = None) -> GatewayAgent:
    """Build and return just the GatewayAgent (no HTTP routes)."""
    comp = build_gateway_components(app_config)
    return comp.gateway_agent


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_seed_hierarchy(policy_resolver: PolicyHierarchyResolver) -> None:
    """Re-check the seeded tree against the resolver's own child<=parent rule.

    The nodes were assigned directly to bypass parent-before-child ordering, so
    the validation set_node() would have done is applied here instead — over the
    complete tree, where every parent is present.

    Raises rather than warns. A seed whose child exceeds its parent describes a
    hierarchy the resolver will not honour: the merge silently clamps to the
    parent's value, so the dashboard would show a limit that is not the limit in
    force. Failing at startup keeps the demo honest about its own numbers.
    """
    violations: list[str] = []
    for node in policy_resolver._nodes.values():
        found = asyncio.run(policy_resolver.validate_node_limits(node))
        violations.extend(f"{node.node_id} -> {v}" for v in found)
    # Every node also has to reach a root, or its limits come from a chain the
    # resolver cannot walk: get_ancestry stops at the first missing parent and
    # returns a partial path, so the node quietly resolves to less than the seed
    # describes. A typo in a parent_id is the likely cause and reads as a
    # mysteriously permissive project otherwise.
    for node in policy_resolver._nodes.values():
        if node.parent_id is not None and node.parent_id not in policy_resolver._nodes:
            violations.append(f"{node.node_id} -> unknown parent {node.parent_id!r}")
    if violations:
        raise ValueError(
            "Seeded policy hierarchy is invalid: " + "; ".join(sorted(violations))
        )


def _apply_seed_data(
    seed: DemoSeedData,
    cost_tracker: CostTracker,
    health_tracker: ProviderHealthTracker,
    all_model_names: list[str],
    quota_enforcer: QuotaEnforcer,
    policy_resolver: PolicyHierarchyResolver,
    audit_trail: AuditTrail,
    api_key_service: APIKeyService,
    event_dispatcher: EventDispatcher,
) -> tuple[dict[str, Project], dict[str, dict], list[dict]]:
    """Apply demo seed data to components. Returns (projects, user_configs, policies)."""
    projects: dict[str, Project] = {}
    for p in seed.projects:
        proj = Project(
            project_id=p["project_id"],
            name=p["name"],
            budget_limit=p.get("budget_limit"),
            alert_threshold=p.get("alert_threshold"),
            cache_enabled=p.get("cache_enabled", False),
            # Was omitted here, so a seed setting cache_ttl_seconds got the
            # 300s default silently. Harmless while nothing wrote to the cache;
            # not harmless now that something does.
            cache_ttl_seconds=p.get("cache_ttl_seconds", 300),
            semantic_cache_enabled=p.get("semantic_cache_enabled", False),
            semantic_cache_threshold=p.get("semantic_cache_threshold"),
            prompt_caching_enabled=p.get("prompt_caching_enabled", False),
            members=p.get("members", []),
            allowed_models=p.get("allowed_models"),
        )
        projects[proj.project_id] = proj
        if proj.budget_limit is not None or proj.alert_threshold is not None:
            cost_tracker.register_project(
                proj.project_id,
                budget_limit=proj.budget_limit,
                alert_threshold=proj.alert_threshold,
            )
        # Fallback policy node, so the quota endpoint can still resolve the
        # project's budget_limit (the resolver reads limits from PolicyNodes,
        # not projects). Parentless and project-level: a flat node carrying one
        # limit. The policy_nodes section below overwrites it where the seed
        # describes a real tree, which is why this runs first.
        if proj.budget_limit is not None:
            policy_resolver._nodes[proj.project_id] = PolicyNode(
                node_id=proj.project_id,
                node_type="project",
                parent_id=None,
                display_name=proj.name,
                limits={"budget_limit": proj.budget_limit},
            )

    # Policy hierarchy. Assigned directly rather than through set_node() because
    # a tree arrives in file order, not parent-first: set_node validates against
    # a parent that may be several lines further down and would reject the child
    # for exceeding limits it cannot see yet. The invariant is not skipped —
    # _validate_seed_hierarchy re-checks the whole tree once it is all present,
    # which is strictly stronger than validating each node on arrival.
    for pn in seed.policy_nodes:
        policy_resolver._nodes[pn["node_id"]] = PolicyNode(
            node_id=pn["node_id"],
            node_type=pn["node_type"],
            parent_id=pn.get("parent_id"),
            display_name=pn.get("display_name", pn["node_id"]),
            limits=pn.get("limits") or {},
        )
    if seed.policy_nodes:
        _validate_seed_hierarchy(policy_resolver)

    # User budgets
    user_configs: dict[str, dict] = {}
    for ub in seed.user_budgets:
        cost_tracker.register_user(
            ub["user_id"],
            budget_limit=ub.get("budget_limit"),
            alert_threshold=ub.get("alert_threshold"),
        )

    # Usage seeds
    async def _seed_usage():
        now = datetime.now(timezone.utc)
        for i, s in enumerate(seed.usage_seeds):
            pt = s.get("prompt_tokens", 0)
            ct = s.get("completion_tokens", 0)
            await cost_tracker.record_usage(UsageRecord(
                # Indexed, because project+user+provider is not unique: several
                # seeded calls share all three, and identical request ids read as
                # one request retried rather than a populated trace log.
                request_id=f"req-{i:04d}-{s['project_id']}-{s['user_id']}",
                project_id=s["project_id"],
                user_id=s["user_id"],
                provider=s["provider"],
                model=s["model"],
                prompt_tokens=pt,
                completion_tokens=ct,
                total_tokens=pt + ct,
                cost=s.get("cost", 0.0),
                # Spread over the window the seed asks for. Stamping every record
                # at import time puts the whole trace log on one clock minute,
                # which is not what a live gateway looks like.
                timestamp=now - timedelta(minutes=float(s.get("minutes_ago", 0))),
                # The dashboard shows an average latency tile; unset it reads
                # 0ms, i.e. a gateway that answered instantly.
                latency_ms=float(s.get("latency_ms", 0.0)),
                cached_tokens=s.get("cached_tokens", 0),
                cache_creation_tokens=s.get("cache_creation_tokens", 0),
            ), share=False)
            # Mirror spend into the quota enforcer so /admin/quotas reflects
            # seeded usage (enforcer tracks spend separately from cost_tracker).
            #
            # share=False on both: every instance applies this same seed file at
            # startup, so the fabricated spend is already fleet-consistent, and
            # the shared counter uses an atomic ADD — sharing it would multiply
            # demo spend by the instance count and again on every restart.
            await quota_enforcer.record_spend(
                s["project_id"],
                s.get("cost", 0.0),
                budget_limit=projects[s["project_id"]].budget_limit
                if s["project_id"] in projects else None,
                share=False,
            )

    asyncio.run(_seed_usage())

    # Unhealthy providers
    for up in seed.unhealthy_providers:
        health_tracker.mark_unhealthy(
            up["provider"],
            cooldown_seconds=up.get("cooldown_seconds", 600),
        )

    # API keys. Issued through the real service so the dashboard sees genuine
    # hashed records; the raw key is discarded because nothing can display it
    # after issuance anyway.
    async def _seed_api_keys():
        for k in seed.api_keys:
            expires_at = None
            expires_in_days = k.get("expires_in_days")
            if expires_in_days is not None:
                expires_at = datetime.now(timezone.utc) + timedelta(days=expires_in_days)
            record, _raw = await api_key_service.issue_key(
                project_id=k["project_id"],
                name=k.get("name", "Unnamed key"),
                scopes=k.get("scopes", ["chat:invoke"]),
                created_by=k.get("created_by", "admin"),
                expires_at=expires_at,
            )
            if k.get("revoked"):
                await api_key_service.revoke_key(record.key_id)

    if seed.api_keys:
        asyncio.run(_seed_api_keys())

    # Audit events. Recorded through AuditTrail.record so each entry gets a real
    # hash-chain link -- the Audit Log page verifies chain integrity, and
    # hand-built records would fail that check.
    async def _seed_audit_events():
        for ev in seed.audit_events:
            try:
                event_type = AuditEventType(ev["event_type"])
            except (KeyError, ValueError):
                logger.warning(
                    "Demo seed: skipping audit event with invalid event_type %r",
                    ev.get("event_type"),
                )
                continue
            await audit_trail.record(
                event_type=event_type,
                user_id=ev.get("user_id", "unknown"),
                project_id=ev.get("project_id", "unknown"),
                request_id=ev.get("request_id", "req-demo"),
                data=ev.get("data", {}),
            )

    if seed.audit_events:
        asyncio.run(_seed_audit_events())

    # Webhook destinations
    for wd in seed.webhook_destinations:
        try:
            dest_type = DestinationType(wd.get("type", "webhook"))
        except ValueError:
            logger.warning(
                "Demo seed: skipping webhook destination %r with invalid type %r",
                wd.get("name"), wd.get("type"),
            )
            continue
        event_dispatcher.add_destination(EventDestination(
            name=wd["name"],
            destination_type=dest_type,
            config=wd.get("config", {}),
            event_filter=wd.get("event_filter"),
            enabled=wd.get("enabled", True),
        ))

    return projects, user_configs, seed.policies


async def _load_persisted_state(persistence: DynamoPersistence):
    """Load projects, user configs, usage records, feedback, Cedar policies,
    event destinations, and the region topology."""
    loaded_projects = await persistence.load_projects()
    loaded_user_configs = await persistence.load_user_configs()
    loaded_records = await persistence.load_usage_records()
    loaded_feedback = await persistence.load_feedback_records()
    loaded_policies = await persistence.load_all_cedar_policies()
    loaded_destinations = await persistence.load_event_destinations()
    # A topology read failure is not equivalent to "no saved topology": the
    # latter permits the checked-in config, while the former could bypass a
    # newer residency or failover rule. Let it fail startup.
    loaded_topology = await persistence.load_region_topology_snapshot()
    return (
        loaded_projects,
        loaded_user_configs,
        loaded_records,
        loaded_feedback,
        loaded_policies,
        loaded_destinations,
        loaded_topology,
    )


def _register_persisted_budgets(
    cost_tracker: CostTracker,
    policy_resolver: PolicyHierarchyResolver,
    loaded_projects: dict[str, Project] | None,
    loaded_user_configs: dict[str, dict] | None,
) -> None:
    """Arm enforcement for budgets that came back from DynamoDB.

    Mirrors what ``_apply_seed_data`` does for seeded projects and users, because
    the two paths must not disagree about what a budget means: a limit set via
    ``PUT /admin/projects/{id}`` and one written in the seed file should behave
    identically after a restart, and before this they did not — the persisted one
    was displayed but never checked.

    The policy node is only created where one does not already exist. A real
    hierarchy (org → team → project) carries limits the flat per-project node
    would flatten away, and the persisted project's own limit is already enforced
    through ``cost_tracker``; overwriting a tree node here would raise the
    effective limit to the project's own, discarding the tighter parent cap.
    """
    for project in (loaded_projects or {}).values():
        if project.budget_limit is None and project.alert_threshold is None:
            continue
        cost_tracker.register_project(
            project.project_id,
            budget_limit=project.budget_limit,
            alert_threshold=project.alert_threshold,
            tenant_id=project.tenant_id,
        )
        if project.budget_limit is not None and (
            project.project_id not in policy_resolver._nodes
        ):
            policy_resolver._nodes[project.project_id] = PolicyNode(
                node_id=project.project_id,
                node_type="project",
                parent_id=None,
                display_name=project.name,
                limits={"budget_limit": project.budget_limit},
            )

    for user_id, config in (loaded_user_configs or {}).items():
        # Registered even when both limits are None, matching _apply_seed_data:
        # a config row exists because someone configured the user, and clearing a
        # limit to None is a deliberate act that should survive a restart rather
        # than silently reverting to a seeded value.
        cost_tracker.register_user(
            user_id,
            budget_limit=config.get("budget_limit"),
            alert_threshold=config.get("alert_threshold"),
        )


def _apply_persisted_infrastructure(
    dispatcher,
    hub_config: HubConfig,
    loaded_destinations: list[dict] | None,
    loaded_topology: dict | None,
) -> None:
    """Apply persisted event destinations and region topology over the seed.

    ``is not None``, not truthiness, in both cases: an empty stored set is a
    deliberate operator state ("I removed every destination / every spoke"), and
    ``[]`` is falsy — a truthiness check would treat that as "nothing was ever
    saved" and silently restore the seed, which is exactly the resurrection this
    persistence exists to prevent.

    Extracted from the app factory so the distinction is testable without booting
    the whole gateway.
    """
    if loaded_destinations is not None:
        _apply_persisted_destinations(dispatcher, loaded_destinations)
    if loaded_topology is not None:
        _apply_persisted_topology(hub_config, loaded_topology)


def _apply_persisted_destinations(dispatcher, loaded: list[dict]) -> None:
    """Replace the seeded event destinations with the persisted set.

    Replace, not merge. Merging by name looks safer but cannot express a
    deletion: a destination removed through ``DELETE /admin/webhooks`` is absent
    from the stored set, so a merge would leave the seeded copy in place and the
    destination would silently resume receiving security events at the next boot
    — verified, before this became a replace.

    Reaching here at all means an operator has written the set through the admin
    API, so the stored list is the newer statement of intent.
    """
    # `destinations` is a copy, so removing while iterating it is safe.
    for existing in dispatcher.destinations:
        dispatcher.remove_destination(existing.name)
    for dest in loaded:
        try:
            dest_type = DestinationType(dest.get("destination_type", "webhook"))
        except ValueError:
            # A destination type this build doesn't know about — skip it rather
            # than crash the gateway, matching how an unsupported stored Cedar
            # policy is handled. Logged loudly because the events it was meant to
            # receive now go nowhere.
            logger.error(
                "Skipping persisted event destination %s: unknown type %r",
                dest.get("name"), dest.get("destination_type"),
            )
            continue
        # remove-then-add rather than a bare add: the stored set is written from
        # the dispatcher's own deduped list, but a hand-edited row shouldn't be
        # able to install the same name twice and double-deliver every event.
        dispatcher.remove_destination(dest["name"])
        dispatcher.add_destination(EventDestination(
            name=dest["name"],
            destination_type=dest_type,
            config=dest.get("config", {}),
            event_filter=dest.get("event_filter"),
            enabled=dest.get("enabled", True),
        ))


def _apply_persisted_topology(hub_config: HubConfig, loaded: dict) -> None:
    """Replace the config-file topology with the persisted one, in place.

    Mutates the caller's ``HubConfig`` rather than returning a new one:
    ``RegionRouter`` and ``SpokeHealthMonitor`` are both already constructed
    around this object by the time persisted state loads, and neither re-reads it,
    so handing back a replacement would leave both routing on spokes.yaml.

    Replace, not merge — unlike policies and destinations, the topology is stored
    as a single item precisely because it is edited as a unit. Merging a stored
    spoke list with spokes.yaml would resurrect every spoke an operator had
    removed through the API, which is the bug this persistence is here to fix.
    """
    apply_persisted_topology(hub_config, loaded)
