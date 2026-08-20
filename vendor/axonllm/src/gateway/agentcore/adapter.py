"""Fail-closed AgentCore authorization and gateway dispatch."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import logging
import os
import socket
import uuid
from collections.abc import AsyncIterator
from typing import Any, Protocol

from src.gateway.auth.authorization import (
    Action,
    AuthorizationDenied,
    ResourceRef,
    require_authorized,
)
from src.gateway.auth.project_repository import (
    ProjectConfigConflict,
    ProjectStoreUnavailable,
)
from src.gateway.config_sync import RegionTopologyUnavailable
from src.gateway.models import GuardrailRule, Project, TenantRole
from src.gateway.persistence import validate_project_storage_size
from src.gateway.query.service import QueryServiceError
from src.gateway.rehearsal_control import (
    RehearsalBinding,
    RehearsalControlLedger,
)
from src.gateway.security.audit_trail import AuditEventType

from .errors import AgentCoreAdapterError
from .identity import InvocationIdentity, resolve_invocation_identity
from .runtime import RuntimeReadiness, RuntimeServices
from .schemas import (
    InvocationAction,
    QueryInvocationResponse,
    QueryResponseValidationError,
    RehearsalInvocation,
    parse_invocation_payload,
)

logger = logging.getLogger(__name__)
_REHEARSAL_PROCESS_EXIT_ENV = "AXON_LAUNCH_REHEARSAL_ALLOW_PROCESS_EXIT"


class RuntimeProviderProtocol(Protocol):
    async def get(self) -> RuntimeServices: ...

    async def initialize(self) -> RuntimeServices: ...

    async def readiness(self, *, force: bool = False) -> RuntimeReadiness: ...

    async def close(self) -> None: ...


def _gateway_context(
    identity: InvocationIdentity,
    project: Project,
    *,
    preferred_provider: str | None = None,
    rehearsal: RehearsalInvocation | None = None,
    rehearsal_binding: RehearsalBinding | None = None,
    rehearsal_ledger: RehearsalControlLedger | None = None,
) -> dict[str, Any]:
    context = identity.request_context
    gateway_context = {
        "user_id": context.user_id,
        "project_id": context.project_id,
        "roles": list(context.roles),
        "scopes": list(context.scopes),
        "tenant_id": context.tenant_id,
        "auth_method": context.auth_method.value,
        "principal_id": context.principal_id,
        "authorization_version": context.authorization_version,
        "authorized_project": project,
    }
    if preferred_provider is not None:
        gateway_context["provider"] = preferred_provider
    if (
        rehearsal is not None
        and rehearsal_binding is not None
        and rehearsal_ledger is not None
    ):
        gateway_context["rehearsal"] = rehearsal
        gateway_context["rehearsal_binding"] = rehearsal_binding
        gateway_context["rehearsal_ledger"] = rehearsal_ledger
    return gateway_context


def _rehearsal_binding(
    rehearsal: RehearsalInvocation | None,
    identity: InvocationIdentity,
) -> RehearsalBinding | None:
    if rehearsal is None:
        return None
    return RehearsalBinding.from_authenticated_request(
        tenant_id=identity.tenant_id,
        project_id=identity.project_id,
        correlation_id=rehearsal.correlation_id,
        owner_id=rehearsal.owner_id,
        release_commit=rehearsal.release_commit,
        fence_token=rehearsal.fence_token,
        expires_at_epoch=rehearsal.expires_at_epoch,
    )


def _require_rehearsal_authority(
    rehearsal: RehearsalInvocation | None,
    identity: InvocationIdentity,
) -> None:
    if rehearsal is None:
        return
    principal = identity.principal
    if (
        TenantRole.SERVICE not in principal.roles
        or "launch.rehearsal" not in principal.scopes
    ):
        raise AgentCoreAdapterError(
            403,
            "authorization_denied",
            "Action is not permitted.",
        )


async def _record_tenant_config_audit(
    runtime: RuntimeServices,
    *,
    event_type: AuditEventType,
    identity: InvocationIdentity,
    request_id: str,
    data: dict[str, Any],
) -> None:
    audit_trail = runtime.audit_trail
    if (
        audit_trail is None
        or getattr(audit_trail, "durable_enabled", False) is not True
    ):
        raise AgentCoreAdapterError(
            503,
            "tenant_config_audit_unavailable",
            "Durable tenant configuration audit is unavailable.",
        )
    try:
        await audit_trail.record(
            event_type=event_type,
            user_id=identity.principal.principal_id,
            project_id=identity.project_id,
            request_id=request_id,
            data=data,
            tenant_id=identity.tenant_id,
        )
    except Exception as exc:
        raise AgentCoreAdapterError(
            503,
            "tenant_config_audit_unavailable",
            "Durable tenant configuration audit is unavailable.",
        ) from exc


def _runtime_instance_id() -> str | None:
    value = socket.gethostname()
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(not (character.isalnum() or character in "._:-") for character in value)
    ):
        return None
    return value


async def _apply_rehearsal_control(
    *,
    ledger: RehearsalControlLedger | None,
    binding: RehearsalBinding | None,
    rehearsal: RehearsalInvocation | None,
) -> None:
    if ledger is None or binding is None or rehearsal is None:
        return

    dependency = rehearsal.dependency
    dependency_fault = await asyncio.to_thread(
        ledger.read_active_fault,
        binding,
        "dependency-unavailable",
    )
    if (
        dependency_fault is not None
        and dependency is not None
        and dependency_fault.parameters.get("dependency") == dependency
    ):
        await asyncio.to_thread(
            ledger.append_observation,
            binding,
            "dependency-call",
            {
                "dependency": dependency,
                "outcome": "unavailable",
                "request_id": rehearsal.correlation_id,
                "status_code": 503,
            },
        )
        raise AgentCoreAdapterError(
            503,
            "rehearsal_dependency_unavailable",
            "A required dependency is temporarily unavailable.",
        )
    if (
        rehearsal.operation == "verify-control-plane-recovery"
        and dependency is not None
    ):
        await asyncio.to_thread(
            ledger.append_observation,
            binding,
            "dependency-call",
            {
                "dependency": dependency,
                "outcome": "available",
                "request_id": rehearsal.correlation_id,
                "status_code": 200,
            },
        )

    instance_id = _runtime_instance_id()
    if (
        rehearsal.operation == "induce-initialization-timeout"
        and os.environ.get(_REHEARSAL_PROCESS_EXIT_ENV) == "true"
        and instance_id is not None
    ):
        control = await asyncio.to_thread(
            ledger.read_active_fault,
            binding,
            "startup-delay",
        )
        if control is None:
            return
        delay = control.parameters.get("delay_seconds")
        if isinstance(delay, bool) or not isinstance(delay, int):
            return
        await asyncio.to_thread(
            ledger.append_observation,
            binding,
            "startup-attempt",
            {
                "boot_id": rehearsal.correlation_id,
                "phase": "started",
                "runtime_id": instance_id,
            },
        )
        await asyncio.sleep(delay)
        if not await asyncio.to_thread(
            ledger.append_observation,
            binding,
            "startup-attempt",
            {
                "boot_id": rehearsal.correlation_id,
                "phase": "timed-out",
                "exit_code": 124,
                "runtime_id": instance_id,
            },
        ):
            return
        os._exit(124)

    if (
        rehearsal.operation
        in {
            "observe-runtime-replacement",
            "verify-replacement-ready",
        }
        and instance_id is not None
    ):
        await asyncio.to_thread(
            ledger.append_observation,
            binding,
            "startup-attempt",
            {
                "boot_id": rehearsal.correlation_id,
                "phase": "ready",
                "runtime_id": instance_id,
            },
        )


def _project_configuration(project: Project) -> dict[str, Any]:
    return {
        "tenant_id": project.tenant_id,
        "project_id": project.project_id,
        "revision": project.revision,
        "config": {
            "name": project.name,
            "budget_limit": project.budget_limit,
            "alert_threshold": project.alert_threshold,
            "allowed_models": deepcopy(project.allowed_models),
            "guardrail_rules": [
                {
                    "name": rule.name,
                    "rule_type": rule.rule_type,
                    "pattern": rule.pattern,
                    "action": rule.action,
                    "applies_to": rule.applies_to,
                }
                for rule in project.guardrail_rules
            ],
            "cache_enabled": project.cache_enabled,
            "cache_ttl_seconds": project.cache_ttl_seconds,
            "semantic_cache_enabled": project.semantic_cache_enabled,
            "semantic_cache_threshold": project.semantic_cache_threshold,
            "log_level": project.log_level,
            "log_destination": project.log_destination,
            "prompt_caching_enabled": project.prompt_caching_enabled,
            "ltm_enabled": project.ltm_enabled,
            "retention_period_hours": project.retention_period_hours,
            "rate_limit_rpm": project.rate_limit_rpm,
        },
    }


def _stage_project_configuration(
    project: Project,
    updates: dict[str, Any],
) -> Project:
    staged = deepcopy(updates)
    if "guardrail_rules" in staged:
        staged["guardrail_rules"] = [
            GuardrailRule(**rule)
            for rule in staged["guardrail_rules"]
        ]
    detached = {
        "allowed_models": deepcopy(project.allowed_models),
        "guardrail_rules": deepcopy(project.guardrail_rules),
        "members": deepcopy(project.members),
    }
    detached.update(staged)
    return replace(project, **detached)


async def _forward_stream(
    stream: AsyncIterator[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    try:
        async for chunk in stream:
            yield chunk
    finally:
        close = getattr(stream, "aclose", None)
        if callable(close):
            try:
                await close()
            except Exception:  # noqa: BLE001 - cleanup cannot replace the stream result
                logger.debug("AgentCore downstream stream close failed", exc_info=True)


class AgentCoreAdapter:
    """Authorize trusted runtime identity before invoking gateway operations."""

    def __init__(self, runtime_provider: RuntimeProviderProtocol) -> None:
        self._runtime_provider = runtime_provider

    async def initialize(self) -> None:
        """Initialize and verify runtime dependencies before serving."""
        await self._runtime_provider.initialize()

    async def readiness(self) -> dict[str, Any]:
        """Return sanitized dependency readiness without authenticating a user."""
        return (await self._runtime_provider.readiness()).as_dict()

    async def close(self) -> None:
        """Close runtime-owned resources during graceful shutdown."""
        await self._runtime_provider.close()

    async def invoke(self, payload: Any, context: Any) -> Any:
        parsed = parse_invocation_payload(payload)
        if parsed.action is InvocationAction.HEALTH:
            return {
                "status": "alive",
                "ready": False,
                "dependencies": "not_checked",
            }

        try:
            runtime = await self._runtime_provider.get()
        except AgentCoreAdapterError:
            raise
        except Exception as exc:
            raise AgentCoreAdapterError(
                503,
                "gateway_initialization_failed",
                "Gateway initialization is temporarily unavailable.",
            ) from exc

        identity = await resolve_invocation_identity(
            context,
            runtime.token_verifier,
            runtime.principal_resolver,
        )
        _require_rehearsal_authority(parsed.rehearsal, identity)
        rehearsal_ledger = runtime.rehearsal_ledger
        rehearsal_binding = _rehearsal_binding(
            parsed.rehearsal,
            identity,
        )
        if runtime.config_sync is not None:
            try:
                await runtime.config_sync.refresh_if_stale()
            except RegionTopologyUnavailable as exc:
                raise AgentCoreAdapterError(
                    503,
                    "region_topology_unavailable",
                    "Region routing configuration is temporarily unavailable.",
                ) from exc
            except Exception:
                logger.warning(
                    "AgentCore config refresh failed; using loaded config",
                    exc_info=True,
                )
        try:
            project = await runtime.project_resolver.resolve(
                identity.tenant_id,
                identity.project_id,
            )
        except ProjectStoreUnavailable as exc:
            raise AgentCoreAdapterError(
                503,
                "project_resolver_unavailable",
                "Project authorization is temporarily unavailable.",
            ) from exc
        if project is None:
            raise AgentCoreAdapterError(
                404,
                "resource_not_found",
                "Resource not found.",
            )

        if parsed.action in {
            InvocationAction.LIST_MODELS,
            InvocationAction.READINESS,
        }:
            action = Action.MODEL_LIST
        elif parsed.action is InvocationAction.QUERY:
            action = Action.QUERY_SELECT
        elif parsed.action is InvocationAction.GET_TENANT_CONFIG:
            action = Action.TENANT_CONFIG_READ
        elif parsed.action is InvocationAction.UPDATE_TENANT_CONFIG:
            action = Action.TENANT_CONFIG_WRITE
        else:
            action = Action.INFERENCE_INVOKE
        resource = ResourceRef(
            resource_type="project",
            resource_id=identity.project_id,
            tenant_id=project.tenant_id,
            project_id=identity.project_id,
        )
        try:
            require_authorized(identity.principal, action, resource)
        except AuthorizationDenied as exc:
            message = "Resource not found." if exc.decision.conceal_resource else "Action is not permitted."
            raise AgentCoreAdapterError(
                exc.decision.status_code,
                "authorization_denied",
                message,
            ) from exc

        if runtime.policy_service is not None:
            refresh = getattr(runtime.policy_service, "refresh_if_stale", None)
            if callable(refresh):
                try:
                    await refresh()
                except Exception:
                    logger.warning(
                        "AgentCore policy refresh failed; using compiled policy",
                        exc_info=True,
                    )

            if parsed.action is InvocationAction.LIST_MODELS:
                policy_action, policy_resource = ("get", "/v1/models")
            elif parsed.action is InvocationAction.READINESS:
                policy_action, policy_resource = ("get", "/ready")
            elif parsed.action is InvocationAction.EMBEDDINGS:
                policy_action, policy_resource = (
                    "post",
                    "/v1/embeddings",
                )
            elif parsed.action is InvocationAction.QUERY:
                policy_action, policy_resource = ("post", "/v1/query")
            elif parsed.action is InvocationAction.GET_TENANT_CONFIG:
                policy_action, policy_resource = (
                    "get",
                    "/v1/tenant/config",
                )
            elif parsed.action is InvocationAction.UPDATE_TENANT_CONFIG:
                policy_action, policy_resource = (
                    "put",
                    "/v1/tenant/config",
                )
            else:
                policy_action, policy_resource = (
                    "post",
                    "/v1/chat/completions",
                )
            try:
                policy_decision = await runtime.policy_service.evaluate(
                    identity.request_context,
                    policy_action,
                    policy_resource,
                )
            except Exception as exc:
                raise AgentCoreAdapterError(
                    503,
                    "policy_evaluation_failed",
                    "Authorization is temporarily unavailable.",
                ) from exc

            if policy_decision == "DENY":
                raise AgentCoreAdapterError(
                    403,
                    "authorization_denied",
                    "Access denied by policy.",
                )
            if policy_decision != "ALLOW":
                raise AgentCoreAdapterError(
                    503,
                    "policy_evaluation_failed",
                    "Authorization is temporarily unavailable.",
                )

        await _apply_rehearsal_control(
            ledger=rehearsal_ledger,
            binding=rehearsal_binding,
            rehearsal=parsed.rehearsal,
        )

        if parsed.action is InvocationAction.READINESS:
            return (
                await self._runtime_provider.readiness(force=True)
            ).as_dict()

        if parsed.action is InvocationAction.LIST_MODELS:
            return await runtime.gateway.handle_list_models(
                project_id=identity.project_id,
                user_id=identity.principal.principal_id,
                tenant_id=identity.tenant_id,
                authorized_project=project,
            )

        if parsed.action is InvocationAction.EMBEDDINGS:
            if parsed.request_data is None:
                raise AgentCoreAdapterError(
                    400,
                    "invalid_payload",
                    "Embeddings payload is required.",
                )
            return await runtime.gateway.handle_embeddings(
                parsed.request_data,
                _gateway_context(
                    identity,
                    project,
                    preferred_provider=parsed.preferred_provider,
                    rehearsal=parsed.rehearsal,
                    rehearsal_binding=rehearsal_binding,
                    rehearsal_ledger=rehearsal_ledger,
                ),
            )

        if parsed.action is InvocationAction.GET_TENANT_CONFIG:
            return _project_configuration(project)

        if parsed.action is InvocationAction.UPDATE_TENANT_CONFIG:
            request = parsed.tenant_config_update
            if request is None:
                raise AgentCoreAdapterError(
                    400,
                    "invalid_payload",
                    "Tenant configuration update payload is required.",
                )
            if runtime.project_config_store is None:
                raise AgentCoreAdapterError(
                    503,
                    "tenant_config_unavailable",
                    "Tenant configuration is temporarily unavailable.",
                )
            try:
                staged = _stage_project_configuration(
                    project,
                    request.updates,
                )
                validate_project_storage_size(staged)
            except (TypeError, ValueError) as exc:
                raise AgentCoreAdapterError(
                    400,
                    "invalid_payload",
                    "Tenant configuration update is invalid.",
                ) from exc
            mutation_request_id = f"cfg_{uuid.uuid4().hex}"
            changed_fields = sorted(request.updates)
            await _record_tenant_config_audit(
                runtime,
                event_type=(
                    AuditEventType.TENANT_CONFIG_MUTATION_REQUEST
                ),
                identity=identity,
                request_id=mutation_request_id,
                data={
                    "changed_fields": changed_fields,
                    "expected_revision": request.expected_revision,
                    "observed_revision": project.revision,
                },
            )

            async def _record_result(
                *,
                status: str,
                revision: int | None = None,
                failure_code: str | None = None,
            ) -> None:
                data: dict[str, Any] = {
                    "changed_fields": changed_fields,
                    "previous_revision": project.revision,
                    "status": status,
                }
                if revision is not None:
                    data["revision"] = revision
                if failure_code is not None:
                    data["failure_code"] = failure_code
                await _record_tenant_config_audit(
                    runtime,
                    event_type=(
                        AuditEventType.TENANT_CONFIG_MUTATION_RESULT
                    ),
                    identity=identity,
                    request_id=mutation_request_id,
                    data=data,
                )

            if request.expected_revision != project.revision:
                await _record_result(
                    status="conflict",
                    failure_code="tenant_config_write_conflict",
                )
                raise AgentCoreAdapterError(
                    409,
                    "tenant_config_write_conflict",
                    "Tenant configuration changed; reload and retry.",
                )
            try:
                committed = await runtime.project_config_store.update(
                    staged,
                    expected_revision=request.expected_revision,
                )
            except ProjectConfigConflict as exc:
                await _record_result(
                    status="conflict",
                    failure_code="tenant_config_write_conflict",
                )
                raise AgentCoreAdapterError(
                    409,
                    "tenant_config_write_conflict",
                    "Tenant configuration changed; reload and retry.",
                ) from exc
            except ProjectStoreUnavailable as exc:
                await _record_result(
                    status="failed",
                    failure_code="tenant_config_unavailable",
                )
                raise AgentCoreAdapterError(
                    503,
                    "tenant_config_unavailable",
                    "Tenant configuration is temporarily unavailable.",
                ) from exc
            except ValueError as exc:
                await _record_result(
                    status="rejected",
                    failure_code="invalid_payload",
                )
                raise AgentCoreAdapterError(
                    400,
                    "invalid_payload",
                    "Tenant configuration update is invalid.",
                ) from exc
            except Exception as exc:
                await _record_result(
                    status="failed",
                    failure_code="tenant_config_unavailable",
                )
                raise AgentCoreAdapterError(
                    503,
                    "tenant_config_unavailable",
                    "Tenant configuration is temporarily unavailable.",
                ) from exc
            await _record_result(
                status="committed",
                revision=committed.revision,
            )
            return _project_configuration(committed)

        if parsed.action is InvocationAction.QUERY:
            request = parsed.query_request
            if request is None:
                raise AgentCoreAdapterError(
                    400,
                    "invalid_payload",
                    "Query payload is required.",
                )
            if runtime.query_service is None:
                raise AgentCoreAdapterError(
                    503,
                    "query_service_unavailable",
                    "Query service is temporarily unavailable.",
                )
            try:
                result = await runtime.query_service.execute(
                    principal=identity.principal,
                    tenant_id=identity.tenant_id,
                    project_id=identity.project_id,
                    datasource_id=request.datasource_id,
                    sql=request.sql,
                    max_rows=request.max_rows,
                    request_id=request.request_id,
                    rehearsal=parsed.rehearsal,
                    rehearsal_binding=rehearsal_binding,
                    rehearsal_ledger=rehearsal_ledger,
                )
            except QueryServiceError as exc:
                raise AgentCoreAdapterError(
                    exc.status_code,
                    exc.code,
                    exc.message,
                ) from exc
            try:
                response = QueryInvocationResponse.from_mapping(
                    result,
                    expected_datasource_id=request.datasource_id,
                    expected_project_id=identity.project_id,
                    expected_request_id=request.request_id,
                )
            except QueryResponseValidationError as exc:
                logger.error(
                    "AgentCore query service returned an invalid response",
                    exc_info=True,
                )
                raise AgentCoreAdapterError(
                    502,
                    "invalid_query_response",
                    "Query service returned an invalid response.",
                ) from exc
            return response.to_dict()

        if parsed.request_data is None:
            raise AgentCoreAdapterError(
                400,
                "invalid_payload",
                "Chat payload is required.",
            )
        result = await runtime.gateway.handle_chat_completion(
            parsed.request_data,
            _gateway_context(
                identity,
                project,
                preferred_provider=parsed.preferred_provider,
                rehearsal=parsed.rehearsal,
                rehearsal_binding=rehearsal_binding,
                rehearsal_ledger=rehearsal_ledger,
            ),
        )
        if hasattr(result, "__aiter__"):
            return _forward_stream(result)
        return result
