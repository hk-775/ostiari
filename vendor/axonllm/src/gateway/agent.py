"""Gateway Agent entrypoint — orchestrates the full chat completion request flow."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
import uuid
import warnings
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator

from src.gateway.cache_manager import CacheManager
from src.gateway.semantic_cache import SemanticCache
from src.gateway.cost_tracker import CostTracker
from src.gateway.guardrail_engine import GuardrailEngine
from src.gateway.models import (
    BudgetStatus,
    ChatCompletionRequest,
    ChatCompletionResponse,
    EmbeddingRequest,
    EnsemblePreset,
    Project,
    ProviderModelMapping,
    RequestContext,
    ResolvedPolicy,
    StreamChunk,
    TokenUsage,
    UsageRecord,
)
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.quota_enforcer import BudgetReservation, QuotaDecision
from src.gateway.request_validator import RequestValidator
from src.gateway.router import (
    AllProvidersExhaustedError,
    EnsembleAccessError,
    EnsembleCostCeilingError,
    EnsembleNoSurvivorsError,
    EnsembleQuorumError,
    EnsembleSynthesisError,
    ProviderError,
    Router,
)
from src.gateway.session_manager import SessionManager
from src.gateway.smart_routing import NoCandidateModelsError
from src.gateway.streaming import simulate_streaming
from src.gateway.routing_runtime import OpenedProviderStream

if TYPE_CHECKING:
    from src.gateway.auth.policy_hierarchy import PolicyHierarchyResolver
    from src.gateway.multi_region.region_router import RegionRouter
    from src.gateway.provider_fn_factory import ProviderFnFactory
    from src.gateway.quota_enforcer import QuotaEnforcer
    from src.gateway.observability.trace_forwarder import TraceForwarder
    from src.gateway.persistence import DynamoPersistence
    from src.gateway.security.audit_trail import AuditTrail
    from src.gateway.security.event_dispatcher import EventDispatcher
    from src.gateway.security.injection_detector import PromptInjectionDetector
    from src.gateway.security.pii_redactor import PIIRedactor, RedactionMapping
    from src.gateway.routing_runtime import RoutingRuntime


logger = logging.getLogger("gateway.agent")

# Complete-output inspection must be bounded. The normal request validator caps
# requested output tokens, but providers can ignore that cap or omit usage.
_MAX_POLICY_BUFFER_BYTES = 8 * 1024 * 1024
_MAX_STREAM_OUTPUT_BYTES = 8 * 1024 * 1024
_PROVIDER_FAILURE_MESSAGE = "The provider request failed."
_MAX_EMBEDDING_INPUTS = 2048
_MAX_EMBEDDING_INPUT_BYTES = 512 * 1024
_MAX_EMBEDDING_DIMENSIONS = 65_536


# ---------------------------------------------------------------------------
# Stub for BedrockAgentCoreApp (since we can't import the real SDK)
# ---------------------------------------------------------------------------


class BedrockAgentCoreApp:
    """Stub that mirrors the real BedrockAgentCoreApp decorator API."""

    def __init__(self) -> None:
        self._entrypoints: dict[str, Any] = {}

    def entrypoint(self, name: str):
        """Decorator that registers an async function as a named entrypoint."""

        def decorator(fn):
            self._entrypoints[name] = fn
            return fn

        return decorator


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


class GatewayError(Exception):
    """Structured error raised during request processing."""

    def __init__(self, status_code: int, error_type: str, message: str, code: str | None = None) -> None:
        self.status_code = status_code
        self.error_type = error_type
        self.message = message
        self.code = code
        super().__init__(message)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "error": {
                "type": self.error_type,
                "message": self.message,
            }
        }
        if self.code:
            d["error"]["code"] = self.code
        return d


def _error_response(status_code: int, error_type: str, message: str, code: str | None = None) -> dict:
    """Build a JSON-style error dict matching the spec error format."""
    d: dict[str, Any] = {
        "error": {
            "type": error_type,
            "message": message,
        },
        "status_code": status_code,
    }
    if code:
        d["error"]["code"] = code
    return d


def _public_ensemble_failure_reason(reason: object) -> str:
    """Return a bounded failure reason that cannot contain an upstream body."""
    if reason in {"timeout", "timeout (60s)"}:
        return "timeout"
    return _PROVIDER_FAILURE_MESSAGE


def extract_last_user_prompt(messages: list[dict] | None) -> str:
    """The last real user text in a conversation, or ``""`` if there is none.

    In a tool loop the final message is a tool result, and mid-loop assistant
    turns have ``content=None`` — classifying either as "the prompt" would route
    later rounds of one conversation to a different model than the round that
    chose the tool. Walk back to the last genuine user text instead, and keep it a
    ``str`` so the classifier cannot crash on a list or ``None``.

    Shared by smart routing, ensemble routing and usage-record classification, so
    all three agree on what "the prompt" means for a given request.
    """
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content:
            return content
        if isinstance(content, list):
            text = " ".join(
                block.get("text", "") for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
            if text:
                return text
    return ""


# ---------------------------------------------------------------------------
# GatewayAgent — orchestration class
# ---------------------------------------------------------------------------


class GatewayAgent:
    """Orchestrates the full chat completion request flow.

    All dependencies are injected via the constructor for testability.
    """

    def __init__(
        self,
        router: Router,
        rate_limiter: SlidingWindowRateLimiter,
        guardrail_engine: GuardrailEngine,
        cache_manager: CacheManager,
        cost_tracker: CostTracker,
        session_manager: SessionManager | None = None,
        projects: dict[str, Project] | None = None,
        provider_fn_factory: ProviderFnFactory | None = None,
        user_configs: dict[str, dict] | None = None,
        request_validator: RequestValidator | None = None,
        smart_routing_enabled: bool = False,
        quota_enforcer: QuotaEnforcer | None = None,
        policy_resolver: PolicyHierarchyResolver | None = None,
        pii_redactor: PIIRedactor | None = None,
        injection_detector: PromptInjectionDetector | None = None,
        audit_trail: AuditTrail | None = None,
        event_dispatcher: EventDispatcher | None = None,
        region_router: RegionRouter | None = None,
        trace_forwarder: TraceForwarder | None = None,
        otlp_exporter: Any = None,
        semantic_cache: SemanticCache | None = None,
        persistence: DynamoPersistence | None = None,
        routing_runtime: RoutingRuntime | None = None,
    ) -> None:
        self.router = router
        self.rate_limiter = rate_limiter
        self.guardrail_engine = guardrail_engine
        self.cache_manager = cache_manager
        self.cost_tracker = cost_tracker
        self.session_manager = session_manager
        # `x if x is not None`, not `x or {}`: both dicts are shared with AdminAPI
        # so an admin write is visible to the request path without a restart, and
        # an empty dict is falsy — so the `or {}` form broke exactly that sharing
        # on a gateway booting without seed data.
        self._projects: dict[str, Project] = projects if projects is not None else {}
        self.provider_fn_factory = provider_fn_factory
        self._user_configs: dict[str, dict] = (
            user_configs if user_configs is not None else {})
        self.request_validator = request_validator
        self._smart_routing_enabled = smart_routing_enabled
        self._quota_enforcer = quota_enforcer
        self._policy_resolver = policy_resolver
        self._pii_redactor = pii_redactor
        self._injection_detector = injection_detector
        self._audit_trail = audit_trail
        self._event_dispatcher = event_dispatcher
        self._region_router = region_router
        self._trace_forwarder = trace_forwarder
        self._otlp_exporter = otlp_exporter
        self._semantic_cache = semantic_cache
        self._persistence = persistence
        self._routing_runtime = routing_runtime

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def handle_chat_completion(
        self,
        request_data: dict,
        context: dict,
    ) -> dict | AsyncIterator[dict]:
        """Main entrypoint that orchestrates the full request flow.

        Returns either a response dict (non-streaming) or an async generator
        of SSE-formatted dicts (streaming).
        """
        # 1. Parse request
        request = self._parse_request(request_data)

        # 2. Extract context
        req_ctx = self._extract_context(context)
        project = self._project_for_context(req_ctx)
        if (
            req_ctx.tenant_id is not None
            and project is None
            and not req_ctx.allow_legacy_project_lookup
        ):
            return _error_response(
                404,
                "not_found",
                "The requested resource was not found.",
                code="resource_not_found",
            )
        if project is not None and (
            project.budget_limit is not None
            or project.alert_threshold is not None
        ):
            self.cost_tracker.register_project(
                project.project_id,
                budget_limit=project.budget_limit,
                alert_threshold=project.alert_threshold,
                tenant_id=req_ctx.tenant_id,
            )

        # 2.5. Request validation (before rate limiting)
        # Skip model validation for smart routing (model will be selected later)
        is_smart_routing = self._is_smart_routing_request(request, context)
        is_ensemble, ensemble_preset_name, ensemble_err = self._is_ensemble_request(request, context)
        # Ensemble requests defer concrete model selection to the preset, so skip
        # single-model validation/access checks exactly as smart routing does.
        skip_model_checks = is_smart_routing or is_ensemble
        if self.request_validator is not None and not skip_model_checks:
            validation_errors = self.request_validator.validate(request)
            if validation_errors:
                first_error = validation_errors[0]
                # Determine status code and error code based on error type
                if first_error.field == "model":
                    return _error_response(
                        404, "not_found", first_error.message, code="model_not_found"
                    )
                elif "role" in first_error.field and "missing" not in first_error.message.lower():
                    return _error_response(
                        400, "invalid_request", first_error.message, code="invalid_role"
                    )
                elif "missing" in first_error.message.lower():
                    return _error_response(
                        400, "invalid_request", first_error.message, code="invalid_message_format"
                    )
                elif first_error.field == "messages" and "token" in first_error.message.lower():
                    return _error_response(
                        400, "invalid_request", first_error.message, code="token_limit_exceeded"
                    )
                else:
                    return _error_response(
                        400, "invalid_request", first_error.message, code="invalid_message_format"
                    )
        # 2.7. Policy-hierarchy quota enforcement
        resolved_policy = None
        if self._quota_enforcer is not None and self._policy_resolver is not None:
            resolved_policy = await self._policy_resolver.resolve(
                req_ctx.project_id,
                tenant_id=req_ctx.tenant_id,
                project=project,
            )
            estimated_cost = self._estimate_request_cost(request)
            quota_decision = await self._quota_enforcer.enforce_all(
                project_id=req_ctx.project_id,
                model=request.model or "",
                provider=context.get("provider"),
                max_tokens=request.max_tokens,
                estimated_cost=estimated_cost,
                policy=resolved_policy,
                tenant_id=req_ctx.tenant_id,
                project=project,
            )
            if not quota_decision.allowed:
                return self._quota_error(quota_decision)

            # Apply the policy's max_tokens ceiling. This also bounds requests
            # that omit max_tokens entirely — otherwise an unbounded (streaming)
            # response could exhaust resources or amplify cost on shared
            # provider credentials.
            request.max_tokens = self._quota_enforcer.cap_max_tokens(
                request.max_tokens, resolved_policy
            )

        if (
            not skip_model_checks
            and not self._router_model_is_available(request.model)
        ):
            return _error_response(
                503,
                "service_unavailable",
                f"Model '{request.model}' is not available in this deployment.",
                code="model_unavailable",
            )

        # 2.8. Prompt injection detection
        request_id = f"req_{uuid.uuid4().hex}"
        pii_mapping = None

        if self._injection_detector is not None:
            injection_result = self._injection_detector.analyze_messages(request.messages or [])
            if injection_result.score > 0:
                if self._audit_trail is not None:
                    await self._audit_trail.record_injection_event(
                        user_id=req_ctx.user_id,
                        project_id=req_ctx.project_id,
                        request_id=request_id,
                        threat_level=injection_result.threat_level.value,
                        patterns=injection_result.detected_patterns,
                        blocked=injection_result.should_block,
                        tenant_id=req_ctx.tenant_id or "__legacy__",
                    )
                if self._event_dispatcher is not None:
                    await self._event_dispatcher.dispatch_injection_event(
                        event_id=f"{request_id}:injection",
                        user_id=req_ctx.user_id,
                        project_id=req_ctx.project_id,
                        threat_level=injection_result.threat_level.value,
                        patterns=injection_result.detected_patterns,
                        blocked=injection_result.should_block,
                        tenant_id=req_ctx.tenant_id or "__legacy__",
                    )
                if injection_result.should_block:
                    return _error_response(
                        400,
                        "content_policy_violation",
                        f"Request blocked: prompt injection detected "
                        f"(threat_level={injection_result.threat_level.value}, "
                        f"score={injection_result.score:.2f})",
                        code="injection_blocked",
                    )

        # 2.9. PII redaction
        if self._pii_redactor is not None:
            effective_policy = resolved_policy or ResolvedPolicy()
            # _async so a policy with pii_ner_enabled also gets name/address
            # detection. It delegates to the sync path when NER is off, so the
            # default request keeps its previous cost and behaviour exactly.
            redacted_messages, pii_mapping = (
                await self._pii_redactor.redact_messages_async(
                    request.messages or [], effective_policy
                )
            )
            if pii_mapping.redacted_count > 0:
                # replace() rather than re-listing every field: a hand-rolled
                # rebuild silently drops any field added later (that is exactly
                # how `tools` went missing), and dropping one here would strip
                # the caller's tools from every request that happened to contain
                # PII — an intermittent failure far harder to find than a total one.
                request = dataclasses.replace(request, messages=redacted_messages)
                if self._audit_trail is not None:
                    redacted_types = list(pii_mapping._counters.keys())
                    await self._audit_trail.record_pii_redaction(
                        user_id=req_ctx.user_id,
                        project_id=req_ctx.project_id,
                        request_id=request_id,
                        redacted_types=redacted_types,
                        count=pii_mapping.redacted_count,
                        tenant_id=req_ctx.tenant_id or "__legacy__",
                    )
                if self._event_dispatcher is not None:
                    await self._event_dispatcher.dispatch_pii_event(
                        event_id=f"{request_id}:pii:input",
                        user_id=req_ctx.user_id,
                        project_id=req_ctx.project_id,
                        redacted_types=list(pii_mapping._counters.keys()),
                        count=pii_mapping.redacted_count,
                        tenant_id=req_ctx.tenant_id or "__legacy__",
                    )

        # 3. Rate limit check
        rate_result = await self.rate_limiter.check_rate_limit(
            req_ctx.user_id,
            req_ctx.project_id,
            tenant_id=req_ctx.tenant_id,
            project=project,
        )

        # Build rate limit headers from the result
        _rate_limit_headers = {
            "X-RateLimit-Limit": str(rate_result.limit),
            "X-RateLimit-Remaining": str(rate_result.remaining),
            "X-RateLimit-Reset": str(int(rate_result.reset_at.timestamp())),
        }

        if not rate_result.allowed:
            if rate_result.retry_after_seconds is not None:
                _rate_limit_headers["Retry-After"] = str(rate_result.retry_after_seconds)
            resp = _error_response(
                429,
                "rate_limit_error",
                f"Rate limit exceeded. Retry after {rate_result.retry_after_seconds}s.",
                code="rate_limit_exceeded",
            )
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp

        # 4. Project model access check (skip for smart routing — model will be selected later)
        if not skip_model_checks and project and project.allowed_models and request.model not in project.allowed_models:
            resp = _error_response(
                403,
                "forbidden",
                f"Model '{request.model}' is not allowed for project '{req_ctx.project_id}'.",
                code="model_not_allowed",
            )
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp

        # 5. User model access check (skip for smart routing)
        try:
            user_config = await self._user_config_for_context(req_ctx)
        except RuntimeError:
            return _error_response(
                503,
                "service_unavailable",
                "Tenant user configuration is temporarily unavailable.",
                code="user_config_unavailable",
            )
        self.cost_tracker.register_user(
            req_ctx.user_id,
            budget_limit=user_config.get("budget_limit"),
            alert_threshold=user_config.get("alert_threshold"),
            tenant_id=req_ctx.tenant_id,
        )
        user_allowed = user_config.get("allowed_models")
        if not skip_model_checks and user_allowed and request.model not in user_allowed:
            resp = _error_response(
                403,
                "forbidden",
                f"Model '{request.model}' is not allowed for user '{req_ctx.user_id}'.",
                code="model_not_allowed",
            )
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp

        # 6. Project budget check
        project_budget: BudgetStatus | None = None
        if project:
            project_budget = await self.cost_tracker.check_budget(
                req_ctx.project_id,
                tenant_id=req_ctx.tenant_id,
            )
            if project_budget.is_over_budget:
                resp = _error_response(
                    429,
                    "budget_exceeded",
                    f"Project '{req_ctx.project_id}' has exceeded its budget.",
                    code="budget_exceeded",
                )
                resp["_rate_limit_headers"] = _rate_limit_headers
                return resp

        # 7. User budget check
        user_budget = await self.cost_tracker.check_user_budget(
            req_ctx.user_id,
            tenant_id=req_ctx.tenant_id,
        )
        if user_budget.is_over_budget:
            resp = _error_response(
                429,
                "budget_exceeded",
                f"User '{req_ctx.user_id}' has exceeded their budget.",
                code="budget_exceeded",
            )
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp
        has_request_budget = self._request_has_budget(
            project_budget,
            user_budget,
            resolved_policy,
        )

        # 8. Request guardrails
        if project and project.guardrail_rules:
            guard_result = await self.guardrail_engine.evaluate_request(
                request, project.guardrail_rules
            )
            if not guard_result.passed:
                resp = _error_response(
                    400,
                    "content_policy_violation",
                    guard_result.message or "Request blocked by guardrail.",
                    code="guardrail_violation",
                )
                resp["_rate_limit_headers"] = _rate_limit_headers
                return resp

        # 9. Cache check
        #
        # Two lookups, in cost order. The exact key is a hash comparison against
        # a dict; the semantic one costs an embedding round-trip, so it only runs
        # once the cheap path has missed. cache_key is kept for step 16, which
        # writes the response back — recomputing it there would hash the request
        # twice per miss.
        cache_key: str | None = None
        semantic_eligible = False
        # Redaction tokens are request-local and normalize different sensitive
        # values to the same placeholders. Sharing that normalized key would
        # conflate distinct prompts even when re-injection is disabled.
        contains_redacted_pii = (
            pii_mapping is not None
            and pii_mapping.redacted_count > 0
        )
        cache_privacy_eligible = (
            not request.stream
            and request.tools is None
            and request.tool_choice is None
            and not contains_redacted_pii
            and not is_smart_routing
            and not is_ensemble
            and not context.get("provider")
            and not context.get("data_residency_zone")
            and not context.get("preferred_region")
        )
        if project and project.cache_enabled and cache_privacy_eligible:
            cache_key = self.cache_manager.compute_cache_key(
                request,
                req_ctx.project_id,
                req_ctx.tenant_id,
            )
            cached = await self.cache_manager.get(
                cache_key,
                tenant_id=req_ctx.tenant_id,
            )
            if cached is not None:
                cached = await self._apply_cached_output_policy(
                    cached,
                    request=request,
                    req_ctx=req_ctx,
                    request_id=request_id,
                    project=project,
                    resolved_policy=resolved_policy,
                    pii_mapping=pii_mapping,
                )
                result = self._response_to_dict(cached, is_cached=True)
                result["_rate_limit_headers"] = _rate_limit_headers
                return result

            semantic_eligible = (
                project.semantic_cache_enabled and self._semantic_cache is not None
            )
            if semantic_eligible:
                sem_hit = await self._semantic_cache.get(
                    request,
                    req_ctx.project_id,
                    threshold=project.semantic_cache_threshold,
                    tenant_id=req_ctx.tenant_id,
                )
                if sem_hit is not None:
                    sem_hit = await self._apply_cached_output_policy(
                        sem_hit,
                        request=request,
                        req_ctx=req_ctx,
                        request_id=request_id,
                        project=project,
                        resolved_policy=resolved_policy,
                        pii_mapping=pii_mapping,
                    )
                    result = self._response_to_dict(sem_hit, is_cached=True)
                    # Flagged separately from is_cached. An exact hit is the
                    # answer to this question; a semantic hit is the answer to a
                    # question judged equivalent, and a caller comparing
                    # responses needs to be able to tell which it got.
                    result["cache_type"] = "semantic"
                    result["_rate_limit_headers"] = _rate_limit_headers
                    return result

        # 9.5. Region routing — check spoke availability and data residency
        region_decision = None
        if self._region_router is not None:
            data_zone = context.get("data_residency_zone")
            region_decision = self._region_router.route(
                model=request.model or None,
                data_residency_zone=data_zone,
                preferred_region=context.get("preferred_region"),
            )
            if region_decision is None:
                resp = _error_response(
                    503,
                    "service_unavailable",
                    "No available region for this request"
                    + (f" (data_residency_zone={data_zone})" if data_zone else ""),
                    code="no_available_region",
                )
                resp["_rate_limit_headers"] = _rate_limit_headers
                return resp

        # 10. Route and execute
        _request_start = time.perf_counter()
        # Multi-region: the selected spoke overrides the provider endpoint/region
        # for the actual call (not just response metadata). None → default region.
        target_spoke = region_decision.target_spoke if region_decision is not None else None
        budget_reservation: BudgetReservation | None = None
        try:
            prompt_caching_enabled = project.prompt_caching_enabled if project else False

            if self._routing_runtime is not None:
                provider_fn = self._routing_runtime.provider_fn(
                    request,
                    prompt_caching_enabled=prompt_caching_enabled,
                    spoke=target_spoke,
                )
            elif self.provider_fn_factory is not None:
                provider_fn = self.provider_fn_factory.create(
                    request, prompt_caching_enabled=prompt_caching_enabled,
                    spoke=target_spoke,
                )
            else:
                provider_fn = self._make_provider_fn()
            provider_fn = self._rehearsal_provider_fn(
                provider_fn,
                context=context,
                request=request,
                request_id=request_id,
            )

            # Compute effective allowed models from project + user access lists
            effective_allowed = self._compute_effective_allowed_models(
                project,
                user_config,
            )

            # Extract prompt from last user message (shared by smart + ensemble).
            prompt = extract_last_user_prompt(request.messages)

            # Check if smart routing / ensemble routing should be used
            smart_routing_decision = None
            ensemble_decision = None
            ensemble_unavailable = False

            ensemble_config = getattr(self.router, "_ensemble_config", None)
            take_ensemble_path = is_ensemble and ensemble_config is not None and ensemble_config.is_configured

            if is_ensemble and not take_ensemble_path:
                # Backward-compat: detected as ensemble but not configured →
                # fall through to the normal/smart path and note unavailability.
                ensemble_unavailable = True

            if take_ensemble_path:
                # Malformed invocation (e.g. "ensemble:" with empty name).
                if ensemble_err:
                    resp = _error_response(
                        400, "invalid_request", ensemble_err,
                        code="ensemble_preset_missing",
                    )
                    resp["_rate_limit_headers"] = _rate_limit_headers
                    return resp

                # Resolve preset (named or default).
                if ensemble_preset_name is not None:
                    preset = ensemble_config.get_preset(ensemble_preset_name)
                    if preset is None:
                        resp = _error_response(
                            404, "not_found",
                            f"Ensemble preset '{ensemble_preset_name}' not found",
                            code="ensemble_preset_not_found",
                        )
                        resp["_rate_limit_headers"] = _rate_limit_headers
                        return resp
                else:
                    preset = ensemble_config.default_preset()
                    if preset is None:
                        resp = _error_response(
                            400, "invalid_request",
                            "No default ensemble preset configured",
                            code="ensemble_no_default",
                        )
                        resp["_rate_limit_headers"] = _rate_limit_headers
                        return resp

                # Validate access and runtime availability before any panel
                # call can incur cost. The router repeats the access check as a
                # defense-in-depth boundary.
                preset_models = [*preset.panel, preset.judge]
                if effective_allowed is not None:
                    denied_model = next(
                        (
                            model
                            for model in preset_models
                            if model not in effective_allowed
                        ),
                        None,
                    )
                    if denied_model is not None:
                        resp = _error_response(
                            403,
                            "forbidden",
                            f"Model '{denied_model}' is not allowed",
                            code="model_not_allowed",
                        )
                        resp["_rate_limit_headers"] = _rate_limit_headers
                        return resp
                unavailable_model = next(
                    (
                        model
                        for model in preset_models
                        if not self._router_model_is_available(model)
                    ),
                    None,
                )
                if unavailable_model is not None:
                    resp = _error_response(
                        503,
                        "service_unavailable",
                        (
                            f"Model '{unavailable_model}' is not available "
                            "in this deployment."
                        ),
                        code="model_unavailable",
                    )
                    resp["_rate_limit_headers"] = _rate_limit_headers
                    return resp

                # Validate the preset ceiling before reserving budget.
                per_call = self._estimate_per_call_cost(request, preset)
                ceiling_estimate = (len(preset.panel) + 1) * per_call
                if (
                    preset.cost_ceiling is not None
                    and ceiling_estimate > preset.cost_ceiling
                ):
                    resp = _error_response(
                        400,
                        "invalid_request",
                        (
                            f"Estimated ensemble cost {ceiling_estimate} "
                            f"exceeds ceiling {preset.cost_ceiling}"
                        ),
                        code="ensemble_cost_ceiling",
                    )
                    resp["_rate_limit_headers"] = _rate_limit_headers
                    return resp

                estimated = (
                    self._estimate_ensemble_cost(request, preset)
                    if has_request_budget
                    else 0.0
                )
                reserve_decision = await self._reserve_request_budget(
                    request_id=request_id,
                    req_ctx=req_ctx,
                    estimated_cost=estimated,
                    project_budget=project_budget,
                    user_budget=user_budget,
                    resolved_policy=resolved_policy,
                )
                if not reserve_decision.allowed:
                    return self._quota_error(
                        reserve_decision,
                        _rate_limit_headers,
                    )
                budget_reservation = reserve_decision.reservation

                # Streaming ensemble request: defer to the streaming
                # generator instead of running the non-streaming path. The
                # panel phase still runs to completion inside the generator
                # (output is withheld until the judge / best-single result is
                # ready), so nothing is streamed during panel dispatch.
                if request.stream:
                    return self._guard_budgeted_stream(
                        self._stream_ensemble_response(
                            request,
                            prompt,
                            preset,
                            effective_allowed,
                            req_ctx.project_id,
                            req_ctx.user_id,
                            per_call,
                            _rate_limit_headers,
                            resolved_policy,
                            pii_mapping,
                            request_id,
                            authorized_project=project,
                            tenant_id=req_ctx.tenant_id,
                            budget_reservation=budget_reservation,
                        ),
                        budget_reservation,
                        req_ctx=req_ctx,
                    )

                response, ensemble_decision = await self.router.ensemble_route(
                    request,
                    self.provider_fn_factory,
                    prompt,
                    preset,
                    allowed_models=effective_allowed,
                    project_id=req_ctx.project_id,
                    user_id=req_ctx.user_id,
                    tenant_id=req_ctx.tenant_id,
                    per_call_cost_estimate=per_call,
                    skip_shared_scopes=(
                        self._reserved_cost_tracker_scopes(
                            budget_reservation
                        )
                    ),
                )
            elif self._is_smart_routing_request(request, context):
                # TRUE STREAMING (#18): for a streaming smart request, resolve the
                # model WITHOUT a blocking call, then open the provider stream so
                # the first token flows immediately. Non-streaming keeps the
                # execute-then-return path.
                if request.stream and self._can_stream_true():
                    runtime_models = {
                        model.name
                        for model in self.router.model_registry.list_models()
                        if self._router_model_is_available(model.name)
                    }
                    routing_allowed = (
                        runtime_models
                        if effective_allowed is None
                        else runtime_models.intersection(effective_allowed)
                    )
                    smart_decision = await self.router._smart_strategy.select_model(
                        prompt,
                        routing_allowed,
                        req_ctx.project_id,
                        req_ctx.user_id,
                        tenant_id=req_ctx.tenant_id,
                    )
                    request.model = smart_decision.selected_model
                    candidate_models = {smart_decision.selected_model}
                    default_model = getattr(
                        self.router._smart_strategy,
                        "default_model",
                        None,
                    )
                    if default_model:
                        candidate_models.add(default_model)
                    reserve_decision = await self._reserve_request_budget(
                        request_id=request_id,
                        req_ctx=req_ctx,
                        estimated_cost=(
                            self._estimate_request_cost(
                                request,
                                candidate_models,
                            )
                            if has_request_budget
                            else 0.0
                        ),
                        project_budget=project_budget,
                        user_budget=user_budget,
                        resolved_policy=resolved_policy,
                    )
                    if not reserve_decision.allowed:
                        return self._quota_error(
                            reserve_decision,
                            _rate_limit_headers,
                        )
                    budget_reservation = reserve_decision.reservation
                    return self._guard_budgeted_stream(
                        self._stream_true(
                            request, context, req_ctx,
                            prompt_caching_enabled,
                            _rate_limit_headers, resolved_policy, pii_mapping,
                            request_id, _request_start,
                            preferred_provider=None,
                            effective_allowed=routing_allowed,
                            smart_routing_decision=smart_decision,
                            spoke=target_spoke,
                            budget_reservation=budget_reservation,
                        ),
                        budget_reservation,
                        req_ctx=req_ctx,
                    )
                registry = getattr(self.router, "model_registry", None)
                configured_models = getattr(registry, "models", {})
                candidate_models = (
                    set(effective_allowed)
                    if effective_allowed is not None
                    else set(configured_models)
                )
                reserve_decision = await self._reserve_request_budget(
                    request_id=request_id,
                    req_ctx=req_ctx,
                    estimated_cost=(
                        self._estimate_request_cost(
                            request,
                            candidate_models,
                        )
                        if has_request_budget
                        else 0.0
                    ),
                    project_budget=project_budget,
                    user_budget=user_budget,
                    resolved_policy=resolved_policy,
                )
                if not reserve_decision.allowed:
                    return self._quota_error(
                        reserve_decision,
                        _rate_limit_headers,
                    )
                budget_reservation = reserve_decision.reservation
                response, smart_routing_decision = await self.router.smart_route(
                    request,
                    self.provider_fn_factory,
                    prompt,
                    allowed_models=effective_allowed,
                    project_id=req_ctx.project_id,
                    user_id=req_ctx.user_id,
                    tenant_id=req_ctx.tenant_id,
                    spoke=target_spoke,
                )
            else:
                reserve_decision = await self._reserve_request_budget(
                    request_id=request_id,
                    req_ctx=req_ctx,
                    estimated_cost=(
                        self._estimate_request_cost(request)
                        if has_request_budget
                        else 0.0
                    ),
                    project_budget=project_budget,
                    user_budget=user_budget,
                    resolved_policy=resolved_policy,
                )
                if not reserve_decision.allowed:
                    return self._quota_error(
                        reserve_decision,
                        _rate_limit_headers,
                    )
                budget_reservation = reserve_decision.reservation
                # TRUE STREAMING (#18): direct single-model streaming opens the
                # provider stream directly — no blocking call, no double billing.
                if request.stream and self._can_stream_true():
                    return self._guard_budgeted_stream(
                        self._stream_true(
                            request, context, req_ctx,
                            prompt_caching_enabled,
                            _rate_limit_headers, resolved_policy, pii_mapping,
                            request_id, _request_start,
                            preferred_provider=context.get("provider"),
                            effective_allowed=effective_allowed,
                            smart_routing_decision=None,
                            spoke=target_spoke,
                            budget_reservation=budget_reservation,
                        ),
                        budget_reservation,
                        req_ctx=req_ctx,
                    )
                if self._routing_runtime is not None:
                    response = await self._routing_runtime.complete(
                        request,
                        preferred_provider=context.get("provider"),
                        allowed_models=effective_allowed,
                        provider_fn=provider_fn,
                    )
                else:
                    response = await self.router.execute_with_fallback(
                        request, provider_fn,
                        preferred_provider=context.get("provider"),
                        allowed_models=effective_allowed,
                    )
        except EnsembleAccessError as exc:
            await self._release_request_budget(
                budget_reservation,
                req_ctx=req_ctx,
            )
            resp = _error_response(
                403, "forbidden",
                f"Model '{exc.model}' is not allowed",
                code="model_not_allowed",
            )
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp
        except EnsembleCostCeilingError as exc:
            await self._release_request_budget(
                budget_reservation,
                req_ctx=req_ctx,
            )
            resp = _error_response(
                400, "invalid_request", str(exc),
                code="ensemble_cost_ceiling",
            )
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp
        except EnsembleNoSurvivorsError as exc:
            totals = await self._finalize_request_budget(
                budget_reservation,
                actual_cost=exc.decision.total_cost,
                req_ctx=req_ctx,
            )
            if totals and "user" in totals:
                self.cost_tracker.adopt_user_spend(
                    req_ctx.user_id,
                    totals["user"],
                    tenant_id=req_ctx.tenant_id,
                )
            resp = _error_response(
                502, "provider_error",
                _PROVIDER_FAILURE_MESSAGE,
                code="ensemble_no_survivors",
            )
            resp["ensemble"] = self._ensemble_metadata(exc.decision)
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp
        except EnsembleQuorumError as exc:
            totals = await self._finalize_request_budget(
                budget_reservation,
                actual_cost=exc.decision.total_cost,
                req_ctx=req_ctx,
            )
            if totals and "user" in totals:
                self.cost_tracker.adopt_user_spend(
                    req_ctx.user_id,
                    totals["user"],
                    tenant_id=req_ctx.tenant_id,
                )
            resp = _error_response(
                502, "provider_error",
                _PROVIDER_FAILURE_MESSAGE,
                code="ensemble_quorum_not_met",
            )
            resp["ensemble"] = self._ensemble_metadata(exc.decision)
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp
        except EnsembleSynthesisError as exc:
            totals = await self._finalize_request_budget(
                budget_reservation,
                actual_cost=exc.decision.total_cost,
                req_ctx=req_ctx,
            )
            if totals and "user" in totals:
                self.cost_tracker.adopt_user_spend(
                    req_ctx.user_id,
                    totals["user"],
                    tenant_id=req_ctx.tenant_id,
                )
            resp = _error_response(
                502, "provider_error",
                _PROVIDER_FAILURE_MESSAGE,
                code="ensemble_synthesis_failed",
            )
            resp["ensemble"] = self._ensemble_metadata(exc.decision)
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp
        except AllProvidersExhaustedError:
            await self._release_request_budget(
                budget_reservation,
                req_ctx=req_ctx,
            )
            resp = _error_response(
                502,
                "provider_error",
                _PROVIDER_FAILURE_MESSAGE,
                code="all_providers_exhausted",
            )
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp
        except NoCandidateModelsError as exc:
            await self._release_request_budget(
                budget_reservation,
                req_ctx=req_ctx,
            )
            resp = _error_response(
                502,
                "provider_error",
                str(exc),
                code="no_candidate_models",
            )
            resp["_rate_limit_headers"] = _rate_limit_headers
            return resp
        except Exception:
            if budget_reservation is not None:
                await self._finalize_request_budget(
                    budget_reservation,
                    actual_cost=budget_reservation.amount,
                    req_ctx=req_ctx,
                )
            raise

        # 12. Cost tracking
        # Ensemble routing records per-call usage internally via the router's
        # cost tracker; skip the normal post-response recording to avoid
        # double-counting.
        budget_finalization_failed = False
        if ensemble_decision is None:
            cost = self.cost_tracker.calculate_cost(
                response.provider,
                self._response_billing_model(response),
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
                cached_tokens=response.usage.cached_tokens,
                cache_creation_tokens=response.usage.cache_creation_tokens,
            )
            # Keep the gateway's own request_id (generated at step 2.8). It used
            # to be replaced with response.id here, but provider ids aren't
            # guaranteed unique per call — the three Bedrock Mantle routes fall
            # back to a constant "mantle-response" when the upstream omits one.
            # Trace/span ids hash from request_id and usage rows de-dupe by it,
            # so a repeated value collapses many calls into one span and one
            # usage row. The provider's id is kept alongside for correlation.
            _latency_ms = (time.perf_counter() - _request_start) * 1000
            usage_record = UsageRecord(
                request_id=request_id,
                project_id=req_ctx.project_id,
                user_id=req_ctx.user_id,
                tenant_id=req_ctx.tenant_id,
                provider=response.provider,
                model=request.model,
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
                cost=cost,
                timestamp=datetime.now(timezone.utc),
                cached_tokens=response.usage.cached_tokens,
                cache_creation_tokens=response.usage.cache_creation_tokens,
                latency_ms=_latency_ms,
                status="success",
                task_type=self._classify_for_usage(prompt, smart_routing_decision),
                provider_request_id=response.id or "",
            )
            reservation_totals = await self._finalize_request_budget(
                budget_reservation,
                actual_cost=cost,
                req_ctx=req_ctx,
            )
            budget_finalization_failed = reservation_totals is None
            await self.cost_tracker.record_usage(
                usage_record,
                skip_shared_scopes=(
                    self._reserved_cost_tracker_scopes(
                        budget_reservation
                    )
                ),
            )
            if reservation_totals:
                self.cost_tracker.adopt_reserved_spend(
                    usage_record,
                    reservation_totals,
                )

            # Forward the trace to an embedding Ostiari (best-effort; never blocks
            # or fails the request). forward() swallows and logs its own errors,
            # and is a no-op when Ostiari isn't detected.
            if self._trace_forwarder is not None:
                await self._trace_forwarder.forward(usage_record)

            # Native OTLP span export — STANDALONE path only. When embedded in
            # Ostiari (trace_forwarder "detected" it), Ostiari emits the span with
            # its governance signal, so we suppress here to avoid a double-export
            # (exactly one span per request in either mode).
            if self._otlp_exporter is not None and not (
                self._trace_forwarder is not None and self._trace_forwarder.enabled
            ):
                self._otlp_exporter.export_usage(usage_record)

            # A project reservation already reconciled this counter. Requests
            # with no project cap retain the existing shared spend history so a
            # budget added later starts from actual prior usage.
            if (
                self._quota_enforcer is not None
                and not self._reservation_has_scope(
                    budget_reservation,
                    "quota",
                )
            ):
                budget_limit = resolved_policy.budget_limit if resolved_policy else None
                await self._quota_enforcer.record_spend(
                    req_ctx.project_id,
                    cost,
                    budget_limit=budget_limit,
                    tenant_id=req_ctx.tenant_id,
                )
        else:
            reservation_totals = await self._finalize_request_budget(
                budget_reservation,
                actual_cost=ensemble_decision.total_cost,
                req_ctx=req_ctx,
            )
            budget_finalization_failed = reservation_totals is None
            if reservation_totals and "user" in reservation_totals:
                self.cost_tracker.adopt_user_spend(
                    req_ctx.user_id,
                    reservation_totals["user"],
                    tenant_id=req_ctx.tenant_id,
                )

        if budget_finalization_failed:
            response_error = _error_response(
                503,
                "service_unavailable",
                "The provider call completed, but budget accounting could not "
                "be finalized.",
                code="budget_finalization_failed",
            )
            response_error["_rate_limit_headers"] = _rate_limit_headers
            return response_error

        # 11. Response guardrails run after accounting.
        # 11.5. Output PII policy re-injects only caller-supplied values and
        # redacts newly generated PII. A policy or audit outage may withhold the
        # response, but cannot erase a provider charge that has already occurred.
        (
            response,
            response_blocked,
            output_pii_count,
            output_pii_types,
        ) = await self._apply_output_policy(
            response,
            project=project,
            resolved_policy=resolved_policy,
            pii_mapping=pii_mapping,
        )

        await self._record_output_pii_redaction(
            req_ctx=req_ctx,
            request_id=request_id,
            count=output_pii_count,
            redacted_types=output_pii_types,
        )

        # 11.6. Audit after accounting. A durable audit outage may still fail the
        # request closed, but it must never erase a provider charge.
        if self._audit_trail is not None:
            await self._audit_trail.record_llm_request(
                user_id=req_ctx.user_id,
                project_id=req_ctx.project_id,
                request_id=request_id,
                model=response.model,
                provider=response.provider,
                message_count=len(request.messages or []),
                pii_redacted_count=(
                    (pii_mapping.redacted_count if pii_mapping else 0)
                    + output_pii_count
                ),
                injection_score=0.0,
                tenant_id=req_ctx.tenant_id or "__legacy__",
            )

        # 13. Budget status for streaming (already enforced pre-request)
        budget_status: BudgetStatus | None = None

        # 14. Session storage
        session_id = context.get("session_id")
        if session_id and self.session_manager:
            await self.session_manager.store_exchange(session_id, request, response)

        # 15. Streaming support
        if request.stream:
            if self.provider_fn_factory is not None and response.provider != "google_ai":
                return self._stream_response_real(
                    request, response, budget_status, _rate_limit_headers,
                    prompt_caching_enabled=prompt_caching_enabled,
                )
            return self._stream_response(
                response, budget_status, _rate_limit_headers
            )

        # 15.5. Cache write
        #
        # Here rather than immediately after the provider call, for two reasons.
        # It is past step 11 (guardrails) and step 11.5 (PII re-injection), so
        # what gets stored is the response as actually returned — caching the
        # pre-guardrail one would let a later hit bypass the guardrail entirely.
        # And it is past step 15, so streaming responses never reach it: the
        # cached object is complete, and replaying it as a stream is a different
        # shape than the caller asked for.
        #
        # cache_key is None whenever caching is off for the project, which is
        # what gates the exact-match write. Until now nothing called
        # cache_manager.put at all — the step 9 read above was against a cache
        # nothing ever wrote to, so it could only ever miss.
        if cache_key is not None and not response_blocked:
            ttl = project.cache_ttl_seconds if project else 300
            await self.cache_manager.put(
                cache_key,
                response,
                ttl,
                tenant_id=req_ctx.tenant_id,
            )
            if semantic_eligible:
                # put() swallows its own failures; an embedding outage must not
                # turn a completed request into an error.
                await self._semantic_cache.put(
                    request,
                    req_ctx.project_id,
                    response,
                    ttl,
                    tenant_id=req_ctx.tenant_id,
                )

        # 16. Non-streaming return
        result = self._response_to_dict(response)
        if smart_routing_decision is not None:
            result["smart_routing"] = {
                "task_type": smart_routing_decision.task_type,
                "confidence": smart_routing_decision.confidence,
                "selected_model": smart_routing_decision.selected_model,
                "benchmark_score": smart_routing_decision.benchmark_score,
                "candidates": smart_routing_decision.candidates_considered,
                "used_fallback": smart_routing_decision.used_fallback,
                "cost_quality_tradeoff": smart_routing_decision.cost_quality_tradeoff,
            }
        if ensemble_decision is not None:
            result["ensemble"] = self._ensemble_metadata(ensemble_decision)
        if ensemble_unavailable:
            result["ensemble_unavailable"] = True
        if region_decision is not None:
            result["region"] = {
                "spoke": region_decision.target_spoke.region,
                "reason": region_decision.reason,
            }
        result["_rate_limit_headers"] = _rate_limit_headers
        return result

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def _can_stream_true(self) -> bool:
        """True when the real end-to-end streaming path is usable.

        Requires a ProviderFnFactory (owns the HttpClient.execute_streaming SSE
        path). Without one we fall back to the old select-then-simulate path.
        """
        return (
            self._routing_runtime is not None
            or self.provider_fn_factory is not None
        )

    def _resolve_stream_chain(
        self, model: str, preferred_provider: str | None,
    ) -> list[ProviderModelMapping]:
        """Ordered provider mappings to try when opening the stream.

        Mirrors execute_with_fallback's ordering: a preferred provider first,
        then remaining providers by fallback_order — but for the streaming path
        we only fall back BEFORE the first byte reaches the client.
        """
        chain = self.router.get_fallback_chain(model)
        if preferred_provider:
            chain = sorted(
                chain,
                key=lambda m: (m.provider != preferred_provider, m.fallback_order),
            )
        return chain

    def _effective_pii_policy(
        self, resolved_policy: ResolvedPolicy | None
    ) -> ResolvedPolicy:
        policy = resolved_policy or ResolvedPolicy()
        if self._pii_redactor is None:
            return policy
        return self._pii_redactor.effective_policy(policy)

    def _requires_output_buffering(
        self,
        *,
        project: Project | None,
        resolved_policy: ResolvedPolicy | None,
    ) -> bool:
        """Whether output must be complete before any provider text is released."""
        has_response_guardrail = bool(
            project
            and any(
                rule.applies_to in ("response", "both")
                for rule in project.guardrail_rules
            )
        )
        policy = self._effective_pii_policy(resolved_policy)
        return has_response_guardrail or bool(
            self._pii_redactor is not None and policy.pii_redaction_enabled
        )

    @staticmethod
    def _guardrail_inspection_response(
        response: ChatCompletionResponse,
    ) -> ChatCompletionResponse:
        """Expose tool/function output to the existing text guardrail engine."""
        choices: list[dict] = []
        for choice in response.choices:
            message = choice.get("message")
            if not isinstance(message, dict):
                choices.append(choice)
                continue
            parts: list[str] = []
            content = message.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
            for key in ("tool_calls", "function_call"):
                value = message.get(key)
                if value is not None:
                    parts.append(
                        json.dumps(
                            value,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            default=str,
                        )
                    )
            choices.append({
                **choice,
                "message": {**message, "content": "\n".join(parts)},
            })
        return dataclasses.replace(response, choices=choices)

    async def _apply_output_policy(
        self,
        response: ChatCompletionResponse,
        *,
        project: Project | None,
        resolved_policy: ResolvedPolicy | None,
        pii_mapping: RedactionMapping | None,
    ) -> tuple[ChatCompletionResponse, bool, int, list[str]]:
        """Apply the same complete-output controls to every response path.

        Guardrails run on raw provider text. PII processing then re-injects only
        values supplied by this caller when policy allows it and redacts any new
        PII generated by the provider. A configured NER detector failure is
        raised so streaming callers can withhold the entire buffered response.
        """
        if project and project.guardrail_rules:
            result = await self.guardrail_engine.evaluate_response(
                self._guardrail_inspection_response(response),
                project.guardrail_rules,
            )
            if not result.passed:
                return (
                    self._replace_response_content(
                        response,
                        result.message or "Response blocked by guardrail.",
                    ),
                    True,
                    0,
                    [],
                )

        if self._pii_redactor is None:
            return response, False, 0, []

        policy = self._effective_pii_policy(resolved_policy)
        known_values = set()
        if pii_mapping is not None and policy.pii_reinject:
            known_values.update(pii_mapping._forward.values())

        output_count = 0
        output_types: set[str] = set()

        async def _process_text(value: str) -> str:
            nonlocal output_count
            if not value:
                return value
            if pii_mapping is not None and pii_mapping.redacted_count > 0:
                value = self._pii_redactor.reinject_response(
                    value, pii_mapping
                )
            if not policy.pii_redaction_enabled:
                return value

            scan_policy = dataclasses.replace(policy, pii_reinject=True)
            messages, output_mapping = (
                await self._pii_redactor.redact_messages_async(
                    [{"role": "assistant", "content": value}],
                    scan_policy,
                )
            )
            value = messages[0]["content"]
            if output_mapping.ner_error is not None:
                output_mapping._forward.clear()
                output_mapping._dedup.clear()
                raise RuntimeError("output PII detector unavailable")

            # Re-injection applies only to PII that came from this caller.
            # Provider-generated PII remains tokenized even in reversible
            # mode, preventing a model from introducing a new secret.
            for token, original in output_mapping._forward.items():
                if original in known_values:
                    value = value.replace(token, original)
                    continue
                output_count += 1
                token_type = token.removeprefix("[").rsplit("_", 1)[0]
                output_types.add(token_type.lower())
            output_mapping._forward.clear()
            output_mapping._dedup.clear()
            return value

        choices: list[dict] = []
        for choice in response.choices:
            message = choice.get("message")
            if not isinstance(message, dict):
                choices.append(choice)
                continue
            content = message.get("content")
            new_message = dict(message)
            if isinstance(content, str):
                new_message["content"] = await _process_text(content)

            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                new_calls: list[Any] = []
                for call in tool_calls:
                    if not isinstance(call, dict):
                        new_calls.append(call)
                        continue
                    function = call.get("function")
                    if not isinstance(function, dict):
                        new_calls.append(call)
                        continue
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        function = {
                            **function,
                            "arguments": await _process_text(arguments),
                        }
                    new_calls.append({**call, "function": function})
                new_message["tool_calls"] = new_calls

            function_call = message.get("function_call")
            if isinstance(function_call, dict):
                arguments = function_call.get("arguments")
                if isinstance(arguments, str):
                    new_message["function_call"] = {
                        **function_call,
                        "arguments": await _process_text(arguments),
                    }

            choices.append({
                **choice,
                "message": new_message,
            })

        return (
            dataclasses.replace(response, choices=choices),
            False,
            output_count,
            sorted(output_types),
        )

    async def _record_output_pii_redaction(
        self,
        *,
        req_ctx: RequestContext,
        request_id: str,
        count: int,
        redacted_types: list[str],
    ) -> None:
        if count <= 0:
            return
        if self._audit_trail is not None:
            await self._audit_trail.record_pii_redaction(
                user_id=req_ctx.user_id,
                project_id=req_ctx.project_id,
                request_id=request_id,
                redacted_types=redacted_types,
                count=count,
                tenant_id=req_ctx.tenant_id or "__legacy__",
            )
        if self._event_dispatcher is not None:
            await self._event_dispatcher.dispatch_pii_event(
                event_id=f"{request_id}:pii:output",
                user_id=req_ctx.user_id,
                project_id=req_ctx.project_id,
                redacted_types=redacted_types,
                count=count,
                tenant_id=req_ctx.tenant_id or "__legacy__",
            )

    async def _apply_cached_output_policy(
        self,
        response: ChatCompletionResponse,
        *,
        request: ChatCompletionRequest,
        req_ctx: RequestContext,
        request_id: str,
        project: Project | None,
        resolved_policy: ResolvedPolicy | None,
        pii_mapping: RedactionMapping | None,
    ) -> ChatCompletionResponse:
        """Re-evaluate cached output under current policy and audit the access."""
        response, _, output_pii_count, output_pii_types = (
            await self._apply_output_policy(
                response,
                project=project,
                resolved_policy=resolved_policy,
                pii_mapping=pii_mapping,
            )
        )
        await self._record_output_pii_redaction(
            req_ctx=req_ctx,
            request_id=request_id,
            count=output_pii_count,
            redacted_types=output_pii_types,
        )
        if self._audit_trail is not None:
            await self._audit_trail.record_llm_request(
                user_id=req_ctx.user_id,
                project_id=req_ctx.project_id,
                request_id=request_id,
                model=response.model,
                provider=response.provider,
                message_count=len(request.messages or []),
                pii_redacted_count=(
                    (pii_mapping.redacted_count if pii_mapping else 0)
                    + output_pii_count
                ),
                injection_score=0.0,
                tenant_id=req_ctx.tenant_id or "__legacy__",
            )
        return response

    @staticmethod
    def _response_text(response: ChatCompletionResponse) -> str:
        parts: list[str] = []
        for choice in response.choices:
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
        return " ".join(parts)

    @staticmethod
    def _stream_chunk_size(chunk: StreamChunk) -> int:
        payload = {
            "id": chunk.id,
            "choices": chunk.choices,
            "model": chunk.model,
        }
        return len(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )

    @classmethod
    def _bounded_simulated_streaming(
        cls,
        response: ChatCompletionResponse,
    ) -> Iterator[StreamChunk]:
        response_bytes = len(
            json.dumps(
                response.choices,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
        if response_bytes > _MAX_STREAM_OUTPUT_BYTES:
            raise RuntimeError("buffered stream output limit exceeded")
        emitted_bytes = 0
        for chunk in simulate_streaming(response):
            emitted_bytes += cls._stream_chunk_size(chunk)
            if emitted_bytes > _MAX_STREAM_OUTPUT_BYTES:
                raise RuntimeError("buffered stream output limit exceeded")
            yield chunk

    @staticmethod
    def _stream_chunk_accounting_text(chunk: StreamChunk) -> str:
        """Return visible text plus tool-call tokens for usage estimation."""
        parts: list[str] = []
        for choice in chunk.choices:
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
            raw_calls = delta.get("tool_calls")
            if not isinstance(raw_calls, list):
                continue
            for raw_call in raw_calls:
                if not isinstance(raw_call, dict):
                    continue
                function = raw_call.get("function")
                if not isinstance(function, dict):
                    continue
                name = function.get("name")
                arguments = function.get("arguments")
                if isinstance(name, str) and name:
                    parts.append(name)
                if isinstance(arguments, str) and arguments:
                    parts.append(arguments)
        return "".join(parts)

    @staticmethod
    def _stream_prompt_accounting_text(
        request: ChatCompletionRequest,
    ) -> str:
        payload: dict[str, Any] = {"messages": request.messages or []}
        if request.system is not None:
            payload["system"] = request.system
        if request.tools is not None:
            payload["tools"] = request.tools
        if request.tool_choice is not None:
            payload["tool_choice"] = request.tool_choice
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )

    @staticmethod
    def _merge_tool_call_delta(
        calls: dict[int, dict], raw_call: dict, position: int
    ) -> None:
        call_index = raw_call.get("index", position)
        if not isinstance(call_index, int):
            call_index = position
        call = calls.setdefault(
            call_index,
            {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            },
        )
        if raw_call.get("id"):
            call["id"] = raw_call["id"]
        if raw_call.get("type"):
            call["type"] = raw_call["type"]
        function = raw_call.get("function")
        if isinstance(function, dict):
            if function.get("name"):
                call["function"]["name"] += str(function["name"])
            if function.get("arguments"):
                call["function"]["arguments"] += str(function["arguments"])

    def _response_from_stream_chunks(
        self,
        chunks: list[StreamChunk],
        *,
        provider: str,
        fallback_model: str,
        usage: TokenUsage | None,
    ) -> ChatCompletionResponse:
        """Reconstruct a checked response from withheld provider chunks."""
        states: dict[int, dict[str, Any]] = {}
        response_id = ""
        response_model = fallback_model

        for chunk in chunks:
            response_id = chunk.id or response_id
            response_model = chunk.model or response_model
            for position, choice in enumerate(chunk.choices):
                index = choice.get("index", position)
                if not isinstance(index, int):
                    index = position
                state = states.setdefault(
                    index,
                    {
                        "content": [],
                        "role": "assistant",
                        "finish_reason": None,
                        "tool_calls": {},
                    },
                )
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        state["content"].append(content)
                    if isinstance(delta.get("role"), str):
                        state["role"] = delta["role"]
                    raw_calls = delta.get("tool_calls")
                    if isinstance(raw_calls, list):
                        for call_position, raw_call in enumerate(raw_calls):
                            if isinstance(raw_call, dict):
                                self._merge_tool_call_delta(
                                    state["tool_calls"],
                                    raw_call,
                                    call_position,
                                )
                if choice.get("finish_reason") is not None:
                    state["finish_reason"] = choice["finish_reason"]

        choices: list[dict] = []
        for index, state in sorted(states.items()):
            message: dict[str, Any] = {
                "role": state["role"],
                "content": "".join(state["content"]),
            }
            if state["tool_calls"]:
                message["tool_calls"] = [
                    state["tool_calls"][call_index]
                    for call_index in sorted(state["tool_calls"])
                ]
            choices.append({
                "index": index,
                "message": message,
                "finish_reason": state["finish_reason"],
            })

        if not choices:
            choices = [{
                "index": 0,
                "message": {"role": "assistant", "content": ""},
                "finish_reason": None,
            }]

        return ChatCompletionResponse(
            id=response_id,
            choices=choices,
            usage=usage or TokenUsage(0, 0, 0),
            model=response_model,
            provider=provider,
        )

    async def _stream_true(
        self,
        request: ChatCompletionRequest,
        context: dict,
        req_ctx: RequestContext,
        prompt_caching_enabled: bool,
        rate_limit_headers: dict[str, str] | None,
        resolved_policy: ResolvedPolicy | None,
        pii_mapping: RedactionMapping | None,
        request_id: str,
        request_start: float,
        *,
        preferred_provider: str | None,
        effective_allowed: list[str] | None,
        smart_routing_decision: Any = None,
        spoke: Any = None,
        budget_reservation: BudgetReservation | None = None,
    ) -> AsyncIterator[dict]:
        """Real end-to-end streaming with policy-aware release.

        Opens one provider SSE stream directly. Policy-free output is relayed
        immediately; configured response guardrails or PII controls require a
        bounded full-response buffer so cross-chunk matches can be evaluated
        before any provider text is released. Provider fallback applies only
        while opening the stream. Usage, audit, trace, and quota accounting run
        on success, provider failure, and cancellation.
        """
        factory = (
            self.provider_fn_factory
            if self.provider_fn_factory is not None
            else getattr(self._routing_runtime, "provider_factory", None)
        )
        assert factory is not None

        # Access-list enforcement (execute_with_fallback did this for the
        # blocking path; smart routing already filtered by effective_allowed).
        if (
            smart_routing_decision is None
            and effective_allowed is not None
            and request.model not in effective_allowed
        ):
            await self._release_request_budget(
                budget_reservation,
                req_ctx=req_ctx,
            )
            yield {"data": {"error": {"type": "forbidden",
                    "message": f"Model '{request.model}' is not allowed",
                    "code": "model_not_allowed"}}}
            yield {"data": "[DONE]"}
            return

        # Classified once here, not in _finalize_stream: this is the only scope
        # holding the smart decision, and both of that method's call sites (the
        # buffered fallback and the relay's finally) must stamp the same value.
        task_type = self._classify_for_usage(
            extract_last_user_prompt(request.messages), smart_routing_decision
        )

        # --- Open the stream, falling back across providers pre-first-byte ---
        stream = None
        chosen = None
        open_errors: list[dict] = []
        if self._routing_runtime is not None:
            opened = await self._routing_runtime.open_stream(
                request,
                preferred_provider=preferred_provider,
                allowed_models=effective_allowed,
                prompt_caching_enabled=prompt_caching_enabled,
                spoke=spoke,
            )
            open_errors = list(opened.attempts)
            if isinstance(opened, OpenedProviderStream):
                stream = opened.stream
                chosen = opened.mapping
                first_chunk = opened.first_chunk
        else:
            chain = self._resolve_stream_chain(
                request.model,
                preferred_provider,
            )
            for mapping in chain:
                if not self.router.health_tracker.is_healthy(mapping.provider):
                    open_errors.append(
                        {
                            "provider": mapping.provider,
                            "message": "skipped (unhealthy)",
                        }
                    )
                    continue
                try:
                    if callable(
                        getattr(type(factory), "execute_streaming", None)
                    ):
                        candidate = factory.execute_streaming(
                            request,
                            mapping,
                            prompt_caching_enabled=prompt_caching_enabled,
                            spoke=spoke,
                        )
                    else:
                        adapter = factory._adapter_registry.get(
                            mapping.provider
                        )
                        config = factory.config_for(
                            mapping.provider,
                            spoke,
                        )
                        if adapter is None or config is None:
                            open_errors.append(
                                {
                                    "provider": mapping.provider,
                                    "message": "no adapter/config",
                                }
                            )
                            continue
                        candidate = factory._http_client.execute_streaming(
                            request,
                            mapping,
                            adapter,
                            config,
                            prompt_caching_enabled=(
                                prompt_caching_enabled
                            ),
                        )
                    # Prime the generator to surface a pre-stream provider error
                    # here, so fallback remains possible before client bytes.
                    first_chunk = await candidate.__anext__()
                    stream, chosen = candidate, mapping
                    break
                except StopAsyncIteration:
                    stream, chosen, first_chunk = candidate, mapping, None
                    break
                except Exception as exc:  # noqa: BLE001
                    diagnostic: dict[str, object] = {
                        "provider": mapping.provider,
                        "error_type": type(exc).__name__,
                    }
                    status_code = getattr(exc, "status_code", None)
                    if isinstance(status_code, int):
                        diagnostic["status_code"] = status_code
                    open_errors.append(diagnostic)
                    if (
                        getattr(exc, "provider_unavailable", None)
                        is not False
                    ):
                        self.router.health_tracker.mark_unhealthy(
                            mapping.provider,
                            getattr(
                                self.router,
                                "cooldown_seconds",
                                60,
                            ),
                        )
                    continue

        if chosen is None:
            # No candidate opened a native SSE stream. Run the normal provider
            # call and emit normalized simulated chunks from the complete
            # response. Only fail if that fallback cannot run either.
            logger.debug("true-streaming unavailable (%s); falling back to buffered "
                         "simulate-stream for model=%s", open_errors, request.model)
            try:
                buffered_request = dataclasses.replace(
                    request,
                    stream=False,
                )
                if self._routing_runtime is not None:
                    response = await self._routing_runtime.complete(
                        buffered_request,
                        preferred_provider=preferred_provider,
                        allowed_models=effective_allowed,
                        prompt_caching_enabled=prompt_caching_enabled,
                        spoke=spoke,
                    )
                else:
                    provider_fn = factory.create(
                        buffered_request,
                        prompt_caching_enabled=prompt_caching_enabled,
                        spoke=spoke,
                    )
                    response = await self.router.execute_with_fallback(
                        buffered_request,
                        provider_fn,
                        preferred_provider=preferred_provider,
                        allowed_models=effective_allowed,
                    )
            except Exception:  # noqa: BLE001 — genuine provider failure
                logger.warning(
                    "buffered stream fallback failed req=%s model=%s",
                    request_id,
                    request.model,
                    exc_info=True,
                )
                await self._release_request_budget(
                    budget_reservation,
                    req_ctx=req_ctx,
                )
                yield {"data": {"error": {"type": "provider_error",
                        "message": "The provider request failed.",
                        "code": "all_providers_exhausted"}}}
                yield {"data": "[DONE]"}
                return

            raw_text = self._response_text(response)
            try:
                (
                    response,
                    _response_blocked,
                    output_pii_count,
                    output_pii_types,
                ) = await self._apply_output_policy(
                    response,
                    project=self._project_for_context(req_ctx),
                    resolved_policy=resolved_policy,
                    pii_mapping=pii_mapping,
                )
                await self._record_output_pii_redaction(
                    req_ctx=req_ctx,
                    request_id=request_id,
                    count=output_pii_count,
                    redacted_types=output_pii_types,
                )
            except Exception:  # noqa: BLE001 — fail closed before any content
                logger.exception(
                    "buffered stream output policy failed req=%s", request_id
                )
                await self._finalize_stream_safely(
                    request=request,
                    req_ctx=req_ctx,
                    request_id=request_id,
                    request_start=request_start,
                    resolved_policy=resolved_policy,
                    pii_mapping=pii_mapping,
                    provider=response.provider,
                    model=request.model,
                    response_model=self._response_billing_model(response),
                    text=raw_text,
                    usage=response.usage,
                    status="error",
                    task_type=task_type,
                    provider_request_id=response.id or "",
                    budget_reservation=budget_reservation,
                )
                yield {"data": {"error": {
                    "type": "output_policy_error",
                    "message": "The response was withheld because output policy evaluation failed.",
                    "code": "output_policy_failed",
                }}}
                yield {"data": "[DONE]"}
                return

            stream_status = "success"
            client_error_emitted = False
            finalization_ok = True
            try:
                if rate_limit_headers:
                    yield {"_rate_limit_headers": rate_limit_headers}
                for chunk in self._bounded_simulated_streaming(response):
                    yield {
                        "data": self._chunk_to_dict(
                            chunk,
                            provider=response.provider,
                        )
                    }
            except asyncio.CancelledError:
                stream_status = "cancelled"
                raise
            except GeneratorExit:
                stream_status = "cancelled"
                raise
            except Exception:  # noqa: BLE001 — do not expose internals
                stream_status = "error"
                client_error_emitted = True
                logger.warning(
                    "buffered response streaming failed req=%s",
                    request_id,
                    exc_info=True,
                )
                yield {"data": {"error": {
                    "type": "stream_error",
                    "message": "The response stream failed.",
                    "code": "response_stream_failed",
                }}}
            finally:
                finalization_ok = await self._finalize_stream_safely(
                    request=request,
                    req_ctx=req_ctx,
                    request_id=request_id,
                    request_start=request_start,
                    resolved_policy=resolved_policy,
                    pii_mapping=pii_mapping,
                    provider=response.provider,
                    model=request.model,
                    response_model=self._response_billing_model(response),
                    text=raw_text,
                    usage=response.usage,
                    status=stream_status,
                    task_type=task_type,
                    provider_request_id=response.id or "",
                    output_pii_count=output_pii_count,
                    budget_reservation=budget_reservation,
                )
            if not finalization_ok and not client_error_emitted:
                yield {"data": {"error": {
                    "type": "stream_error",
                    "message": "The response accounting could not be completed.",
                    "code": "stream_finalization_failed",
                }}}
            yield {"data": "[DONE]"}
            return

        # --- Relay or policy-buffer chunks; always accumulate for accounting ---
        project = self._project_for_context(req_ctx)
        buffer_for_policy = self._requires_output_buffering(
            project=project,
            resolved_policy=resolved_policy,
        )
        pii_buffer: dict = {"pending": ""}
        buffered_chunks: list[StreamChunk] = []
        buffered_bytes = 0
        stream_output_bytes = 0
        accumulated: list[str] = []
        final_usage: TokenUsage | None = None
        stream_status = "success"
        response_model = chosen.model_id
        output_pii_count = 0
        failure_type = "provider"
        client_error_emitted = False
        finalization_ok = True
        provider_request_id = ""

        def _consume(chunk: StreamChunk) -> dict | None:
            nonlocal final_usage, buffered_bytes, provider_request_id
            nonlocal stream_output_bytes
            stream_output_bytes += self._stream_chunk_size(chunk)
            if stream_output_bytes > _MAX_STREAM_OUTPUT_BYTES:
                raise RuntimeError("provider stream output limit exceeded")
            if chunk.id and (not provider_request_id or chunk.is_final):
                provider_request_id = chunk.id
            if chunk.usage is not None:
                final_usage = self._merge_stream_usage(final_usage, chunk.usage)
            accounting_text = self._stream_chunk_accounting_text(chunk)
            if accounting_text:
                accumulated.append(accounting_text)

            if buffer_for_policy:
                buffered_bytes += self._stream_chunk_size(chunk)
                if buffered_bytes > _MAX_POLICY_BUFFER_BYTES:
                    raise RuntimeError("output policy buffer limit exceeded")
                buffered_chunks.append(chunk)
                return None

            # A usage-only trailing chunk (OpenAI include_usage) has empty
            # choices — capture its usage above but don't emit an empty SSE chunk.
            if not chunk.choices:
                return None
            chunk_dict = self._chunk_to_dict(
                chunk,
                provider=chosen.provider,
            )
            chunk_dict["model"] = request.model
            if pii_mapping and pii_mapping.redacted_count > 0:
                chunk_dict = self._reinject_chunk_pii(chunk_dict, pii_mapping, pii_buffer)
            return {"data": chunk_dict}

        try:
            if rate_limit_headers:
                yield {"_rate_limit_headers": rate_limit_headers}

            if first_chunk is not None:
                out = _consume(first_chunk)
                if out is not None:
                    yield out

            # Keep consuming to the end of the SSE stream. We do NOT stop on
            # is_final: providers that report usage in-stream (OpenAI) send the
            # usage chunk AFTER the finish_reason chunk.
            async for chunk in stream:
                out = _consume(chunk)
                if out is not None:
                    yield out

            if buffer_for_policy:
                failure_type = "output_policy"
                buffered_response = self._response_from_stream_chunks(
                    buffered_chunks,
                    provider=chosen.provider,
                    fallback_model=response_model,
                    usage=final_usage,
                )
                buffered_response.provider_model = response_model
                buffered_response.model = request.model
                (
                    buffered_response,
                    _response_blocked,
                    output_pii_count,
                    output_pii_types,
                ) = await self._apply_output_policy(
                    buffered_response,
                    project=project,
                    resolved_policy=resolved_policy,
                    pii_mapping=pii_mapping,
                )
                await self._record_output_pii_redaction(
                    req_ctx=req_ctx,
                    request_id=request_id,
                    count=output_pii_count,
                    redacted_types=output_pii_types,
                )
                for approved_chunk in self._bounded_simulated_streaming(
                    buffered_response
                ):
                    yield {
                        "data": self._chunk_to_dict(
                            approved_chunk,
                            provider=chosen.provider,
                        )
                    }
        except asyncio.CancelledError:
            stream_status = "cancelled"
            raise
        except GeneratorExit:
            stream_status = "cancelled"
            raise
        except Exception:  # noqa: BLE001 — after first byte, no fallback
            stream_status = "error"
            client_error_emitted = True
            logger.warning(
                "%s stream failure req=%s provider=%s",
                failure_type,
                request_id,
                chosen.provider,
                exc_info=True,
            )
            if failure_type == "output_policy":
                yield {"data": {"error": {
                    "type": "output_policy_error",
                    "message": "The response was withheld because output policy evaluation failed.",
                    "code": "output_policy_failed",
                }}}
            else:
                yield {"data": {"error": {
                    "type": "stream_error",
                    "message": "The provider stream failed.",
                    "code": "provider_stream_failed",
                }}}
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001 — cleanup cannot replace result
                    logger.debug(
                        "provider stream close failed req=%s", request_id,
                        exc_info=True,
                    )
            finalization_ok = await self._finalize_stream_safely(
                request=request, req_ctx=req_ctx, request_id=request_id,
                request_start=request_start, resolved_policy=resolved_policy,
                pii_mapping=pii_mapping, provider=chosen.provider,
                model=request.model, response_model=response_model,
                text="".join(accumulated), usage=final_usage, status=stream_status,
                task_type=task_type,
                provider_request_id=provider_request_id,
                output_pii_count=output_pii_count,
                budget_reservation=budget_reservation,
            )
        if not finalization_ok and not client_error_emitted:
            yield {"data": {"error": {
                "type": "stream_error",
                "message": "The response accounting could not be completed.",
                "code": "stream_finalization_failed",
            }}}
        yield {"data": "[DONE]"}

    def _merge_stream_usage(
        self, current: TokenUsage | None, incoming: TokenUsage,
    ) -> TokenUsage:
        """Merge usage across stream events (Anthropic splits input/output).

        Takes the max of each field so a later event's cumulative counts win,
        while a field only one event reports (input on message_start, output on
        message_delta) is preserved.
        """
        if current is None:
            return incoming
        prompt = max(current.prompt_tokens, incoming.prompt_tokens)
        completion = max(current.completion_tokens, incoming.completion_tokens)
        return TokenUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            cached_tokens=max(current.cached_tokens, incoming.cached_tokens),
            cache_creation_tokens=max(
                current.cache_creation_tokens, incoming.cache_creation_tokens),
        )

    async def _finalize_stream(
        self, *, request, req_ctx, request_id, request_start, resolved_policy,
        pii_mapping, provider, model, response_model, text, usage, status,
        task_type: str = "", provider_request_id: str = "",
        output_pii_count: int = 0,
        budget_reservation: BudgetReservation | None = None,
    ) -> None:
        """End-of-stream accounting: usage, cost, audit, trace, OTLP, quota.

        Mirrors the non-streaming steps 11.6/12 exactly, but on the accumulated
        stream result. When the provider didn't report usage, estimate tokens
        from the prompt + accumulated text via tiktoken (flagged approximate).
        """
        approximate = usage is None
        if usage is None:
            prompt_text = self._stream_prompt_accounting_text(request)
            try:
                prompt_tokens = await self.cost_tracker.estimate_tokens(
                    prompt_text,
                    response_model,
                )
                completion_tokens = await self.cost_tracker.estimate_tokens(
                    text,
                    response_model,
                )
            except Exception:  # noqa: BLE001 — estimation must never fail the stream
                prompt_tokens = completion_tokens = 0
            usage = TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            )

        # Cost tracking (step 12 parity)
        cost = self.cost_tracker.calculate_cost(
            provider,
            response_model,
            usage.prompt_tokens,
            usage.completion_tokens,
            cached_tokens=usage.cached_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
        )
        latency_ms = (time.perf_counter() - request_start) * 1000
        usage_record = UsageRecord(
            request_id=request_id, project_id=req_ctx.project_id, user_id=req_ctx.user_id,
            tenant_id=req_ctx.tenant_id,
            provider=provider, model=model,
            prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens, cost=cost,
            timestamp=datetime.now(timezone.utc),
            cached_tokens=usage.cached_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            latency_ms=latency_ms, status=status, routing_strategy="stream",
            # Passed in from _stream_true, which is the only scope that knows
            # whether smart routing already classified this request.
            task_type=task_type,
            provider_request_id=provider_request_id,
        )
        reservation_totals = await self._finalize_request_budget(
            budget_reservation,
            actual_cost=cost,
            req_ctx=req_ctx,
        )
        await self.cost_tracker.record_usage(
            usage_record,
            skip_shared_scopes=(
                self._reserved_cost_tracker_scopes(
                    budget_reservation
                )
            ),
        )
        if reservation_totals is None:
            raise RuntimeError("budget reservation finalization failed")
        if reservation_totals:
            self.cost_tracker.adopt_reserved_spend(
                usage_record,
                reservation_totals,
            )

        if self._trace_forwarder is not None:
            await self._trace_forwarder.forward(usage_record)
        if self._otlp_exporter is not None and not (
            self._trace_forwarder is not None and self._trace_forwarder.enabled
        ):
            self._otlp_exporter.export_usage(usage_record)
        if (
            self._quota_enforcer is not None
            and not self._reservation_has_scope(
                budget_reservation,
                "quota",
            )
        ):
            budget_limit = resolved_policy.budget_limit if resolved_policy else None
            await self._quota_enforcer.record_spend(
                req_ctx.project_id,
                cost,
                budget_limit=budget_limit,
                tenant_id=req_ctx.tenant_id,
            )

        # Audit after spend and usage are durable enough to retry independently.
        if self._audit_trail is not None:
            await self._audit_trail.record_llm_request(
                user_id=req_ctx.user_id,
                project_id=req_ctx.project_id,
                request_id=request_id,
                model=model,
                provider=provider,
                message_count=len(request.messages or []),
                pii_redacted_count=(
                    (pii_mapping.redacted_count if pii_mapping else 0)
                    + output_pii_count
                ),
                injection_score=0.0,
                tenant_id=req_ctx.tenant_id or "__legacy__",
            )
        if approximate:
            logger.debug("stream usage estimated (provider reported none) req=%s", request_id)

    async def _finalize_stream_safely(self, **kwargs: Any) -> bool:
        """Contain accounting failures so streams terminate in protocol order."""
        try:
            await self._finalize_stream(**kwargs)
            return True
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception:  # noqa: BLE001 — client receives a stable error event
            logger.exception(
                "stream finalization failed req=%s",
                kwargs.get("request_id", ""),
            )
            return False

    async def _stream_response(
        self,
        response: ChatCompletionResponse,
        budget_status: BudgetStatus | None = None,
        rate_limit_headers: dict[str, str] | None = None,
        pii_mapping: RedactionMapping | None = None,
    ) -> AsyncIterator[dict]:
        """Async generator that yields SSE-formatted chunks.

        Uses simulated streaming (chunking the complete response) since the
        provider call already returned a full response.
        """
        # Yield rate limit headers as first metadata chunk if present
        if rate_limit_headers:
            yield {"_rate_limit_headers": rate_limit_headers}
        try:
            pii_buffer: dict = {"pending": ""}
            chunks = self._bounded_simulated_streaming(response)
            for chunk in chunks:
                chunk_dict = self._chunk_to_dict(
                    chunk,
                    provider=response.provider,
                )
                if pii_mapping and pii_mapping.redacted_count > 0:
                    chunk_dict = self._reinject_chunk_pii(chunk_dict, pii_mapping, pii_buffer)
                yield {"data": chunk_dict}
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception:
            logger.warning("buffered response streaming failed", exc_info=True)
            yield {"data": {"error": {
                "type": "stream_error",
                "message": "The response stream failed.",
                "code": "response_stream_failed",
            }}}

        done_data: dict[str, Any] = {"data": "[DONE]"}
        if budget_status and budget_status.is_over_budget:
            done_data["budget_exceeded"] = True
        yield done_data

    async def _stream_ensemble_response(
        self,
        request: ChatCompletionRequest,
        prompt: str,
        preset: EnsemblePreset,
        effective_allowed: list[str] | None,
        project_id: str,
        user_id: str,
        per_call: float,
        rate_limit_headers: dict[str, str] | None = None,
        resolved_policy: ResolvedPolicy | None = None,
        pii_mapping: RedactionMapping | None = None,
        request_id: str = "",
        authorized_project: Project | None = None,
        tenant_id: str | None = None,
        budget_reservation: BudgetReservation | None = None,
    ) -> AsyncIterator[dict]:
        """Async generator for streaming ensemble requests.

        The panel phase cannot be streamed (Req 10.1): all output is withheld
        until ``ensemble_route`` completes the scatter-gather-synthesize flow
        and returns the final response. ``ensemble_route`` already produces the
        judge's synthesized output when quorum is met, or the highest-ranked
        survivor when the best-single fallback applies, so we stream only that
        final response (Req 10.2, 10.3). When the quorum is not met under an
        error policy (or there are zero survivors), the stream terminates with
        an error chunk and no synthesized content (Req 10.4).
        """
        try:
            response, _decision = await self.router.ensemble_route(
                request,
                self.provider_fn_factory,
                prompt,
                preset,
                allowed_models=effective_allowed,
                project_id=project_id,
                user_id=user_id,
                tenant_id=tenant_id,
                per_call_cost_estimate=per_call,
                skip_shared_scopes=(
                    self._reserved_cost_tracker_scopes(
                        budget_reservation
                    )
                ),
            )
        except (
            EnsembleAccessError,
            EnsembleCostCeilingError,
            EnsembleNoSurvivorsError,
            EnsembleQuorumError,
            EnsembleSynthesisError,
        ) as exc:
            # Pre-dispatch rejection (access / cost ceiling), below quorum
            # under error policy, 0 survivors, or judge failure: terminate the
            # stream with an error chunk and no synthesized content (Req 10.4).
            logger.warning(
                "ensemble stream failed req=%s project=%s",
                request_id,
                project_id,
                exc_info=True,
            )
            decision = getattr(exc, "decision", None)
            if decision is None:
                await self._release_request_budget(
                    budget_reservation,
                    req_ctx=RequestContext(
                        user_id=user_id,
                        project_id=project_id,
                        roles=[],
                        scopes=[],
                        tenant_id=tenant_id,
                        authorized_project=authorized_project,
                    ),
                )
            else:
                failed_ctx = RequestContext(
                    user_id=user_id,
                    project_id=project_id,
                    roles=[],
                    scopes=[],
                    tenant_id=tenant_id,
                    authorized_project=authorized_project,
                )
                totals = await self._finalize_request_budget(
                    budget_reservation,
                    actual_cost=decision.total_cost,
                    req_ctx=failed_ctx,
                )
                if totals and "user" in totals:
                    self.cost_tracker.adopt_user_spend(
                        user_id,
                        totals["user"],
                        tenant_id=tenant_id,
                    )
            if rate_limit_headers:
                yield {"_rate_limit_headers": rate_limit_headers}
            yield {"data": {"error": {
                "type": "ensemble_error",
                "message": "The ensemble request failed.",
                "code": "ensemble_failed",
            }}}
            yield {"data": "[DONE]"}
            return

        req_ctx = RequestContext(
            user_id=user_id,
            project_id=project_id,
            roles=[],
            scopes=[],
            tenant_id=tenant_id,
            authorized_project=authorized_project,
        )
        reservation_totals = await self._finalize_request_budget(
            budget_reservation,
            actual_cost=_decision.total_cost,
            req_ctx=req_ctx,
        )
        if reservation_totals is None:
            yield {"data": {"error": {
                "type": "service_unavailable",
                "message": "Budget accounting could not be finalized.",
                "code": "budget_finalization_failed",
            }}}
            yield {"data": "[DONE]"}
            return
        if "user" in reservation_totals:
            self.cost_tracker.adopt_user_spend(
                user_id,
                reservation_totals["user"],
                tenant_id=tenant_id,
            )
        if rate_limit_headers:
            yield {"_rate_limit_headers": rate_limit_headers}
        try:
            (
                response,
                _response_blocked,
                output_pii_count,
                output_pii_types,
            ) = await self._apply_output_policy(
                response,
                project=self._project_for_context(req_ctx),
                resolved_policy=resolved_policy,
                pii_mapping=pii_mapping,
            )
            await self._record_output_pii_redaction(
                req_ctx=req_ctx,
                request_id=request_id,
                count=output_pii_count,
                redacted_types=output_pii_types,
            )
            if self._audit_trail is not None:
                await self._audit_trail.record_llm_request(
                    user_id=user_id,
                    project_id=project_id,
                    request_id=request_id,
                    model=response.model,
                    provider=response.provider,
                    message_count=len(request.messages or []),
                    pii_redacted_count=(
                        (pii_mapping.redacted_count if pii_mapping else 0)
                        + output_pii_count
                    ),
                    injection_score=0.0,
                    tenant_id=tenant_id or "__legacy__",
                )
        except Exception:  # noqa: BLE001 — no unapproved content has been sent
            logger.exception(
                "ensemble output policy failed req=%s project=%s",
                request_id,
                project_id,
            )
            yield {"data": {"error": {
                "type": "output_policy_error",
                "message": "The response was withheld because output policy evaluation failed.",
                "code": "output_policy_failed",
            }}}
            yield {"data": "[DONE]"}
            return

        # Quorum met (judge output) or best-single survivor: stream only the
        # final response incrementally via simulated streaming. Panel/survivor
        # responses are never streamed (Req 10.2, 10.3).
        try:
            chunks = self._bounded_simulated_streaming(response)
            for chunk in chunks:
                yield {
                    "data": self._chunk_to_dict(
                        chunk,
                        provider=response.provider,
                    )
                }
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception:
            logger.warning(
                "ensemble response streaming failed req=%s", request_id,
                exc_info=True,
            )
            yield {"data": {"error": {
                "type": "stream_error",
                "message": "The response stream failed.",
                "code": "response_stream_failed",
            }}}
        yield {"data": "[DONE]"}

    async def _stream_response_real(
        self,
        request: ChatCompletionRequest,
        response: ChatCompletionResponse,
        budget_status: BudgetStatus | None = None,
        rate_limit_headers: dict[str, str] | None = None,
        prompt_caching_enabled: bool = False,
        pii_mapping: RedactionMapping | None = None,
    ) -> AsyncIterator[dict]:
        """Replay an already checked response for the legacy streaming path.

        Real provider streaming is opened by ``_stream_true`` before any blocking
        call. Reopening a second provider stream here would double bill and expose
        output different from the response that passed guardrails.
        """
        async for chunk_dict in self._stream_response(
            response,
            budget_status,
            rate_limit_headers,
        ):
            yield chunk_dict

    # ------------------------------------------------------------------
    # Smart Routing
    # ------------------------------------------------------------------

    def _is_smart_routing_request(self, request: ChatCompletionRequest, context: dict) -> bool:
        """Detect when smart routing should be used.

        Returns True when:
        - The context has "smart_routing": True flag (from Routing Explorer), OR
        - The request model is empty/not specified (auto-select mode)

        Also requires that smart routing is actually configured on the router.
        """
        if not hasattr(self.router, '_smart_strategy') or self.router._smart_strategy is None:
            return False
        if context.get("smart_routing") is True:
            return True
        if not request.model or request.model.strip() == "":
            return True
        return False

    def _classify_for_usage(self, prompt: str, smart_routing_decision: Any = None) -> str:
        """Task type to stamp on a :class:`UsageRecord`, or ``""`` if unavailable.

        Reuses the smart-routing decision's classification when there is one — the
        classifier already ran for that request, and re-running it could disagree
        with the model that was actually selected.

        Falls back to classifying the prompt directly, because most requests never
        go through smart routing: without this, per-user task aggregates would
        describe only the auto-select minority and silently omit everyone else.
        The cost is ~0.13 ms on a typical prompt, against a network round trip.

        Never raises. This runs after the response is in hand, so a classifier
        failure must not turn a served request into an error — an empty string
        loses one record's worth of reporting detail, which is the cheaper failure.
        """
        if smart_routing_decision is not None:
            task_type = getattr(smart_routing_decision, "task_type", "")
            if task_type:
                return str(task_type)

        if not prompt:
            return ""
        strategy = getattr(self.router, "_smart_strategy", None)
        classifier = getattr(strategy, "classifier", None) if strategy else None
        if classifier is None:
            return ""
        try:
            return str(classifier.classify(prompt).task_type)
        except Exception:
            logger.debug("Task classification failed for usage record", exc_info=True)
            return ""

    def _is_ensemble_request(
        self, request: ChatCompletionRequest, context: dict
    ) -> tuple[bool, str | None, str | None]:
        """Detect when ensemble routing should be used.

        Returns ``(is_ensemble, preset_name, error)`` where:
        - ``is_ensemble`` is True when the request targets ensemble routing
        - ``preset_name`` is the named preset, or None for the default preset
        - ``error`` describes a malformed invocation (e.g. missing preset name)

        Triggers on ``model == "ensemble"`` (default preset),
        ``model == "ensemble:<name>"`` (named preset), and
        ``context["ensemble"] is True`` (default preset). The model value takes
        precedence over the context flag. Returns ``(False, None, None)`` when
        ensemble routing is not configured on the router.
        """
        if getattr(self.router, "_ensemble_config", None) is None:
            return (False, None, None)
        model = request.model or ""
        if model == "ensemble":
            return (True, None, None)            # default preset
        if model.startswith("ensemble:"):
            name = model[len("ensemble:"):]
            if name == "":
                return (True, None, "missing preset name")
            return (True, name, None)
        if context.get("ensemble") is True:
            return (True, None, None)
        return (False, None, None)

    def _estimate_per_call_cost(
        self, request: ChatCompletionRequest, preset: EnsemblePreset
    ) -> float:
        """Conservative per-call cost estimate for the ensemble budget pre-check.

        Estimates a single underlying call's cost from the request's prompt size
        and a nominal completion-token count, priced using the judge model's
        resolved provider pricing. The caller multiplies this by ``(N + 1)`` to
        bound the total ensemble cost before any dispatch.

        Returns ``0.0`` when pricing cannot be resolved, in which case the
        budget pre-check becomes a no-op beyond the existing budget checks.
        """
        # Estimate prompt tokens from message content length (~4 chars/token).
        prompt_chars = 0
        for msg in request.messages or []:
            if isinstance(msg, dict):
                content = msg.get("content", "")
                if isinstance(content, str):
                    prompt_chars += len(content)
        estimated_prompt_tokens = max(1, prompt_chars // 4)
        # Nominal completion budget for the estimate.
        estimated_completion_tokens = request.max_tokens or 256

        return self._estimate_models_cost(
            {preset.judge},
            prompt_tokens=estimated_prompt_tokens,
            completion_tokens=estimated_completion_tokens,
        )

    def _estimate_ensemble_cost(
        self,
        request: ChatCompletionRequest,
        preset: EnsemblePreset,
    ) -> float:
        """Bound panel calls plus the larger synthesis prompt conservatively."""
        prompt_chars = sum(
            len(content)
            for message in (request.messages or [])
            if isinstance(message, dict)
            and isinstance((content := message.get("content", "")), str)
        )
        prompt_tokens = max(1, prompt_chars // 4)
        completion_tokens = request.max_tokens or 256
        panel_total = sum(
            self._estimate_models_cost(
                {model},
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            for model in preset.panel
        )
        # The judge sees the original prompt plus every successful panel output.
        judge_prompt_tokens = (
            prompt_tokens + len(preset.panel) * completion_tokens
        )
        judge_cost = self._estimate_models_cost(
            {preset.judge},
            prompt_tokens=judge_prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return panel_total + judge_cost

    @staticmethod
    def _ensemble_metadata(decision) -> dict:
        """Build the ``ensemble`` metadata block from an ``EnsembleDecision``."""
        return {
            "preset": decision.preset_name,
            "panel": decision.panel_members,
            "judge": decision.judge_model,
            "succeeded": decision.succeeded,
            "failed": [
                {
                    "model": failure.get("model"),
                    "reason": _public_ensemble_failure_reason(
                        failure.get("reason")
                    ),
                }
                for failure in decision.failed
            ],
            "quorum_met": decision.quorum_met,
            "succeeded_count": decision.succeeded_count,
            "quorum_threshold": decision.quorum_threshold,
            "total_cost": decision.total_cost,
            "cost_multiplier": decision.cost_multiplier,
            "fallback_used": decision.fallback_used,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _reserve_request_budget(
        self,
        *,
        request_id: str,
        req_ctx: RequestContext,
        estimated_cost: float,
        project_budget: BudgetStatus | None,
        user_budget: BudgetStatus,
        resolved_policy: ResolvedPolicy | None,
    ) -> QuotaDecision:
        """Reserve estimated cost against the strictest project and user caps."""
        project_limits = [
            limit
            for limit in (
                project_budget.budget_limit if project_budget else None,
                resolved_policy.budget_limit if resolved_policy else None,
            )
            if limit is not None
        ]
        project_limit = min(project_limits) if project_limits else None
        user_limit = user_budget.budget_limit
        if project_limit is None and user_limit is None:
            return QuotaDecision(allowed=True)
        if estimated_cost <= 0:
            return QuotaDecision(
                allowed=False,
                reason=(
                    "Budgeted requests require a positive, priced cost "
                    "estimate."
                ),
                limit_type="budget_estimate_unavailable",
                status_code=503,
            )
        if self._quota_enforcer is None:
            if (
                project_limit is not None
                and (project_budget.current_spend if project_budget else 0.0)
                + estimated_cost
                > project_limit
            ):
                return QuotaDecision(
                    allowed=False,
                    reason="Project budget limit would be exceeded.",
                    limit_type="budget_limit",
                    limit_value=project_limit,
                    current_value=(
                        project_budget.current_spend
                        if project_budget
                        else 0.0
                    ),
                    error_code="budget_exceeded",
                )
            if (
                user_limit is not None
                and user_budget.current_spend + estimated_cost > user_limit
            ):
                return QuotaDecision(
                    allowed=False,
                    reason="User budget limit would be exceeded.",
                    limit_type="user_budget_limit",
                    limit_value=user_limit,
                    current_value=user_budget.current_spend,
                    error_code="budget_exceeded",
                )
            return QuotaDecision(allowed=True)
        return await self._quota_enforcer.reserve_budget(
            request_id=request_id,
            project_id=req_ctx.project_id,
            user_id=req_ctx.user_id,
            estimated_cost=estimated_cost,
            project_budget_limit=project_limit,
            user_budget_limit=user_limit,
            tenant_id=req_ctx.tenant_id,
            project_current_spend=(
                project_budget.current_spend if project_budget else 0.0
            ),
            user_current_spend=user_budget.current_spend,
        )

    @staticmethod
    def _request_has_budget(
        project_budget: BudgetStatus | None,
        user_budget: BudgetStatus,
        resolved_policy: ResolvedPolicy | None,
    ) -> bool:
        return any(
            limit is not None
            for limit in (
                project_budget.budget_limit if project_budget else None,
                user_budget.budget_limit,
                resolved_policy.budget_limit if resolved_policy else None,
            )
        )

    @staticmethod
    def _reservation_has_scope(
        reservation: BudgetReservation | None,
        scope: str,
    ) -> bool:
        return bool(
            reservation
            and any(
                counter_scope == scope
                for counter_scope, _ident, _limit in reservation.counters
            )
        )

    @staticmethod
    def _reserved_cost_tracker_scopes(
        reservation: BudgetReservation | None,
    ) -> frozenset[str]:
        if reservation is None:
            return frozenset()
        return frozenset(
            scope
            for scope, _ident, _limit in reservation.counters
            if scope in {"project", "user"}
        )

    async def _finalize_request_budget(
        self,
        reservation: BudgetReservation | None,
        *,
        actual_cost: float,
        req_ctx: RequestContext,
    ) -> dict[str, float] | None:
        if reservation is None or self._quota_enforcer is None:
            return {}
        return await self._quota_enforcer.finalize_budget(
            reservation,
            actual_cost,
            tenant_id=req_ctx.tenant_id,
            project_id=req_ctx.project_id,
        )

    async def _guard_budgeted_stream(
        self,
        stream: AsyncIterator[dict],
        reservation: BudgetReservation | None,
        *,
        req_ctx: RequestContext,
    ) -> AsyncIterator[dict]:
        """Reconcile a reservation when a caller abandons a stream."""
        completed = False
        try:
            async for event in stream:
                yield event
            completed = True
        finally:
            if not completed:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "budgeted stream close failed req=%s",
                            reservation.request_id if reservation else "",
                            exc_info=True,
                        )
                if reservation is not None:
                    try:
                        totals = await self._finalize_request_budget(
                            reservation,
                            actual_cost=reservation.amount,
                            req_ctx=req_ctx,
                        )
                        if totals and "user" in totals:
                            self.cost_tracker.adopt_user_spend(
                                req_ctx.user_id,
                                totals["user"],
                                tenant_id=req_ctx.tenant_id,
                            )
                        if totals is None:
                            logger.error(
                                "Failed to reconcile interrupted stream "
                                "reservation req=%s",
                                reservation.request_id,
                            )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Interrupted stream reservation reconciliation "
                            "raised req=%s",
                            reservation.request_id,
                        )

    async def _release_request_budget(
        self,
        reservation: BudgetReservation | None,
        *,
        req_ctx: RequestContext,
    ) -> None:
        if reservation is None or self._quota_enforcer is None:
            return
        released = await self._quota_enforcer.release_budget(
            reservation,
            tenant_id=req_ctx.tenant_id,
            project_id=req_ctx.project_id,
        )
        if not released:
            logger.error(
                "Failed to release budget reservation req=%s",
                reservation.request_id,
            )

    @staticmethod
    def _quota_error(
        decision: QuotaDecision,
        rate_limit_headers: dict[str, str] | None = None,
    ) -> dict:
        response = _error_response(
            decision.status_code,
            (
                "service_unavailable"
                if decision.status_code == 503
                else "quota_exceeded"
            ),
            decision.reason,
            code=decision.error_code or f"quota_{decision.limit_type}",
        )
        if rate_limit_headers:
            response["_rate_limit_headers"] = rate_limit_headers
        return response

    def _estimate_models_cost(
        self,
        models: set[str],
        *,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float:
        estimates: list[float] = []
        for model in models:
            try:
                chain = self.router.get_fallback_chain(model)
            except Exception:
                continue
            for mapping in chain:
                try:
                    estimates.append(
                        self.cost_tracker.calculate_cost(
                            mapping.provider,
                            mapping.model_id,
                            prompt_tokens,
                            completion_tokens,
                        )
                    )
                except Exception:
                    continue
        return max(estimates, default=0.0)

    def _estimate_request_cost(
        self,
        request: ChatCompletionRequest,
        candidate_models: set[str] | None = None,
    ) -> float:
        """Estimate the cost of a request before execution for quota pre-check.

        Uses a rough token estimate from message content and the model's pricing.
        Returns 0.0 if pricing cannot be resolved (quota budget check becomes no-op).
        """
        prompt_chars = 0
        for msg in request.messages or []:
            if isinstance(msg, dict):
                content = msg.get("content", "")
                if isinstance(content, str):
                    prompt_chars += len(content)
        estimated_prompt_tokens = max(1, prompt_chars // 4)
        estimated_completion_tokens = request.max_tokens or 256

        models = candidate_models or {request.model or ""}
        return self._estimate_models_cost(
            models,
            prompt_tokens=estimated_prompt_tokens,
            completion_tokens=estimated_completion_tokens,
        )

    def _compute_effective_allowed_models(
        self,
        project: Project | None,
        user_config: dict | str,
    ) -> set[str] | None:
        """Compute the effective allowed-models set from project and user access lists.

        Returns the intersection when both are set, the single list when only one
        is set, or ``None`` when neither is set (meaning all models are permitted).

        ``str`` retains the legacy internal call contract. Canonical request
        paths resolve a tenant-qualified config first and always pass the dict.
        """
        if isinstance(user_config, str):
            user_config = self._user_configs.get(user_config, {})
        project_allowed: set[str] | None = None
        if project and project.allowed_models:
            project_allowed = set(project.allowed_models)

        user_allowed_list = user_config.get("allowed_models")
        user_allowed: set[str] | None = None
        if user_allowed_list:
            user_allowed = set(user_allowed_list)

        if project_allowed is not None and user_allowed is not None:
            return project_allowed & user_allowed
        if project_allowed is not None:
            return project_allowed
        if user_allowed is not None:
            return user_allowed
        return None

    async def _user_config_for_context(
        self,
        req_ctx: RequestContext,
    ) -> dict:
        """Resolve user restrictions only inside the authenticated tenant."""
        if req_ctx.tenant_id is None:
            return self._user_configs.get(req_ctx.user_id, {})

        cache_key = (
            f"tenant:{len(req_ctx.tenant_id)}:{req_ctx.tenant_id}:"
            f"user:{len(req_ctx.user_id)}:{req_ctx.user_id}"
        )
        if self._persistence is None or not self._persistence.enabled:
            return self._user_configs.get(cache_key, {})
        loader = getattr(
            self._persistence,
            "get_tenant_user_config",
            None,
        )
        if not callable(loader):
            raise RuntimeError("tenant user config loading is unavailable")
        config = await loader(req_ctx.tenant_id, req_ctx.user_id)
        if config is None:
            self._user_configs.pop(cache_key, None)
            return {}
        self._user_configs[cache_key] = config
        return config

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_request(self, data: dict) -> ChatCompletionRequest:
        """Parse raw dict into ChatCompletionRequest."""
        stop = data.get("stop")
        if isinstance(stop, str):
            stop = [stop]
        return ChatCompletionRequest(
            messages=data.get("messages", []),
            model=data.get("model", ""),
            temperature=data.get("temperature"),
            max_tokens=data.get("max_tokens"),
            top_p=data.get("top_p"),
            stop=stop,
            stream=data.get("stream", False),
            system=data.get("system"),
            tools=data.get("tools"),
            tool_choice=data.get("tool_choice"),
        )

    def _extract_context(self, context: dict) -> RequestContext:
        """Extract RequestContext from the context dict."""
        authorized_project = context.get("authorized_project")
        if not isinstance(authorized_project, Project):
            authorized_project = None
        return RequestContext(
            user_id=context.get("user_id", ""),
            project_id=context.get("project_id", ""),
            roles=context.get("roles", []),
            scopes=context.get("scopes", []),
            tenant_id=context.get("tenant_id"),
            authorized_project=authorized_project,
            allow_legacy_project_lookup=bool(
                context.get("allow_legacy_project_lookup", False)
            ),
        )

    def _project_for_context(self, context: RequestContext) -> Project | None:
        """Return only the project proven to belong to this request's tenant."""
        project = context.authorized_project
        if project is not None:
            if project.project_id != context.project_id:
                return None
            if (
                context.tenant_id is None
                or project.tenant_id != context.tenant_id
            ):
                return None
            return project
        if (
            context.tenant_id is not None
            and not context.allow_legacy_project_lookup
        ):
            return None
        return self._projects.get(context.project_id)

    def _make_provider_fn(self):
        """Return a provider function compatible with Router.execute_with_fallback.

        .. deprecated::
            Use ``ProviderFnFactory`` instead. This method is retained for
            backward compatibility with tests that do not supply a factory.
        """
        warnings.warn(
            "_make_provider_fn is deprecated; use ProviderFnFactory instead",
            DeprecationWarning,
            stacklevel=2,
        )

        async def _noop(mapping):
            raise NotImplementedError("provider_fn must be supplied by caller")
        return _noop

    def _rehearsal_provider_fn(
        self,
        provider_fn,
        *,
        context: dict[str, Any],
        request: ChatCompletionRequest,
        request_id: str,
    ):
        rehearsal = context.get("rehearsal")
        binding = context.get("rehearsal_binding")
        ledger = context.get("rehearsal_ledger")
        operation = getattr(rehearsal, "operation", None)
        if (
            binding is None
            or ledger is None
            or operation
            not in {
                "exercise-routing-strategies",
                "verify-routing-decisions",
                "inject-primary-provider-fault",
                "verify-provider-fallback",
                "verify-primary-provider-recovery",
            }
        ):
            return provider_fn

        attempts: dict[str, int] = {}

        async def observed(mapping: ProviderModelMapping):
            attempt = attempts.get(mapping.provider, 0) + 1
            attempts[mapping.provider] = attempt
            fault = await asyncio.to_thread(
                ledger.read_active_fault,
                binding,
                "provider-unavailable",
            )
            if (
                fault is not None
                and fault.parameters.get("provider") == mapping.provider
            ):
                status_code = fault.parameters.get("status_code")
                if isinstance(status_code, int) and not isinstance(
                    status_code,
                    bool,
                ):
                    await asyncio.to_thread(
                        ledger.append_observation,
                        binding,
                        "provider-attempt",
                        {
                            "attempt": attempt,
                            "outcome": "retryable-failure",
                            "provider": mapping.provider,
                            "request_id": request_id,
                            "status_code": status_code,
                        },
                    )
                    raise ProviderError(
                        status_code,
                        mapping.provider,
                        "Provider is temporarily unavailable.",
                        retryable=False,
                        provider_unavailable=False,
                    )
            try:
                response = await provider_fn(mapping)
            except ProviderError as exc:
                await asyncio.to_thread(
                    ledger.append_observation,
                    binding,
                    "provider-attempt",
                    {
                        "attempt": attempt,
                        "outcome": (
                            "retryable-failure"
                            if exc.retryable is not False
                            else "non-retryable-failure"
                        ),
                        "provider": mapping.provider,
                        "request_id": request_id,
                        "status_code": exc.status_code,
                    },
                )
                raise
            await asyncio.to_thread(
                ledger.append_observation,
                binding,
                "provider-attempt",
                {
                    "attempt": attempt,
                    "outcome": "success",
                    "provider": mapping.provider,
                    "request_id": request_id,
                    "status_code": 200,
                },
            )
            strategy = getattr(rehearsal, "routing_strategy", None)
            if strategy is not None:
                try:
                    candidate_count = len(
                        self.router.available_mappings(request.model)
                    )
                except Exception:
                    candidate_count = 0
                if candidate_count > 0:
                    await asyncio.to_thread(
                        ledger.append_observation,
                        binding,
                        "routing-decision",
                        {
                            "candidate_count": candidate_count,
                            "provider": mapping.provider,
                            "request_id": request_id,
                            "strategy": strategy,
                        },
                    )
            return response

        return observed

    def _router_model_is_available(self, model: str) -> bool:
        """Use runtime availability when supported by the injected router.

        Older embedding integrations and lightweight custom routers predate the
        availability API. They remain compatible, while the production Router
        still enforces provider and pricing gates.
        """
        checker = getattr(self.router, "is_model_available", None)
        if not callable(checker):
            return True
        try:
            return bool(checker(model))
        except KeyError:
            return False

    @staticmethod
    def _response_billing_model(response: ChatCompletionResponse) -> str:
        return response.provider_model or response.model

    def _response_to_dict(
        self, response: ChatCompletionResponse, is_cached: bool = False
    ) -> dict:
        """Convert ChatCompletionResponse to a plain dict."""
        d: dict[str, Any] = {
            "id": response.id,
            "choices": response.choices,
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            },
            "model": response.model,
            "provider": response.provider,
        }
        if response.warnings:
            d["warnings"] = response.warnings
        if is_cached:
            d["is_cached"] = True
        return d

    def _chunk_to_dict(
        self,
        chunk: StreamChunk,
        *,
        provider: str | None = None,
    ) -> dict:
        """Convert StreamChunk to a plain dict."""
        result = {
            "id": chunk.id,
            "choices": chunk.choices,
            "model": chunk.model,
            "is_final": chunk.is_final,
        }
        if provider:
            result["provider"] = provider
        return result

    def _reinject_chunk_pii(
        self, chunk_dict: dict, mapping: RedactionMapping, buffer: dict
    ) -> dict:
        """Re-inject PII tokens in a streaming chunk's delta content.

        Uses a buffer to handle tokens split across chunk boundaries.
        Buffer holds {"pending": str} — text that might be a partial token.
        """
        # Permanent-redaction mode retains no mapping, so there is nothing to
        # re-inject. Skip the buffering entirely (it would otherwise hold back
        # any legitimate "[" in the model's output waiting for a closing "]").
        if not mapping.reversible:
            return chunk_dict
        choices = chunk_dict.get("choices", [])
        new_choices = []
        for choice in choices:
            delta = choice.get("delta", {})
            content = delta.get("content", "")
            if isinstance(content, str) and content:
                text = buffer.get("pending", "") + content
                # Check if text ends with a potential partial token (starts with [ but no closing ])
                last_open = text.rfind("[")
                if last_open != -1 and "]" not in text[last_open:]:
                    buffer["pending"] = text[last_open:]
                    text = text[:last_open]
                else:
                    buffer["pending"] = ""
                reinjected = mapping.reinject(text)
                new_choices.append({**choice, "delta": {**delta, "content": reinjected}})
            else:
                new_choices.append(choice)
        return {**chunk_dict, "choices": new_choices}

    def _replace_response_content(
        self, response: ChatCompletionResponse, message: str
    ) -> ChatCompletionResponse:
        """Return a copy of the response with content replaced by a policy message."""
        replaced_choices = []
        for choice in response.choices:
            new_choice = dict(choice)
            new_choice["message"] = {"role": "assistant", "content": message}
            replaced_choices.append(new_choice)
        return ChatCompletionResponse(
            id=response.id,
            choices=replaced_choices,
            usage=response.usage,
            model=response.model,
            provider=response.provider,
            warnings=response.warnings + ["Response modified by guardrail"],
        )

    async def handle_embeddings(
        self,
        request_data: dict,
        context: dict,
    ) -> dict:
        """Run a governed embeddings request through the shared router."""
        raw_input = request_data.get("input")
        if isinstance(raw_input, str):
            inputs = [raw_input]
        elif isinstance(raw_input, list):
            inputs = raw_input
        else:
            return _error_response(
                400,
                "invalid_request",
                "Field 'input' must be a string or a list of strings.",
                code="invalid_embedding_input",
            )
        if not inputs or len(inputs) > _MAX_EMBEDDING_INPUTS:
            return _error_response(
                400,
                "invalid_request",
                (
                    "Field 'input' must contain between 1 and "
                    f"{_MAX_EMBEDDING_INPUTS} strings."
                ),
                code="invalid_embedding_input",
            )
        if not all(isinstance(item, str) and item for item in inputs):
            return _error_response(
                400,
                "invalid_request",
                "Every embeddings input must be a non-empty string.",
                code="invalid_embedding_input",
            )
        if (
            sum(len(item.encode("utf-8")) for item in inputs)
            > _MAX_EMBEDDING_INPUT_BYTES
        ):
            return _error_response(
                413,
                "invalid_request",
                "Embeddings input exceeds the maximum request size.",
                code="embedding_input_too_large",
            )

        model = request_data.get("model")
        if not isinstance(model, str) or not model.strip():
            return _error_response(
                400,
                "invalid_request",
                "Field 'model' is required.",
                code="model_required",
            )
        encoding_format = request_data.get("encoding_format", "float")
        if encoding_format not in {"float", "base64"}:
            return _error_response(
                400,
                "invalid_request",
                "Field 'encoding_format' must be 'float' or 'base64'.",
                code="invalid_encoding_format",
            )
        dimensions = request_data.get("dimensions")
        if dimensions is not None and (
            not isinstance(dimensions, int)
            or isinstance(dimensions, bool)
            or not 1 <= dimensions <= _MAX_EMBEDDING_DIMENSIONS
        ):
            return _error_response(
                400,
                "invalid_request",
                (
                    "Field 'dimensions' must be an integer between 1 and "
                    f"{_MAX_EMBEDDING_DIMENSIONS}."
                ),
                code="invalid_dimensions",
            )
        user = request_data.get("user")
        if user is not None and (
            not isinstance(user, str) or not user.strip()
        ):
            return _error_response(
                400,
                "invalid_request",
                "Field 'user' must be a non-empty string.",
                code="invalid_user",
            )

        req_ctx = self._extract_context(context)
        project = self._project_for_context(req_ctx)
        if (
            req_ctx.tenant_id is not None
            and project is None
            and not req_ctx.allow_legacy_project_lookup
        ):
            return _error_response(
                404,
                "not_found",
                "The requested resource was not found.",
                code="resource_not_found",
            )

        model_config = self.router.model_registry.models.get(model)
        if model_config is None:
            return _error_response(
                404,
                "not_found",
                f"Model '{model}' not found.",
                code="model_not_found",
            )
        if "embeddings" not in set(model_config.capabilities or []):
            return _error_response(
                400,
                "invalid_request",
                f"Model '{model}' is not configured for embeddings.",
                code="model_capability_mismatch",
            )
        if not self._router_model_is_available(model):
            return _error_response(
                503,
                "service_unavailable",
                f"Model '{model}' is not available in this deployment.",
                code="model_unavailable",
            )

        if project is not None and (
            project.budget_limit is not None
            or project.alert_threshold is not None
        ):
            self.cost_tracker.register_project(
                project.project_id,
                budget_limit=project.budget_limit,
                alert_threshold=project.alert_threshold,
                tenant_id=req_ctx.tenant_id,
            )

        estimated_tokens = sum(max(1, len(item) // 4) for item in inputs)
        estimated_cost = self._estimate_models_cost(
            {model},
            prompt_tokens=estimated_tokens,
            completion_tokens=0,
        )
        resolved_policy = None
        if self._quota_enforcer is not None and self._policy_resolver is not None:
            resolved_policy = await self._policy_resolver.resolve(
                req_ctx.project_id,
                tenant_id=req_ctx.tenant_id,
                project=project,
            )
            quota_decision = await self._quota_enforcer.enforce_all(
                project_id=req_ctx.project_id,
                model=model,
                provider=context.get("provider"),
                max_tokens=None,
                estimated_cost=estimated_cost,
                policy=resolved_policy,
                tenant_id=req_ctx.tenant_id,
                project=project,
            )
            if not quota_decision.allowed:
                return self._quota_error(quota_decision)

        rate_result = await self.rate_limiter.check_rate_limit(
            req_ctx.user_id,
            req_ctx.project_id,
            tenant_id=req_ctx.tenant_id,
            project=project,
        )
        rate_limit_headers = {
            "X-RateLimit-Limit": str(rate_result.limit),
            "X-RateLimit-Remaining": str(rate_result.remaining),
            "X-RateLimit-Reset": str(int(rate_result.reset_at.timestamp())),
        }
        if not rate_result.allowed:
            if rate_result.retry_after_seconds is not None:
                rate_limit_headers["Retry-After"] = str(
                    rate_result.retry_after_seconds
                )
            response = _error_response(
                429,
                "rate_limit_error",
                "Rate limit exceeded.",
                code="rate_limit_exceeded",
            )
            response["_rate_limit_headers"] = rate_limit_headers
            return response

        if project and project.allowed_models and model not in project.allowed_models:
            response = _error_response(
                403,
                "forbidden",
                f"Model '{model}' is not allowed for this project.",
                code="model_not_allowed",
            )
            response["_rate_limit_headers"] = rate_limit_headers
            return response

        try:
            user_config = await self._user_config_for_context(req_ctx)
        except RuntimeError:
            response = _error_response(
                503,
                "service_unavailable",
                "Tenant user configuration is temporarily unavailable.",
                code="user_config_unavailable",
            )
            response["_rate_limit_headers"] = rate_limit_headers
            return response
        self.cost_tracker.register_user(
            req_ctx.user_id,
            budget_limit=user_config.get("budget_limit"),
            alert_threshold=user_config.get("alert_threshold"),
            tenant_id=req_ctx.tenant_id,
        )
        user_allowed = user_config.get("allowed_models")
        if user_allowed and model not in user_allowed:
            response = _error_response(
                403,
                "forbidden",
                f"Model '{model}' is not allowed for this user.",
                code="model_not_allowed",
            )
            response["_rate_limit_headers"] = rate_limit_headers
            return response

        project_budget: BudgetStatus | None = None
        if project is not None:
            project_budget = await self.cost_tracker.check_budget(
                req_ctx.project_id,
                tenant_id=req_ctx.tenant_id,
            )
            if project_budget.is_over_budget:
                response = _error_response(
                    429,
                    "budget_exceeded",
                    "The project has exceeded its budget.",
                    code="budget_exceeded",
                )
                response["_rate_limit_headers"] = rate_limit_headers
                return response
        user_budget = await self.cost_tracker.check_user_budget(
            req_ctx.user_id,
            tenant_id=req_ctx.tenant_id,
        )
        if user_budget.is_over_budget:
            response = _error_response(
                429,
                "budget_exceeded",
                "The user has exceeded their budget.",
                code="budget_exceeded",
            )
            response["_rate_limit_headers"] = rate_limit_headers
            return response

        request_id = f"req_{uuid.uuid4().hex}"
        reservation: BudgetReservation | None = None
        if self._request_has_budget(
            project_budget,
            user_budget,
            resolved_policy,
        ):
            decision = await self._reserve_request_budget(
                request_id=request_id,
                req_ctx=req_ctx,
                estimated_cost=estimated_cost,
                project_budget=project_budget,
                user_budget=user_budget,
                resolved_policy=resolved_policy,
            )
            if not decision.allowed:
                return self._quota_error(decision, rate_limit_headers)
            reservation = decision.reservation

        embedding_request = EmbeddingRequest(
            input=inputs,
            model=model,
            encoding_format=encoding_format,
            dimensions=dimensions,
            user=user,
        )
        if self._routing_runtime is None:
            await self._release_request_budget(
                reservation,
                req_ctx=req_ctx,
            )
            response = _error_response(
                503,
                "service_unavailable",
                "The embeddings routing runtime is unavailable.",
                code="routing_runtime_unavailable",
            )
            response["_rate_limit_headers"] = rate_limit_headers
            return response

        request_started = time.perf_counter()
        try:
            embedding_response = await self._routing_runtime.embed(
                embedding_request,
                preferred_provider=context.get("provider"),
            )
        except AllProvidersExhaustedError:
            await self._release_request_budget(
                reservation,
                req_ctx=req_ctx,
            )
            response = _error_response(
                502,
                "provider_error",
                _PROVIDER_FAILURE_MESSAGE,
                code="all_providers_exhausted",
            )
            response["_rate_limit_headers"] = rate_limit_headers
            return response
        except Exception:
            if reservation is not None:
                await self._finalize_request_budget(
                    reservation,
                    actual_cost=reservation.amount,
                    req_ctx=req_ctx,
                )
            logger.exception("embeddings request failed")
            response = _error_response(
                500,
                "server_error",
                "The embeddings request failed.",
                code="embeddings_failed",
            )
            response["_rate_limit_headers"] = rate_limit_headers
            return response

        cost = self.cost_tracker.calculate_cost(
            embedding_response.provider,
            embedding_response.provider_model or embedding_response.model,
            embedding_response.usage.prompt_tokens,
            0,
        )
        usage_record = UsageRecord(
            request_id=request_id,
            project_id=req_ctx.project_id,
            user_id=req_ctx.user_id,
            tenant_id=req_ctx.tenant_id,
            provider=embedding_response.provider,
            model=model,
            prompt_tokens=embedding_response.usage.prompt_tokens,
            completion_tokens=0,
            total_tokens=embedding_response.usage.total_tokens,
            cost=cost,
            timestamp=datetime.now(timezone.utc),
            latency_ms=(time.perf_counter() - request_started) * 1000,
            status="success",
            provider_request_id=embedding_response.id,
        )
        reservation_totals = await self._finalize_request_budget(
            reservation,
            actual_cost=cost,
            req_ctx=req_ctx,
        )
        if reservation_totals is None:
            response = _error_response(
                503,
                "service_unavailable",
                "Budget accounting could not be finalized.",
                code="budget_finalization_failed",
            )
            response["_rate_limit_headers"] = rate_limit_headers
            return response
        await self.cost_tracker.record_usage(
            usage_record,
            skip_shared_scopes=self._reserved_cost_tracker_scopes(
                reservation
            ),
        )
        if reservation_totals:
            self.cost_tracker.adopt_reserved_spend(
                usage_record,
                reservation_totals,
            )
        if (
            self._quota_enforcer is not None
            and not self._reservation_has_scope(reservation, "quota")
        ):
            await self._quota_enforcer.record_spend(
                req_ctx.project_id,
                cost,
                budget_limit=(
                    resolved_policy.budget_limit
                    if resolved_policy
                    else None
                ),
                tenant_id=req_ctx.tenant_id,
            )
        if self._trace_forwarder is not None:
            await self._trace_forwarder.forward(usage_record)
        if self._otlp_exporter is not None and not (
            self._trace_forwarder is not None
            and self._trace_forwarder.enabled
        ):
            self._otlp_exporter.export_usage(usage_record)

        return {
            "object": "list",
            "data": [
                {
                    "object": "embedding",
                    "index": item.index,
                    "embedding": item.embedding,
                }
                for item in embedding_response.data
            ],
            "model": embedding_response.model,
            "usage": {
                "prompt_tokens": embedding_response.usage.prompt_tokens,
                "total_tokens": embedding_response.usage.total_tokens,
            },
            "_rate_limit_headers": rate_limit_headers,
        }

    async def handle_list_models(
        self,
        project_id: str | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
        authorized_project: Project | None = None,
        allow_legacy_project_lookup: bool = False,
    ) -> dict:
        """Return all models with descriptions and capabilities.

        If project_id is provided and the project has allowed_models,
        only those models are returned. If user_id is provided and the
        user has allowed_models, further filter to the intersection.
        """
        def available_mappings(model):
            return self.router.available_mappings(model.name)

        models = [
            model
            for model in self.router.model_registry.list_models()
            if available_mappings(model)
        ]
        if project_id:
            project = self._project_for_context(
                RequestContext(
                    user_id=user_id or "",
                    project_id=project_id,
                    roles=[],
                    scopes=[],
                    tenant_id=tenant_id,
                    authorized_project=authorized_project,
                    allow_legacy_project_lookup=allow_legacy_project_lookup,
                )
            )
            if (
                tenant_id is not None
                and project is None
                and not allow_legacy_project_lookup
            ):
                return {"models": []}
            if project and project.allowed_models:
                allowed = set(project.allowed_models)
                models = [m for m in models if m.name in allowed]

        if user_id:
            user_config = self._user_configs.get(user_id, {})
            user_allowed = user_config.get("allowed_models")
            if user_allowed:
                allowed_set = set(user_allowed)
                models = [m for m in models if m.name in allowed_set]

        return {
            "models": [
                {
                    "name": m.name,
                    "description": m.description,
                    "providers": [
                        mapping.provider
                        for mapping in available_mappings(m)
                    ],
                    "capabilities": m.capabilities or [],
                    "routing_strategy": m.routing_strategy.value,
                }
                for m in models
            ]
        }

    async def handle_health_check(self) -> dict:
        """Return service status and per-provider health."""
        models = self.router.model_registry.list_models()
        providers: set[str] = set()
        for m in models:
            for mapping in self.router.available_mappings(m.name):
                providers.add(mapping.provider)

        provider_health: dict[str, str] = {}
        for provider in sorted(providers):
            provider_health[provider] = (
                "healthy" if self.router.health_tracker.is_healthy(provider) else "unhealthy"
            )

        return {
            "status": "ok",
            "providers": provider_health,
        }



# ---------------------------------------------------------------------------
# App instance and entrypoint registration
# ---------------------------------------------------------------------------

app = BedrockAgentCoreApp()

# Singleton agent — wired up by create_gateway_agent()
_agent: GatewayAgent | None = None


@app.entrypoint("chat_completions")
async def chat_completions(request_data: dict, context: dict) -> dict | AsyncIterator[dict]:
    """Main chat completions entrypoint registered with BedrockAgentCoreApp."""
    if _agent is None:
        return _error_response(500, "server_error", "Gateway agent not initialised.")
    return await _agent.handle_chat_completion(request_data, context)

@app.entrypoint("list_models")
async def list_models() -> dict:
    """List all available models."""
    if _agent is None:
        return _error_response(500, "server_error", "Gateway agent not initialised.")
    return await _agent.handle_list_models()


@app.entrypoint("health_check")
async def health_check() -> dict:
    """Health check with per-provider status."""
    if _agent is None:
        return _error_response(500, "server_error", "Gateway agent not initialised.")
    return await _agent.handle_health_check()



# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_gateway_agent(
    router: Router,
    rate_limiter: SlidingWindowRateLimiter,
    guardrail_engine: GuardrailEngine,
    cache_manager: CacheManager,
    cost_tracker: CostTracker,
    session_manager: SessionManager | None = None,
    projects: dict[str, Project] | None = None,
    provider_fn_factory: ProviderFnFactory | None = None,
    user_configs: dict[str, dict] | None = None,
    request_validator: RequestValidator | None = None,
    quota_enforcer: QuotaEnforcer | None = None,
    policy_resolver: PolicyHierarchyResolver | None = None,
    pii_redactor: PIIRedactor | None = None,
    injection_detector: PromptInjectionDetector | None = None,
    audit_trail: AuditTrail | None = None,
    event_dispatcher: EventDispatcher | None = None,
    region_router: RegionRouter | None = None,
    trace_forwarder: TraceForwarder | None = None,
    otlp_exporter: Any = None,
    semantic_cache: SemanticCache | None = None,
    persistence: DynamoPersistence | None = None,
) -> GatewayAgent:
    """Create and wire a GatewayAgent, also setting the module-level singleton."""
    global _agent
    agent = GatewayAgent(
        router=router,
        rate_limiter=rate_limiter,
        guardrail_engine=guardrail_engine,
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
        session_manager=session_manager,
        projects=projects,
        provider_fn_factory=provider_fn_factory,
        user_configs=user_configs,
        request_validator=request_validator,
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
    )
    _agent = agent
    return agent
