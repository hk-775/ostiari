"""Bounded lifecycle and dependency readiness for AgentCore services."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from src.gateway.auth.principal import PrincipalResolver
from src.gateway.auth.project_repository import (
    ProjectConfigStore,
    ProjectResolver,
)
from src.gateway.models import RequestContext

if TYPE_CHECKING:
    from src.gateway.query.service import QueryService

logger = logging.getLogger(__name__)

DEFAULT_INITIALIZATION_TIMEOUT_SECONDS = 60.0
DEFAULT_READINESS_TIMEOUT_SECONDS = 5.0
DEFAULT_READINESS_CACHE_SECONDS = 5.0
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 10.0
INITIALIZATION_TIMEOUT_EXIT_CODE = 124


def _terminate_process_on_initialization_timeout(_: float) -> None:
    os._exit(INITIALIZATION_TIMEOUT_EXIT_CODE)


def _terminate_process_on_shutdown_timeout(_: float) -> None:
    os._exit(INITIALIZATION_TIMEOUT_EXIT_CODE)


class _ProcessDeadline:
    """Enforce lifecycle containment independently from the asyncio event loop."""

    def __init__(
        self,
        timeout_seconds: float,
        on_expired: Callable[[float], None],
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._on_expired = on_expired
        self._expires_at = time.monotonic() + timeout_seconds
        self._lock = threading.Lock()
        self._resolved = False
        self._expired = False
        self._must_expire = False
        self._timer = threading.Timer(timeout_seconds, self.expire)
        self._timer.daemon = True

    @property
    def remaining(self) -> float:
        return max(0.0, self._expires_at - time.monotonic())

    @property
    def expired(self) -> bool:
        with self._lock:
            return self._expired

    def start(self) -> None:
        self._timer.start()

    def require_expiration(self) -> None:
        with self._lock:
            if not self._resolved:
                self._must_expire = True

    def _invoke_expiration_handler(self) -> None:
        self._on_expired(self._timeout_seconds)
        logger.critical(
            "AgentCore lifecycle timeout handler returned without "
            "terminating the process"
        )

    def expire(self) -> bool:
        with self._lock:
            if self._resolved:
                return False
            self._resolved = True
            self._expired = True
        self._invoke_expiration_handler()
        return True

    def disarm(self) -> bool:
        with self._lock:
            if self._resolved or self._must_expire:
                return False
            if time.monotonic() >= self._expires_at:
                self._resolved = True
                self._expired = True
                invoke_expiration = True
            else:
                self._resolved = True
                self._timer.cancel()
                return True
        if invoke_expiration:
            self._invoke_expiration_handler()
        return False


def _consume_background_task(task: asyncio.Task[Any]) -> None:
    """Retrieve a detached task outcome without extending a deadline."""
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


def _cancel_detached_tasks(tasks: list[asyncio.Task[Any]]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
            task.add_done_callback(_consume_background_task)


class GatewayProtocol(Protocol):
    """Gateway operations used by the AgentCore adapter."""

    async def handle_chat_completion(
        self,
        request_data: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any] | AsyncIterator[dict[str, Any]]: ...

    async def handle_embeddings(
        self,
        request_data: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def handle_list_models(
        self,
        project_id: str | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
        authorized_project: Any | None = None,
    ) -> dict[str, Any]: ...


class OIDCTokenVerifier(Protocol):
    """Cryptographic verifier for runtime-forwarded bearer tokens."""

    async def validate_oidc_jwt(self, token: str) -> RequestContext | None: ...


class PolicyService(Protocol):
    """Policy evaluation used by the AgentCore adapter."""

    async def evaluate(
        self,
        context: RequestContext,
        action: str,
        resource: str,
    ) -> str: ...


class ConfigSync(Protocol):
    """Request-path fleet convergence used by both runtime front doors."""

    async def refresh_if_stale(self) -> bool: ...


@dataclass(frozen=True)
class RuntimeDependency:
    """One bounded dependency check required for runtime readiness."""

    name: str
    check: Callable[[], Awaitable[bool | str]]
    startup_check: Callable[[], Awaitable[bool | str]]


@dataclass(frozen=True)
class RuntimeCloseHook:
    """One asynchronous cleanup operation owned by the runtime."""

    name: str
    close: Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class RuntimeReadiness:
    """Sanitized runtime and dependency readiness."""

    ready: bool
    state: str
    dependencies: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        status = "not_ready"
        if self.ready:
            status = (
                "degraded"
                if "degraded" in self.dependencies.values()
                else "ready"
            )
        return {
            "status": status,
            "ready": self.ready,
            "state": self.state,
            "dependencies": dict(self.dependencies),
        }


@dataclass(frozen=True)
class RuntimeServices:
    """AgentCore dependencies built and closed as one runtime unit."""

    gateway: GatewayProtocol
    token_verifier: OIDCTokenVerifier
    principal_resolver: PrincipalResolver
    project_resolver: ProjectResolver
    project_config_store: ProjectConfigStore | None = None
    audit_trail: Any | None = None
    query_service: QueryService | None = None
    policy_service: PolicyService | None = None
    config_sync: ConfigSync | None = None
    rehearsal_ledger: Any | None = None
    readiness_checks: tuple[RuntimeDependency, ...] = field(default_factory=tuple)
    close_hooks: tuple[RuntimeCloseHook, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        readiness_names = [check.name for check in self.readiness_checks]
        close_names = [hook.name for hook in self.close_hooks]
        for kind, names in (
            ("readiness check", readiness_names),
            ("close hook", close_names),
        ):
            if any(not name or name != name.strip() for name in names):
                raise ValueError(f"AgentCore {kind} names must be non-empty")
            if len(names) != len(set(names)):
                raise ValueError(f"AgentCore {kind} names must be unique")
        if "runtime" in readiness_names:
            raise ValueError("AgentCore readiness check name 'runtime' is reserved")

    async def _check_readiness(
        self,
        timeout_seconds: float,
        *,
        startup: bool,
    ) -> RuntimeReadiness:
        dependencies = {"runtime": "ready"}
        if not self.readiness_checks:
            return RuntimeReadiness(True, RuntimeState.READY.value, dependencies)

        async def _run(check: RuntimeDependency) -> str:
            try:
                callback = check.startup_check if startup else check.check
                result = await callback()
                if isinstance(result, bool):
                    return "ready" if result else "unavailable"
                if result in {"ready", "degraded", "unavailable"}:
                    return result
                return "unavailable"
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "AgentCore readiness dependency failed: %s",
                    check.name,
                    exc_info=True,
                )
                return "unavailable"

        tasks = {check.name: asyncio.create_task(_run(check)) for check in self.readiness_checks}
        try:
            done, _ = await asyncio.wait(
                tasks.values(),
                timeout=timeout_seconds,
            )
        except BaseException:
            _cancel_detached_tasks(list(tasks.values()))
            raise
        for name, task in tasks.items():
            if task in done and not task.cancelled():
                dependencies[name] = task.result()
            else:
                dependencies[name] = "timeout"
                _cancel_detached_tasks([task])

        ready = all(
            status in {"ready", "degraded"}
            for status in dependencies.values()
        )
        return RuntimeReadiness(ready, RuntimeState.READY.value, dependencies)

    async def check_startup_readiness(
        self,
        timeout_seconds: float,
    ) -> RuntimeReadiness:
        """Probe dependencies without touching service-loop-owned async state."""
        return await self._check_readiness(timeout_seconds, startup=True)

    async def check_readiness(self, timeout_seconds: float) -> RuntimeReadiness:
        """Probe shared dependencies on the authenticated-handler service loop."""
        return await self._check_readiness(timeout_seconds, startup=False)

    async def close(self, timeout_seconds: float) -> bool:
        """Run all registered cleanup hooks within one shared deadline."""
        if not self.close_hooks:
            return True

        async def _run(hook: RuntimeCloseHook) -> None:
            try:
                await hook.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "AgentCore shutdown hook failed: %s",
                    hook.name,
                    exc_info=True,
                )

        tasks = [asyncio.create_task(_run(hook)) for hook in self.close_hooks]
        try:
            _, pending = await asyncio.wait(
                tasks,
                timeout=timeout_seconds,
            )
        except BaseException:
            _cancel_detached_tasks(tasks)
            raise
        if pending:
            logger.warning(
                "AgentCore shutdown timed out with %d cleanup operation(s) pending",
                len(pending),
            )
            _cancel_detached_tasks(list(pending))
            return False
        return True


class RuntimeState(str, Enum):
    NOT_INITIALIZED = "not_initialized"
    INITIALIZING = "initializing"
    READY = "ready"
    FAILED = "failed"
    CLOSING = "closing"
    CLOSED = "closed"


class RuntimeInitializationError(RuntimeError):
    """The runtime could not become ready within its startup contract."""


class RuntimeUnavailableError(RuntimeError):
    """The explicitly initialized runtime is not available for requests."""


class _DependenciesUnavailable(RuntimeError):
    def __init__(self, readiness: RuntimeReadiness) -> None:
        super().__init__("required AgentCore dependencies are unavailable")
        self.readiness = readiness
        self.cleanup_incomplete = False


class _InitializationCleanupIncomplete(RuntimeError):
    """Initialization failed while runtime cleanup still owned native work."""


def _retain_deadline_for_initialization_failure(
    deadline: _ProcessDeadline | None,
    error: BaseException,
) -> None:
    if (
        deadline is not None
        and (
            (
                isinstance(error, _DependenciesUnavailable)
                and (
                    "timeout" in error.readiness.dependencies.values()
                    or error.cleanup_incomplete
                )
            )
            or isinstance(error, _InitializationCleanupIncomplete)
        )
    ):
        deadline.require_expiration()


def _validate_duration(value: float, name: str, *, allow_zero: bool = False) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be a finite {qualifier} duration")
    return value


def build_runtime_services() -> RuntimeServices:
    """Build production services and their AgentCore lifecycle contracts."""
    from src.gateway.admin.webhook_routes import WebhookAPI
    from src.gateway.auth.cedar_policy import CedarPolicyService
    from src.gateway.bootstrap import build_gateway_components
    from src.gateway.config_sync import ConfigSyncService
    from src.gateway.rehearsal_control import RehearsalControlLedger

    components = build_gateway_components()
    if components.oidc_service is None:
        raise RuntimeError("AgentCore OIDC verifier is not configured")
    if components.principal_resolver is None:
        raise RuntimeError("canonical principal resolution is not configured")
    if components.project_resolver is None:
        raise RuntimeError("tenant project resolution is not configured")
    config_sync = ConfigSyncService(
        projects=components.projects,
        user_configs=components.user_configs,
        cost_tracker=components.cost_tracker,
        persistence=components.persistence,
        model_registry=getattr(components, "registry", None),
        policy_resolver=components.policy_resolver,
        region_config=components.region_router.config,
        health_monitor=components.health_monitor,
    )
    # AgentCore has no Starlette admin-route construction step. Constructing the
    # manager here installs the dispatcher's tenant destination refresh hook so
    # canonical security events still converge across runtime replicas.
    WebhookAPI(
        dispatcher=components.event_dispatcher,
        persistence=components.persistence,
    )

    async def _identity_provider_ready() -> bool:
        verifier = components.oidc_service
        issuer = getattr(verifier, "_validated_oidc_issuer", None)
        audience = getattr(verifier, "_validated_oidc_audience", None)
        get_jwks = getattr(verifier, "_get_jwks", None)
        if not callable(issuer) or not callable(audience) or not callable(get_jwks):
            return False
        if issuer() is None or audience() is None:
            return False
        return await get_jwks() is not None

    async def _identity_provider_startup_ready() -> bool:
        verifier = components.oidc_service
        issuer = getattr(verifier, "_validated_oidc_issuer", None)
        audience = getattr(verifier, "_validated_oidc_audience", None)
        fetch_jwks = getattr(verifier, "_fetch_valid_jwks", None)
        if not callable(issuer) or not callable(audience) or not callable(fetch_jwks):
            return False
        if issuer() is None or audience() is None:
            return False
        # Fetch through a request-local HTTP client without acquiring or populating
        # the verifier's service-loop-owned JWKS cache and asyncio lock.
        return await fetch_jwks() is not None

    async def _principal_store_ready() -> bool:
        status = await components.persistence.health_status()
        return status.get("enabled") is True and status.get("reachable") is True

    async def _routing_configuration_ready() -> str:
        await config_sync.refresh_routing_if_stale()
        if config_sync.active_routing_snapshot is None:
            return "unavailable"
        status = config_sync.routing_config_status
        return (
            "degraded"
            if status.get("status") == "degraded"
            else "ready"
        )

    async def _event_outbox_startup_ready() -> bool:
        return await components.event_dispatcher.check_readiness()

    async def _event_outbox_ready() -> bool:
        dispatcher = components.event_dispatcher
        if dispatcher.outbox_enabled and not dispatcher.worker_running:
            await dispatcher.start()
        return await dispatcher.check_readiness()

    query_reconciliation_worker = getattr(
        components,
        "query_reconciliation_worker",
        None,
    )

    async def _query_reconciliation_startup_ready() -> bool:
        return (
            components.persistence.enabled
            and components.audit_trail.durable_enabled
        )

    async def _query_reconciliation_ready() -> bool:
        if query_reconciliation_worker is None:
            return False
        await query_reconciliation_worker.start()
        return query_reconciliation_worker.running

    async def _close_provider_factory() -> None:
        close = getattr(components.multi_factory, "close", None)
        if callable(close):
            await close()
            return
        client = getattr(components.multi_factory, "_http_client", None)
        close = getattr(client, "close", None)
        if callable(close):
            await close()

    async def _shutdown_otlp() -> None:
        exporter = getattr(components.gateway_agent, "_otlp_exporter", None)
        shutdown = getattr(exporter, "shutdown", None)
        if callable(shutdown):
            await asyncio.to_thread(shutdown)

    async def _close_trace_forwarder() -> None:
        forwarder = getattr(
            components.gateway_agent,
            "_trace_forwarder",
            None,
        )
        close = getattr(forwarder, "close", None)
        if callable(close):
            await close()

    # Bootstrap owns the canonical query repository, executor, and audit trail.
    query_service = getattr(components, "query_service", None)
    query_readiness = (
        (
            RuntimeDependency(
                "query_reconciliation",
                _query_reconciliation_ready,
                _query_reconciliation_startup_ready,
            ),
        )
        if query_reconciliation_worker is not None
        else ()
    )
    query_close_hooks = (
        (
            RuntimeCloseHook(
                "query_reconciliation",
                query_reconciliation_worker.stop,
            ),
        )
        if query_reconciliation_worker is not None
        else ()
    )
    return RuntimeServices(
        gateway=components.gateway_agent,
        token_verifier=components.oidc_service,
        principal_resolver=components.principal_resolver,
        project_resolver=components.project_resolver,
        project_config_store=components.project_resolver,
        audit_trail=components.audit_trail,
        query_service=query_service,
        policy_service=CedarPolicyService(
            components.policies,
            persistence=components.persistence,
        ),
        config_sync=config_sync,
        rehearsal_ledger=RehearsalControlLedger(),
        readiness_checks=(
            RuntimeDependency(
                "identity_provider",
                _identity_provider_ready,
                _identity_provider_startup_ready,
            ),
            RuntimeDependency(
                "principal_store",
                _principal_store_ready,
                _principal_store_ready,
            ),
            RuntimeDependency(
                "routing_configuration",
                _routing_configuration_ready,
                _routing_configuration_ready,
            ),
            RuntimeDependency(
                "security_event_outbox",
                _event_outbox_ready,
                _event_outbox_startup_ready,
            ),
        )
        + query_readiness,
        close_hooks=query_close_hooks
        + (
            RuntimeCloseHook(
                "spoke_health_monitor",
                components.health_monitor.stop,
            ),
            RuntimeCloseHook("provider_http", _close_provider_factory),
            RuntimeCloseHook(
                "security_event_outbox",
                components.event_dispatcher.stop,
            ),
            RuntimeCloseHook(
                "trace_forwarder",
                _close_trace_forwarder,
            ),
            RuntimeCloseHook("otlp", _shutdown_otlp),
        ),
    )


class RuntimeProvider:
    """Explicitly initialize, probe, share, and close AgentCore services."""

    def __init__(
        self,
        factory: Callable[[], RuntimeServices] = build_runtime_services,
        *,
        initialization_timeout_seconds: float = DEFAULT_INITIALIZATION_TIMEOUT_SECONDS,
        readiness_timeout_seconds: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
        readiness_cache_seconds: float = DEFAULT_READINESS_CACHE_SECONDS,
        shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        _initialization_timeout_handler: Callable[[float], None] | None = None,
        _shutdown_timeout_handler: Callable[[float], None] | None = None,
    ) -> None:
        self._factory = factory
        self._initialization_timeout = _validate_duration(
            initialization_timeout_seconds,
            "initialization_timeout_seconds",
        )
        self._readiness_timeout = _validate_duration(
            readiness_timeout_seconds,
            "readiness_timeout_seconds",
        )
        self._readiness_cache = _validate_duration(
            readiness_cache_seconds,
            "readiness_cache_seconds",
            allow_zero=True,
        )
        self._shutdown_timeout = _validate_duration(
            shutdown_timeout_seconds,
            "shutdown_timeout_seconds",
        )
        if _initialization_timeout_handler is not None and not callable(
            _initialization_timeout_handler
        ):
            raise TypeError("_initialization_timeout_handler must be callable")
        if _shutdown_timeout_handler is not None and not callable(
            _shutdown_timeout_handler
        ):
            raise TypeError("_shutdown_timeout_handler must be callable")
        self._initialization_timeout_handler = (
            _initialization_timeout_handler
            or _terminate_process_on_initialization_timeout
        )
        self._shutdown_timeout_handler = (
            _shutdown_timeout_handler
            or _terminate_process_on_shutdown_timeout
        )
        self._runtime: RuntimeServices | None = None
        self._initialization: asyncio.Task[tuple[RuntimeServices, RuntimeReadiness]] | None = None
        self._initialization_deadline: _ProcessDeadline | None = None
        self._closing: asyncio.Task[None] | None = None
        self._state = RuntimeState.NOT_INITIALIZED
        self._last_readiness = RuntimeReadiness(
            False,
            self._state.value,
            {"runtime": self._state.value},
        )
        self._last_readiness_at = 0.0
        self._last_service_readiness_at = 0.0
        self._service_loop_initialized = False
        self._lifecycle_epoch = 0
        self._lifecycle_lock = asyncio.Lock()
        self._startup_readiness_lock = asyncio.Lock()
        self._readiness_lock = asyncio.Lock()
        self._state_lock = threading.Lock()
        self._service_loop: asyncio.AbstractEventLoop | None = None
        self._service_loop_lock = threading.Lock()

    @property
    def state(self) -> RuntimeState:
        with self._state_lock:
            return self._state

    async def _build_and_check(
        self,
    ) -> tuple[RuntimeServices, RuntimeReadiness]:
        # Gateway bootstrap uses asyncio.run internally, so it must not execute
        # on AgentCore's active ASGI event loop.
        runtime = await asyncio.to_thread(self._factory)
        if not isinstance(runtime, RuntimeServices):
            raise TypeError("AgentCore runtime factory returned an invalid service unit")
        try:
            readiness = await runtime.check_startup_readiness(self._readiness_timeout)
            if not readiness.ready:
                raise _DependenciesUnavailable(readiness)
            return runtime, readiness
        except BaseException as exc:
            cleanup_complete = await runtime.close(self._shutdown_timeout)
            if not cleanup_complete:
                if isinstance(exc, _DependenciesUnavailable):
                    exc.cleanup_incomplete = True
                else:
                    raise _InitializationCleanupIncomplete(
                        "AgentCore initialization cleanup timed out"
                    ) from exc
            raise

    async def initialize(self) -> RuntimeServices:
        """Build and verify services before the application accepts traffic."""
        async with self._lifecycle_lock:
            with self._state_lock:
                if self._state is RuntimeState.READY and self._runtime is not None:
                    return self._runtime
                if self._state in {RuntimeState.CLOSING, RuntimeState.CLOSED}:
                    raise RuntimeInitializationError("AgentCore runtime is closed")
                if self._initialization is None:
                    self._initialization = asyncio.create_task(self._build_and_check())
                    self._initialization_deadline = _ProcessDeadline(
                        self._initialization_timeout,
                        self._initialization_timeout_handler,
                    )
                    self._initialization_deadline.start()
                initialization = self._initialization
                deadline = self._initialization_deadline
                self._state = RuntimeState.INITIALIZING

        if deadline is None:
            raise RuntimeInitializationError(
                "AgentCore runtime initialization deadline is unavailable"
            )

        try:
            done, _ = await asyncio.wait(
                (initialization,),
                timeout=deadline.remaining,
            )
        except asyncio.CancelledError:
            # The bootstrap task and its watchdog retain ownership. A caller
            # cancellation must not strand a synchronous worker.
            raise

        if initialization not in done or deadline.expired:
            deadline.expire()
            failure = RuntimeReadiness(
                False,
                RuntimeState.FAILED.value,
                {"runtime": "initialization_timeout"},
            )
            await self._record_initialization_failure(
                initialization,
                deadline,
                failure,
            )
            raise RuntimeInitializationError(
                "AgentCore runtime initialization timed out"
            )

        try:
            runtime, readiness = initialization.result()
        except _DependenciesUnavailable as exc:
            # A timed-out async wrapper can still own a native SDK worker.
            # Keep the process watchdog armed until the outer deadline.
            _retain_deadline_for_initialization_failure(deadline, exc)
            failure = RuntimeReadiness(
                False,
                RuntimeState.FAILED.value,
                dict(exc.readiness.dependencies),
            )
            await self._record_initialization_failure(
                initialization,
                deadline,
                failure,
            )
            raise RuntimeInitializationError("AgentCore runtime dependencies are unavailable") from exc
        except Exception as exc:
            _retain_deadline_for_initialization_failure(deadline, exc)
            failure = RuntimeReadiness(
                False,
                RuntimeState.FAILED.value,
                {"runtime": "initialization_failed"},
            )
            await self._record_initialization_failure(
                initialization,
                deadline,
                failure,
            )
            raise RuntimeInitializationError("AgentCore runtime initialization failed") from exc

        deadline_expired = False
        async with self._lifecycle_lock:
            with self._state_lock:
                if self._state in {RuntimeState.CLOSING, RuntimeState.CLOSED}:
                    raise RuntimeInitializationError("AgentCore runtime is closed")
                if self._state is RuntimeState.READY and self._runtime is runtime:
                    return runtime
                if not deadline.disarm():
                    deadline_expired = True
                else:
                    self._runtime = runtime
                    self._initialization = None
                    self._initialization_deadline = None
                    self._state = RuntimeState.READY
                    self._last_readiness = readiness
                    self._last_readiness_at = time.monotonic()
                    self._last_service_readiness_at = 0.0
                    self._service_loop_initialized = False

        if deadline_expired:
            await runtime.close(self._shutdown_timeout)
            failure = RuntimeReadiness(
                False,
                RuntimeState.FAILED.value,
                {"runtime": "initialization_timeout"},
            )
            await self._record_initialization_failure(
                initialization,
                deadline,
                failure,
            )
            raise RuntimeInitializationError(
                "AgentCore runtime initialization timed out"
            )
        return runtime

    async def _record_initialization_failure(
        self,
        initialization: asyncio.Task[tuple[RuntimeServices, RuntimeReadiness]],
        deadline: _ProcessDeadline,
        readiness: RuntimeReadiness,
    ) -> None:
        async with self._lifecycle_lock:
            with self._state_lock:
                if self._initialization is not initialization:
                    return
                if self._state not in {RuntimeState.CLOSING, RuntimeState.CLOSED}:
                    self._state = RuntimeState.FAILED
                    self._last_readiness = readiness
                    self._last_readiness_at = time.monotonic()
                if initialization.done() and not initialization.cancelled():
                    deadline_disarmed = deadline.disarm()
                    self._initialization = None
                    if (
                        deadline_disarmed
                        and self._initialization_deadline is deadline
                    ):
                        self._initialization_deadline = None

    async def get(self) -> RuntimeServices:
        """Return only services that completed explicit startup initialization."""
        with self._state_lock:
            runtime = self._runtime
            if self._state is not RuntimeState.READY or runtime is None:
                raise RuntimeUnavailableError("AgentCore runtime is not ready")

        current_loop = asyncio.get_running_loop()
        with self._service_loop_lock:
            if self._service_loop is None:
                self._service_loop = current_loop
            elif self._service_loop is not current_loop:
                raise RuntimeUnavailableError("AgentCore runtime request loop changed")

        with self._state_lock:
            if self._state is not RuntimeState.READY or self._runtime is not runtime:
                raise RuntimeUnavailableError("AgentCore runtime is not ready")
            service_loop_initialized = self._service_loop_initialized

        if not service_loop_initialized:
            readiness = await self._probe_service_readiness(
                runtime,
                activate=True,
            )
            if not readiness.ready:
                raise RuntimeUnavailableError("AgentCore runtime dependencies are not ready")

        with self._state_lock:
            if (
                self._state is not RuntimeState.READY
                or self._runtime is not runtime
                or not self._service_loop_initialized
            ):
                raise RuntimeUnavailableError("AgentCore runtime is not ready")
        return runtime

    def _unavailable_readiness_locked(self) -> RuntimeReadiness:
        dependencies = {"runtime": self._state.value}
        if self._state is RuntimeState.FAILED:
            dependencies = dict(self._last_readiness.dependencies)
        return RuntimeReadiness(
            False,
            self._state.value,
            dependencies,
        )

    def _commit_readiness(
        self,
        runtime: RuntimeServices,
        lifecycle_epoch: int,
        readiness: RuntimeReadiness,
        *,
        service_probe: bool,
        activate: bool = False,
    ) -> RuntimeReadiness:
        with self._state_lock:
            if (
                self._lifecycle_epoch != lifecycle_epoch
                or self._state is not RuntimeState.READY
                or self._runtime is not runtime
            ):
                return self._unavailable_readiness_locked()
            now = time.monotonic()
            self._last_readiness = readiness
            self._last_readiness_at = now
            if service_probe:
                self._last_service_readiness_at = now
            if activate and readiness.ready:
                self._service_loop_initialized = True
            return readiness

    async def _probe_startup_readiness(
        self,
        runtime: RuntimeServices,
        *,
        force: bool,
    ) -> RuntimeReadiness:
        async with self._startup_readiness_lock:
            with self._state_lock:
                if self._state is not RuntimeState.READY or self._runtime is not runtime:
                    return self._unavailable_readiness_locked()
                now = time.monotonic()
                if not force and now - self._last_readiness_at < self._readiness_cache:
                    return self._last_readiness
                lifecycle_epoch = self._lifecycle_epoch

            readiness = await runtime.check_startup_readiness(self._readiness_timeout)
            return self._commit_readiness(
                runtime,
                lifecycle_epoch,
                readiness,
                service_probe=False,
            )

    async def _probe_service_readiness(
        self,
        runtime: RuntimeServices,
        *,
        force: bool = False,
        activate: bool = False,
    ) -> RuntimeReadiness:
        async with self._readiness_lock:
            with self._state_lock:
                if self._state is not RuntimeState.READY or self._runtime is not runtime:
                    return self._unavailable_readiness_locked()
                if activate and self._service_loop_initialized:
                    return RuntimeReadiness(
                        True,
                        RuntimeState.READY.value,
                        {"runtime": "ready"},
                    )
                now = time.monotonic()
                if (
                    not force
                    and self._last_service_readiness_at > 0
                    and now - self._last_service_readiness_at < self._readiness_cache
                ):
                    return self._last_readiness
                lifecycle_epoch = self._lifecycle_epoch

            readiness = await runtime.check_readiness(self._readiness_timeout)
            return self._commit_readiness(
                runtime,
                lifecycle_epoch,
                readiness,
                service_probe=True,
                activate=activate,
            )

    async def readiness(self, *, force: bool = False) -> RuntimeReadiness:
        """Check dependencies on the loop that owns their shared async state."""
        with self._state_lock:
            runtime = self._runtime
            if self._state is not RuntimeState.READY or runtime is None:
                return self._unavailable_readiness_locked()
            lifecycle_epoch = self._lifecycle_epoch

        with self._service_loop_lock:
            service_loop = self._service_loop

        if service_loop is None:
            return await self._probe_startup_readiness(
                runtime,
                force=force,
            )

        current_loop = asyncio.get_running_loop()
        if service_loop is current_loop:
            return await self._probe_service_readiness(
                runtime,
                force=force,
            )
        if not service_loop.is_running():
            failure = RuntimeReadiness(
                False,
                RuntimeState.READY.value,
                {"runtime": "service_loop_unavailable"},
            )
            return self._commit_readiness(
                runtime,
                lifecycle_epoch,
                failure,
                service_probe=True,
            )

        readiness_future = asyncio.run_coroutine_threadsafe(
            self._probe_service_readiness(runtime, force=force),
            service_loop,
        )
        try:
            return await asyncio.wait_for(
                asyncio.wrap_future(readiness_future),
                timeout=self._readiness_timeout + 0.25,
            )
        except TimeoutError:
            readiness_future.cancel()
            failure = RuntimeReadiness(
                False,
                RuntimeState.READY.value,
                {"runtime": "service_loop_timeout"},
            )
            return self._commit_readiness(
                runtime,
                lifecycle_epoch,
                failure,
                service_probe=True,
            )

    async def _close_runtime_with_lock(
        self,
        runtime: RuntimeServices,
        lock: asyncio.Lock,
    ) -> bool:
        try:
            async with asyncio.timeout(self._shutdown_timeout):
                async with lock:
                    return await runtime.close(self._shutdown_timeout)
        except TimeoutError:
            logger.warning("AgentCore runtime cleanup did not finish before shutdown")
            return False

    async def _close_runtime(self, runtime: RuntimeServices) -> bool:
        with self._service_loop_lock:
            service_loop = self._service_loop

        current_loop = asyncio.get_running_loop()
        if service_loop is None:
            return await self._close_runtime_with_lock(
                runtime,
                self._startup_readiness_lock,
            )
        if service_loop is current_loop:
            return await self._close_runtime_with_lock(
                runtime,
                self._readiness_lock,
            )
        if not service_loop.is_running():
            return await runtime.close(self._shutdown_timeout)

        close_future = asyncio.run_coroutine_threadsafe(
            self._close_runtime_with_lock(runtime, self._readiness_lock),
            service_loop,
        )
        try:
            return await asyncio.wait_for(
                asyncio.wrap_future(close_future),
                timeout=self._shutdown_timeout + 0.1,
            )
        except TimeoutError:
            close_future.cancel()
            logger.warning("AgentCore runtime cleanup did not finish on the request loop")
            return False

    def _close_late_initialization(
        self,
        initialization: asyncio.Task[tuple[RuntimeServices, RuntimeReadiness]],
        deadline: _ProcessDeadline | None,
    ) -> None:
        """Close a factory result that arrived after shutdown's deadline."""

        def _schedule_close(
            completed: asyncio.Task[tuple[RuntimeServices, RuntimeReadiness]],
        ) -> None:
            if completed.cancelled():
                return
            try:
                runtime, _ = completed.result()
            except BaseException as exc:
                _retain_deadline_for_initialization_failure(deadline, exc)
                if deadline is not None:
                    deadline.disarm()
                return
            loop = completed.get_loop()
            if loop.is_running():
                async def _close_and_disarm() -> None:
                    cleanup_complete = await runtime.close(self._shutdown_timeout)
                    if cleanup_complete and deadline is not None:
                        deadline.disarm()
                    elif deadline is not None:
                        deadline.require_expiration()

                loop.create_task(_close_and_disarm())
            else:
                logger.warning("Late AgentCore initialization completed after its loop stopped")

        initialization.add_done_callback(_schedule_close)

    async def _close_owned_resources(
        self,
        runtime: RuntimeServices | None,
        initialization: asyncio.Task[
            tuple[RuntimeServices, RuntimeReadiness]
        ] | None,
        initialization_deadline: _ProcessDeadline | None,
    ) -> bool:
        if runtime is None and initialization is not None:
            try:
                done, _ = await asyncio.wait(
                    (initialization,),
                    timeout=self._shutdown_timeout,
                )
            except asyncio.CancelledError:
                self._close_late_initialization(
                    initialization,
                    initialization_deadline,
                )
                raise
            if initialization not in done:
                self._close_late_initialization(
                    initialization,
                    initialization_deadline,
                )
                logger.warning("AgentCore initialization was still running at shutdown")
                initialization_deadline = None
                return False
            elif initialization.cancelled():
                initialization_deadline = None
                return False
            else:
                try:
                    runtime, _ = initialization.result()
                except Exception as exc:
                    _retain_deadline_for_initialization_failure(
                        initialization_deadline,
                        exc,
                    )
                    if initialization_deadline is not None:
                        initialization_deadline.disarm()
                    initialization_deadline = None

        if runtime is not None:
            cleanup_complete = await self._close_runtime(runtime)
            if cleanup_complete and initialization_deadline is not None:
                initialization_deadline.disarm()
            elif initialization_deadline is not None:
                initialization_deadline.require_expiration()
            return cleanup_complete
        return True

    async def _finish_close(
        self,
        runtime: RuntimeServices | None,
        initialization: asyncio.Task[
            tuple[RuntimeServices, RuntimeReadiness]
        ] | None,
        initialization_deadline: _ProcessDeadline | None,
        shutdown_deadline: _ProcessDeadline,
    ) -> None:
        cleanup_complete = False
        try:
            cleanup_complete = await self._close_owned_resources(
                runtime,
                initialization,
                initialization_deadline,
            )
        finally:
            if cleanup_complete:
                shutdown_deadline.disarm()
            else:
                shutdown_deadline.require_expiration()
            async with self._lifecycle_lock:
                with self._state_lock:
                    self._state = RuntimeState.CLOSED
                    self._last_readiness = RuntimeReadiness(
                        False,
                        self._state.value,
                        {"runtime": self._state.value},
                    )
                    self._last_readiness_at = time.monotonic()
                    if self._closing is asyncio.current_task():
                        self._closing = None

    async def close(self) -> None:
        """Stop accepting services and retain cleanup ownership if cancelled."""
        async with self._lifecycle_lock:
            with self._state_lock:
                if self._state is RuntimeState.CLOSED:
                    return
                if self._closing is None:
                    self._state = RuntimeState.CLOSING
                    self._lifecycle_epoch += 1
                    runtime = self._runtime
                    initialization = self._initialization
                    initialization_deadline = self._initialization_deadline
                    self._runtime = None
                    self._initialization = None
                    self._initialization_deadline = None
                    self._service_loop_initialized = False
                    shutdown_deadline = _ProcessDeadline(
                        self._shutdown_timeout,
                        self._shutdown_timeout_handler,
                    )
                    shutdown_deadline.start()
                    self._closing = asyncio.create_task(
                        self._finish_close(
                            runtime,
                            initialization,
                            initialization_deadline,
                            shutdown_deadline,
                        )
                    )
                closing = self._closing

        await asyncio.shield(closing)
