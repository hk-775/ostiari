"""Infrastructure-neutral AxonLLM adapter for an embedding Ostiari host."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    UsageRecord,
)
from src.gateway.routing_config import RoutingConfigSnapshot
from src.gateway.routing_config_contract import (
    validate_routing_config_signing_key_arn,
)

from .hosts import IdentityContext, OstiariHost
from .router import AsyncRouter, InvalidRequestError

logger = logging.getLogger(__name__)


class OstiariAdapterError(RuntimeError):
    """Base error raised by the embedded Ostiari delivery adapter."""


class OstiariAdapterNotStartedError(OstiariAdapterError):
    """Raised when Ostiari invokes the router outside its owned lifecycle."""


class OstiariConfigurationError(OstiariAdapterError):
    """Raised when a host supplies an unsafe or inconsistent configuration."""


class OstiariRoutingModeUnavailableError(OstiariAdapterError):
    """Raised when smart or ensemble routing was not configured on the core."""


class OstiariUsageRecordingError(OstiariAdapterError):
    """Usage persistence failed after the provider returned a valid result.

    ``result`` lets Ostiari avoid a second provider call. A host may return the
    completed result while marking accounting degraded, or fail the request
    without retrying the model invocation.
    """

    def __init__(
        self,
        result: OstiariResult,
        cause: BaseException,
    ) -> None:
        self.result = result
        self.cause = cause
        super().__init__(
            "AxonLLM completed the provider call but Ostiari usage recording "
            "failed; do not retry the provider invocation"
        )


@dataclass(frozen=True)
class OstiariResult:
    """Provider-neutral result consumed by Ostiari's gateway shims."""

    content: str | None
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost: float
    tool_calls: tuple[dict[str, Any], ...] = ()
    finish_reason: str | None = None
    request_id: str = ""
    provider_request_id: str = ""
    routing_strategy: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)


def _last_user_text(messages: Sequence[Mapping[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, Mapping) and part.get("type") in {"text", "input_text"}
            )
    return ""


def _flatten_system(system: Any) -> str:
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(
            str(block.get("text", "")) for block in system if isinstance(block, Mapping) and block.get("type") == "text"
        )
    return str(system or "")


class OstiariRouterAdapter:
    """Run the AxonLLM routing core inside an Ostiari-owned lifecycle.

    The adapter creates no server, worker, database, AWS client, or network
    listener. Ostiari supplies verified routing snapshots, credential
    resolution, request identity, telemetry, durable usage, and lifecycle.
    """

    def __init__(
        self,
        router: AsyncRouter,
        host: OstiariHost,
        *,
        trusted_signing_key_arn: str,
        owns_router: bool = True,
    ) -> None:
        if not isinstance(router, AsyncRouter):
            raise TypeError("router must be an AsyncRouter")
        if not isinstance(host, OstiariHost):
            raise TypeError("host does not implement the OstiariHost protocol")
        if not isinstance(trusted_signing_key_arn, str) or not trusted_signing_key_arn.strip():
            raise ValueError("trusted_signing_key_arn must be a non-empty string")
        validate_routing_config_signing_key_arn(trusted_signing_key_arn)
        self._router = router
        self._host = host
        self._trusted_signing_key_arn = trusted_signing_key_arn
        self._owns_router = owns_router
        self._started = False
        self._closed = False
        self._error = ""
        self._active_snapshot: RoutingConfigSnapshot | None = None
        self._lifecycle_started = False
        self._lifecycle_lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        """Whether the adapter is started, configured, and open."""
        return self._started and not self._closed and not self._error

    @property
    def error(self) -> str:
        """Sanitized startup or lifecycle failure detail."""
        return self._error

    @property
    def active_snapshot(self) -> RoutingConfigSnapshot:
        """Return the signed snapshot currently adopted by this adapter."""
        self._ensure_ready()
        if self._active_snapshot is None:
            raise OstiariConfigurationError("Ostiari routing snapshot is unavailable")
        return self._active_snapshot

    async def start(self) -> None:
        """Start host dependencies and adopt the verified routing snapshot."""
        async with self._lifecycle_lock:
            if self._closed:
                raise OstiariAdapterError("Ostiari adapter is closed")
            if self._started:
                return
            try:
                await self._host.start()
                self._lifecycle_started = True
                snapshot = await self._host.load_snapshot()
                self._adopt_snapshot(snapshot)
            except BaseException as exc:
                self._error = type(exc).__name__
                if self._lifecycle_started:
                    try:
                        await self._host.close()
                    except Exception:
                        logger.warning(
                            "Ostiari host cleanup failed after startup error",
                            exc_info=True,
                        )
                    self._lifecycle_started = False
                raise
            self._error = ""
            self._started = True

    def require(self) -> None:
        """Fail if Ostiari did not start the adapter successfully."""
        self._ensure_ready()

    async def refresh_configuration(self) -> RoutingConfigSnapshot:
        """Adopt a newer host-verified snapshot, preserving last-known-good."""
        self._ensure_ready()
        snapshot = await self._host.load_snapshot()
        self._adopt_snapshot(snapshot)
        return self.active_snapshot

    async def publish_configuration(
        self,
        config: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> RoutingConfigSnapshot:
        """Validate, publish, bind, and adopt the next routing revision."""
        self._ensure_ready()
        active = self.active_snapshot
        if expected_revision != active.revision:
            raise OstiariConfigurationError("routing configuration revision changed")
        if not isinstance(config, Mapping):
            raise TypeError("config must be a mapping")
        candidate = RoutingConfigSnapshot.from_config(
            dict(config),
            revision=expected_revision + 1,
        )
        published = await self._host.publish_snapshot(
            dict(config),
            expected_revision=expected_revision,
        )
        self._validate_snapshot(published)
        if (
            published.revision != candidate.revision
            or published.document != candidate.document
            or published.sha256 != candidate.sha256
        ):
            raise OstiariConfigurationError("published routing snapshot does not match the validated candidate")
        self._adopt_snapshot(published)
        return published

    async def configure_provider_routes(
        self,
        routes: Sequence[Mapping[str, Any]],
    ) -> dict[str, int]:
        """Resolve opaque credentials and atomically replace provider routes."""
        self._ensure_ready()
        if not isinstance(routes, Sequence) or isinstance(routes, (str, bytes)):
            raise TypeError("provider routes must be a sequence of mappings")

        resolved: list[dict[str, Any]] = []
        for route in routes:
            if not isinstance(route, Mapping):
                raise TypeError("provider route must be a mapping")
            candidate = copy.deepcopy(dict(route))
            provider = str(candidate.get("provider") or "").strip()
            if not provider:
                raise ValueError("provider route provider is required")
            inline_credentials = candidate.pop("credentials", None)
            if inline_credentials:
                raise OstiariConfigurationError(
                    "provider routes must use credential_reference instead of inline credentials"
                )
            reference = candidate.pop("credential_reference", None)
            if reference is not None:
                if not isinstance(reference, str) or not reference.strip():
                    raise ValueError("credential_reference must be a non-empty string")
                credentials = await self._host.resolve(
                    provider=provider,
                    reference=reference,
                )
                if not isinstance(credentials, Mapping):
                    raise OstiariConfigurationError("credential resolver returned a non-mapping value")
                normalized = {
                    str(key): str(value)
                    for key, value in credentials.items()
                    if str(key) and value is not None and str(value)
                }
                if not normalized:
                    raise OstiariConfigurationError("credential resolver returned no transport fields")
                candidate["credentials"] = normalized
            resolved.append(candidate)

        return self._router.configure_routes(resolved)

    def model_registry_config(self) -> dict[str, Any]:
        """Return a detached credential-free routing configuration."""
        return copy.deepcopy(self.active_snapshot.config)

    def provider_route_snapshot(self) -> list[dict]:
        """Return route health and metadata without credential values."""
        self._ensure_ready()
        return self._router.route_snapshot()

    def knows_model(self, model: str) -> bool:
        self._ensure_ready()
        return self._router.knows_model(model)

    def model_available(self, model: str) -> bool:
        self._ensure_ready()
        return self._router.model_available(model)

    def has_available_models(self) -> bool:
        self._ensure_ready()
        return self._router.has_available_models()

    @staticmethod
    def supports_tools() -> bool:
        """The stable embedded request contract preserves OpenAI tool specs."""
        return True

    async def route(
        self,
        messages: list[dict[str, Any]],
        *,
        identity: IdentityContext,
        model: str = "",
        max_tokens: int = 4096,
        temperature: float | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        smart: bool = False,
        ensemble: str | bool = False,
        preferred_provider: str | None = None,
        session_id: str = "",
        system: Any = None,
    ) -> OstiariResult:
        """Route one governed Ostiari call through the in-process core."""
        self._ensure_ready()
        if not isinstance(identity, IdentityContext):
            raise TypeError("identity must be an IdentityContext")
        if not isinstance(session_id, str):
            raise TypeError("session_id must be a string")

        request_id = f"req_{uuid.uuid4().hex}"
        started_at = time.perf_counter()
        routed_messages: list[dict[str, Any]] = []
        system_text = _flatten_system(system)
        if system_text:
            routed_messages.append({"role": "system", "content": system_text})
        routed_messages.extend(copy.deepcopy(messages))

        smart = bool(smart or not model) and not bool(ensemble)
        request = ChatCompletionRequest(
            model=model,
            messages=routed_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stop=stop,
            stream=False,
            tools=copy.deepcopy(tools),
            tool_choice=copy.deepcopy(tool_choice),
        )
        errors = self._router._validator.validate(
            request,
            check_model=not (smart or ensemble),
            require_model=not (smart or ensemble),
        )
        if errors:
            raise InvalidRequestError(errors)

        routing_strategy = "configured"
        task_type = ""
        cost_override: float | None = None
        try:
            if ensemble:
                response, routing_strategy, cost_override = await self._route_ensemble(
                    request,
                    identity=identity,
                    ensemble=ensemble,
                )
            elif smart:
                response, task_type = await self._route_smart(
                    request,
                    identity=identity,
                    preferred_provider=preferred_provider,
                )
                routing_strategy = "smart"
            else:
                response = await self._router._runtime.complete(
                    request,
                    preferred_provider=preferred_provider,
                )
        except BaseException as exc:
            await self._emit_telemetry(
                {
                    "schema": "axonllm.ostiari.routing.v1",
                    "request_id": request_id,
                    "tenant_id": identity.tenant_id,
                    "project_id": identity.project_id,
                    "principal_id": identity.principal_id,
                    "session_id": session_id,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "routing_strategy": routing_strategy,
                    "latency_ms": (time.perf_counter() - started_at) * 1000,
                }
            )
            raise

        latency_ms = (time.perf_counter() - started_at) * 1000
        result = self._result_from_response(
            response,
            request_id=request_id,
            routing_strategy=routing_strategy,
            cost=(cost_override if cost_override is not None else self._calculate_cost(response)),
        )
        usage = UsageRecord(
            request_id=request_id,
            project_id=identity.project_id,
            user_id=identity.principal_id,
            tenant_id=identity.tenant_id,
            provider=result.provider,
            model=result.model,
            prompt_tokens=result.input_tokens,
            completion_tokens=result.output_tokens,
            total_tokens=result.input_tokens + result.output_tokens,
            cost=result.cost,
            timestamp=datetime.now(timezone.utc),
            latency_ms=latency_ms,
            status="success",
            routing_strategy=routing_strategy,
            task_type=task_type,
            provider_request_id=result.provider_request_id,
        )
        try:
            await self._host.record(usage)
        except BaseException as exc:
            await self._emit_telemetry(
                self._telemetry_event(
                    usage,
                    session_id=session_id,
                    status="accounting_failed",
                )
            )
            raise OstiariUsageRecordingError(result, exc) from exc

        await self._emit_telemetry(
            self._telemetry_event(
                usage,
                session_id=session_id,
                status="success",
            )
        )
        return result

    async def close(self) -> None:
        """Close the router and host lifecycle exactly once."""
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._started = False
            errors: list[BaseException] = []
            if self._owns_router:
                try:
                    await self._router.close()
                except BaseException as exc:
                    errors.append(exc)
            if self._lifecycle_started:
                try:
                    await self._host.close()
                except BaseException as exc:
                    errors.append(exc)
                self._lifecycle_started = False
            if errors:
                raise OstiariAdapterError("Ostiari adapter shutdown failed") from errors[0]

    def _ensure_ready(self) -> None:
        if self._closed:
            raise OstiariAdapterNotStartedError("Ostiari adapter is closed")
        if not self._started:
            detail = f": {self._error}" if self._error else ""
            raise OstiariAdapterNotStartedError(f"Ostiari adapter has not been started{detail}")

    def _validate_snapshot(self, snapshot: RoutingConfigSnapshot) -> None:
        if not isinstance(snapshot, RoutingConfigSnapshot):
            raise OstiariConfigurationError("host returned an invalid routing snapshot")
        if not snapshot.is_signed:
            raise OstiariConfigurationError("Ostiari routing snapshots must be signed")
        if snapshot.signing_key_arn != self._trusted_signing_key_arn:
            raise OstiariConfigurationError("routing snapshot uses an unexpected signing key")

    def _adopt_snapshot(self, snapshot: RoutingConfigSnapshot) -> None:
        self._validate_snapshot(snapshot)
        current = self._router.config_snapshot()
        if self._active_snapshot is None:
            if snapshot.revision < current.revision:
                raise OstiariConfigurationError("routing configuration revision rollback is forbidden")
            self._router.apply_snapshot(snapshot)
            self._active_snapshot = snapshot
            return

        active = self._active_snapshot
        if snapshot.revision < active.revision:
            raise OstiariConfigurationError("routing configuration revision rollback is forbidden")
        if snapshot.revision == active.revision:
            if snapshot.sha256 != active.sha256:
                raise OstiariConfigurationError("routing configuration revision equivocation detected")
        else:
            self._router.apply_snapshot(snapshot)
        self._active_snapshot = snapshot

    async def _route_smart(
        self,
        request: ChatCompletionRequest,
        *,
        identity: IdentityContext,
        preferred_provider: str | None,
    ) -> tuple[ChatCompletionResponse, str]:
        router = self._router._runtime.router
        if getattr(router, "_smart_strategy", None) is None:
            raise OstiariRoutingModeUnavailableError("smart routing is not configured")
        prompt = _last_user_text(request.messages)
        response, decision = await router.smart_route(
            request,
            self._router._runtime.provider_factory,
            prompt,
            project_id=identity.project_id,
            user_id=identity.principal_id,
            tenant_id=identity.tenant_id,
        )
        if preferred_provider and response.provider != preferred_provider:
            logger.debug(
                "smart routing selected provider %s instead of preferred %s",
                response.provider,
                preferred_provider,
            )
        return response, decision.task_type

    async def _route_ensemble(
        self,
        request: ChatCompletionRequest,
        *,
        identity: IdentityContext,
        ensemble: str | bool,
    ) -> tuple[ChatCompletionResponse, str, float]:
        router = self._router._runtime.router
        config = getattr(router, "_ensemble_config", None)
        if config is None or not config.is_configured:
            raise OstiariRoutingModeUnavailableError("ensemble routing is not configured")
        if ensemble is True:
            preset = config.default_preset()
        elif isinstance(ensemble, str) and ensemble.strip():
            preset = config.get_preset(ensemble.strip())
        else:
            preset = None
        if preset is None:
            raise OstiariRoutingModeUnavailableError("requested ensemble preset is not configured")
        request.model = preset.judge
        response, decision = await router.ensemble_route(
            request,
            self._router._runtime.provider_factory,
            _last_user_text(request.messages),
            preset,
            project_id=identity.project_id,
            user_id=identity.principal_id,
            tenant_id=identity.tenant_id,
        )
        return response, f"ensemble:{preset.name}", decision.total_cost

    def _calculate_cost(self, response: ChatCompletionResponse) -> float:
        tracker = getattr(self._router._runtime.router, "_cost_tracker", None)
        if tracker is None:
            return 0.0
        usage = response.usage
        return tracker.calculate_cost(
            response.provider,
            response.provider_model or response.model,
            usage.prompt_tokens,
            usage.completion_tokens,
            cached_tokens=usage.cached_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
        )

    @staticmethod
    def _result_from_response(
        response: ChatCompletionResponse,
        *,
        request_id: str,
        routing_strategy: str,
        cost: float,
    ) -> OstiariResult:
        choice = response.choices[0] if response.choices else {}
        message = choice.get("message") or {}
        tool_calls = tuple(copy.deepcopy(message.get("tool_calls") or []))
        raw = {
            "id": response.id,
            "object": "chat.completion",
            "model": response.model,
            "provider": response.provider,
            "choices": copy.deepcopy(response.choices),
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
                "cached_tokens": response.usage.cached_tokens,
                "cache_creation_tokens": (response.usage.cache_creation_tokens),
            },
        }
        return OstiariResult(
            content=message.get("content"),
            model=response.model,
            provider=response.provider,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            cost=cost,
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason"),
            request_id=request_id,
            provider_request_id=response.id,
            routing_strategy=routing_strategy,
            raw=raw,
        )

    @staticmethod
    def _telemetry_event(
        usage: UsageRecord,
        *,
        session_id: str,
        status: str,
    ) -> dict[str, Any]:
        return {
            "schema": "axonllm.ostiari.routing.v1",
            "request_id": usage.request_id,
            "tenant_id": usage.tenant_id,
            "project_id": usage.project_id,
            "principal_id": usage.user_id,
            "session_id": session_id,
            "provider": usage.provider,
            "model": usage.model,
            "input_tokens": usage.prompt_tokens,
            "output_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "cost": usage.cost,
            "latency_ms": usage.latency_ms,
            "routing_strategy": usage.routing_strategy,
            "task_type": usage.task_type,
            "status": status,
        }

    async def _emit_telemetry(self, event: Mapping[str, Any]) -> None:
        try:
            await self._host.emit(copy.deepcopy(dict(event)))
        except Exception:
            logger.warning(
                "Ostiari telemetry sink failed",
                exc_info=True,
            )


__all__ = [
    "OstiariAdapterError",
    "OstiariAdapterNotStartedError",
    "OstiariConfigurationError",
    "OstiariResult",
    "OstiariRouterAdapter",
    "OstiariRoutingModeUnavailableError",
    "OstiariUsageRecordingError",
]
