"""Concrete provider routes and adaptive route selection.

The model router chooses a logical provider.  ``ProviderRoutePool`` then chooses
the concrete credential, endpoint, and region used for that provider call.
Route state is intentionally process-local: latency and failures are properties
of this gateway instance's network path, while durable desired configuration is
owned by the control plane.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from src.gateway.config import VALID_PROVIDERS
from src.gateway.provider_config import AccessTokenProvider, ProviderConfig

_ROUTE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROVIDER_ALIASES = {
    "azure": "azure_openai",
    "vertex": "vertex_ai",
}
_DEFAULT_ENDPOINTS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
    "azure_openai": "",
    "vertex_ai": "https://us-central1-aiplatform.googleapis.com",
    "google_ai": "https://generativelanguage.googleapis.com",
    "cohere": "https://api.cohere.ai",
    "xai": "https://api.x.ai",
    "groq": "https://api.groq.com/openai",
    "together": "https://api.together.xyz",
    "fireworks": "https://api.fireworks.ai/inference",
    "ai21": "https://api.ai21.com/studio",
    "bedrock": "",
    "bedrock-mantle": "",
}


def canonical_provider(name: str) -> str:
    """Return AxonLLM's canonical provider name."""
    normalized = name.strip().lower()
    return _PROVIDER_ALIASES.get(normalized, normalized)


@dataclass(frozen=True)
class ProviderRoute:
    """One independently selectable provider credential/endpoint."""

    route_id: str
    provider: str
    endpoint: str = ""
    auth_type: str = "api_key"
    credentials: dict[str, str] = field(default_factory=dict, repr=False)
    region: str = ""
    allowed_models: frozenset[str] = field(default_factory=frozenset)
    weight: float = 1.0
    priority: int = 0
    enabled: bool = True
    max_concurrency: int = 100
    capacity_group: str = ""
    capacity_limit: int = 0
    connect_timeout: float = 30.0
    read_timeout: float = 120.0
    max_connections: int = 100
    max_connections_per_host: int = 100
    keepalive_timeout: float = 30.0
    extra_headers: dict[str, str] = field(default_factory=dict, repr=False)
    extra_params: dict[str, str] = field(default_factory=dict, repr=False)
    credential_provider: AccessTokenProvider | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        provider = canonical_provider(self.provider)
        object.__setattr__(self, "provider", provider)
        if not _ROUTE_ID_RE.fullmatch(self.route_id):
            raise ValueError(
                "route_id must start with an alphanumeric character and contain "
                "only letters, numbers, '.', '_', ':', or '-'"
            )
        if provider not in VALID_PROVIDERS:
            raise ValueError(f"unsupported provider route: {provider}")
        if self.endpoint:
            parsed = urlparse(self.endpoint)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("route endpoint must be an absolute HTTP(S) URL")
            if parsed.username or parsed.password:
                raise ValueError("route endpoint must not contain userinfo")
            if parsed.query or parsed.fragment:
                raise ValueError("route endpoint must not contain a query or fragment")
        if self.weight <= 0:
            raise ValueError("route weight must be greater than zero")
        if self.priority < 0:
            raise ValueError("route priority must be non-negative")
        if self.max_concurrency <= 0:
            raise ValueError("route max_concurrency must be greater than zero")
        if self.capacity_limit < 0:
            raise ValueError("route capacity_limit must be non-negative")
        for name, value in (
            ("connect_timeout", self.connect_timeout),
            ("read_timeout", self.read_timeout),
            ("keepalive_timeout", self.keepalive_timeout),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(
                    f"route {name} must be finite and greater than zero"
                )
        if self.max_connections <= 0 or self.max_connections_per_host <= 0:
            raise ValueError("route connection limits must be greater than zero")
        if (
            self.credential_provider is not None
            and not callable(
                getattr(self.credential_provider, "get_token", None)
            )
        ):
            raise ValueError(
                "route credential_provider must expose get_token()"
            )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProviderRoute":
        """Parse a control-plane or YAML route document."""
        provider = canonical_provider(str(data.get("provider", "")))
        route_id = str(data.get("route_id") or data.get("id") or "").strip()
        endpoint = str(
            data.get("endpoint")
            or data.get("base_url")
            or _DEFAULT_ENDPOINTS.get(provider, "")
        ).rstrip("/")
        region = str(data.get("region") or "").strip()
        credentials = {
            str(key): str(value)
            for key, value in (data.get("credentials") or {}).items()
            if value is not None and str(value)
        }
        if region and provider in {"bedrock", "bedrock-mantle"}:
            credentials.setdefault("region", region)
        extra_params = {
            str(key): str(value)
            for key, value in (data.get("extra_params") or {}).items()
            if value is not None
        }
        if data.get("project_id"):
            extra_params.setdefault("project", str(data["project_id"]))
        if region and provider == "vertex_ai":
            extra_params.setdefault("location", region)
        return cls(
            route_id=route_id,
            provider=provider,
            endpoint=endpoint,
            auth_type=str(data.get("auth_type") or _default_auth_type(provider)),
            credentials=credentials,
            region=region,
            allowed_models=frozenset(
                str(model) for model in (data.get("allowed_models") or []) if model
            ),
            weight=float(data.get("weight", 1.0)),
            priority=int(data.get("priority", 0)),
            enabled=bool(data.get("enabled", True)),
            max_concurrency=int(data.get("max_concurrency", 100)),
            capacity_group=str(data.get("capacity_group") or ""),
            capacity_limit=int(data.get("capacity_limit", 0)),
            connect_timeout=float(data.get("connect_timeout", 30.0)),
            read_timeout=float(data.get("read_timeout", 120.0)),
            max_connections=int(data.get("max_connections", 100)),
            max_connections_per_host=int(
                data.get("max_connections_per_host", 100)
            ),
            keepalive_timeout=float(data.get("keepalive_timeout", 30.0)),
            extra_headers={
                str(key): str(value)
                for key, value in (data.get("extra_headers") or {}).items()
            },
            extra_params=extra_params,
            credential_provider=data.get("credential_provider"),
        )

    @classmethod
    def from_provider_config(
        cls,
        config: ProviderConfig,
        *,
        route_id: str | None = None,
    ) -> "ProviderRoute":
        """Wrap a legacy single-provider config as one concrete route."""
        region = config.credentials.get("region", "")
        return cls(
            route_id=route_id or config.route_id or f"{config.provider_name}:default",
            provider=config.provider_name,
            endpoint=config.base_url,
            auth_type=config.auth_type,
            credentials=dict(config.credentials),
            region=region,
            connect_timeout=config.connect_timeout,
            read_timeout=config.read_timeout,
            max_connections=config.max_connections,
            max_connections_per_host=config.max_connections_per_host,
            keepalive_timeout=config.keepalive_timeout,
            extra_headers=dict(config.extra_headers),
            extra_params=dict(config.extra_params),
            credential_provider=config.credential_provider,
        )

    def supports_model(self, model_id: str) -> bool:
        return not self.allowed_models or model_id in self.allowed_models

    def to_provider_config(self) -> ProviderConfig:
        """Build the concrete HTTP configuration selected for one attempt."""
        credentials = dict(self.credentials)
        if self.region and self.auth_type == "aws_credentials":
            credentials.setdefault("region", self.region)
        return ProviderConfig(
            provider_name=self.provider,
            base_url=self.endpoint or _DEFAULT_ENDPOINTS.get(self.provider, ""),
            auth_type=self.auth_type,
            credentials=credentials,
            route_id=self.route_id,
            connect_timeout=self.connect_timeout,
            read_timeout=self.read_timeout,
            max_connections=self.max_connections,
            max_connections_per_host=self.max_connections_per_host,
            keepalive_timeout=self.keepalive_timeout,
            extra_headers=dict(self.extra_headers),
            extra_params=dict(self.extra_params),
            credential_provider=self.credential_provider,
        )

    def fingerprint(self) -> str:
        """Hash configuration so credential rotation resets stale health state."""
        payload = {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "auth_type": self.auth_type,
            "credentials": self.credentials,
            "region": self.region,
            "models": sorted(self.allowed_models),
            "capacity_group": self.capacity_group,
            "capacity_limit": self.capacity_limit,
            "connect_timeout": self.connect_timeout,
            "read_timeout": self.read_timeout,
            "extra_headers": self.extra_headers,
            "extra_params": self.extra_params,
            "credential_provider": (
                type(self.credential_provider).__qualname__
                if self.credential_provider is not None
                else ""
            ),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def _default_auth_type(provider: str) -> str:
    if provider in {"bedrock", "bedrock-mantle"}:
        return "aws_credentials"
    if provider == "azure_openai":
        return "azure_key"
    if provider == "vertex_ai":
        return "gcp_service_account"
    return "api_key"


@dataclass
class RouteRuntime:
    """Mutable health and load signals for one route."""

    status: str = "healthy"
    inflight: int = 0
    selected: int = 0
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    recovery_successes: int = 0
    latency_ewma_ms: float | None = None
    latency_per_token_ewma_ms: float | None = None
    error_ewma: float = 0.0
    cooldown_until: float = 0.0
    last_status_code: int | None = None
    last_selected_at: float | None = None


@dataclass(frozen=True)
class RouteLease:
    """A route reservation held for the lifetime of one provider attempt."""

    route: ProviderRoute
    model_id: str
    started_at: float
    generation: int = 1


class NoAvailableRouteError(RuntimeError):
    """Raised when a provider has no eligible route at selection time."""

    def __init__(self, provider: str, *, temporarily_unavailable: bool) -> None:
        self.provider = provider
        self.temporarily_unavailable = temporarily_unavailable
        super().__init__(f"No eligible route for provider '{provider}'")


class ProviderRoutePool:
    """Selects concrete routes using adaptive weighted random choice."""

    _EWMA_ALPHA = 0.2
    _AUTH_COOLDOWN_SECONDS = 300.0
    _THROTTLE_COOLDOWN_SECONDS = 30.0
    _FAILURE_COOLDOWN_SECONDS = 20.0
    _RECOVERY_FACTOR = 0.1

    def __init__(
        self,
        routes: list[ProviderRoute] | None = None,
        *,
        rng: random.Random | None = None,
        clock=time.monotonic,
    ) -> None:
        self._lock = threading.RLock()
        self._rng = rng or random.Random()
        self._clock = clock
        self._routes: dict[str, ProviderRoute] = {}
        self._by_provider: dict[str, list[str]] = {}
        self._runtime: dict[tuple[str, int], RouteRuntime] = {}
        self._generation_routes: dict[
            tuple[str, int],
            ProviderRoute,
        ] = {}
        self._active_generations: dict[str, int] = {}
        self._last_generations: dict[str, int] = {}
        self._fingerprints: dict[str, str] = {}
        self.replace(routes or [])

    def replace(self, routes: list[ProviderRoute]) -> None:
        """Atomically replace desired routes, preserving unchanged health."""
        route_map: dict[str, ProviderRoute] = {}
        by_provider: dict[str, list[str]] = {}
        for route in routes:
            if route.route_id in route_map:
                raise ValueError(f"duplicate provider route_id: {route.route_id}")
            route_map[route.route_id] = route
            by_provider.setdefault(route.provider, []).append(route.route_id)

        with self._lock:
            active_generations: dict[str, int] = {}
            fingerprints: dict[str, str] = {}
            for route_id, route in route_map.items():
                fingerprint = route.fingerprint()
                fingerprints[route_id] = fingerprint
                if (
                    self._fingerprints.get(route_id) == fingerprint
                    and route_id in self._active_generations
                ):
                    generation = self._active_generations[route_id]
                else:
                    generation = self._last_generations.get(route_id, 0) + 1
                    self._last_generations[route_id] = generation
                    key = (route_id, generation)
                    self._runtime[key] = RouteRuntime()
                    self._generation_routes[key] = route
                active_generations[route_id] = generation
                self._generation_routes[(route_id, generation)] = route
            self._routes = route_map
            self._by_provider = by_provider
            self._active_generations = active_generations
            self._fingerprints = fingerprints
            self._prune_retired_locked()

    @property
    def providers(self) -> frozenset[str]:
        with self._lock:
            return frozenset(
                provider
                for provider, route_ids in self._by_provider.items()
                if any(self._routes[route_id].enabled for route_id in route_ids)
            )

    def routes(self) -> list[ProviderRoute]:
        with self._lock:
            return list(self._routes.values())

    def route_count(self, provider: str, model_id: str = "") -> int:
        provider = canonical_provider(provider)
        with self._lock:
            return sum(
                1
                for route_id in self._by_provider.get(provider, [])
                if self._routes[route_id].enabled
                and self._routes[route_id].supports_model(model_id)
            )

    def acquire(self, provider: str, model_id: str) -> RouteLease:
        """Reserve an eligible route and increment its in-flight count."""
        provider = canonical_provider(provider)
        now = self._clock()
        with self._lock:
            configured = [
                self._routes[route_id]
                for route_id in self._by_provider.get(provider, [])
                if self._routes[route_id].enabled
                and self._routes[route_id].supports_model(model_id)
            ]
            eligible = [
                route for route in configured if self._is_eligible(route, now)
            ]
            if not eligible:
                raise NoAvailableRouteError(
                    provider,
                    temporarily_unavailable=bool(configured),
                )

            priority = min(route.priority for route in eligible)
            candidates = [
                route for route in eligible if route.priority == priority
            ]
            for route in candidates:
                state = self._active_state(route.route_id)
                if state.cooldown_until and now >= state.cooldown_until:
                    state.status = "recovering"
                    state.cooldown_until = 0.0
                    state.recovery_successes = 0
            weights = self._adaptive_weights(candidates)
            route = self._rng.choices(candidates, weights=weights, k=1)[0]
            generation = self._active_generations[route.route_id]
            state = self._runtime[(route.route_id, generation)]
            state.inflight += 1
            state.selected += 1
            state.last_selected_at = now
            return RouteLease(
                route=route,
                model_id=model_id,
                started_at=now,
                generation=generation,
            )

    def peek(self, provider: str, model_id: str = "") -> ProviderRoute | None:
        """Return an eligible route without reserving capacity."""
        provider = canonical_provider(provider)
        now = self._clock()
        with self._lock:
            candidates = [
                self._routes[route_id]
                for route_id in self._by_provider.get(provider, [])
                if (route := self._routes[route_id]).enabled
                and route.supports_model(model_id)
                and self._is_eligible(route, now)
            ]
            if not candidates:
                return None
            priority = min(route.priority for route in candidates)
            return next(
                route for route in candidates if route.priority == priority
            )

    def release(self, lease: RouteLease) -> None:
        with self._lock:
            key = self._lease_key(lease)
            state = self._runtime.get(key)
            if state is not None:
                state.inflight = max(0, state.inflight - 1)
                self._prune_runtime_key_locked(key)

    def record_success(
        self,
        lease: RouteLease,
        *,
        latency_ms: float,
        output_tokens: int = 0,
    ) -> None:
        with self._lock:
            key = self._lease_key(lease)
            state = self._runtime.get(key)
            if state is None:
                return
            state.inflight = max(0, state.inflight - 1)
            state.successes += 1
            state.consecutive_failures = 0
            state.error_ewma = self._ewma(state.error_ewma, 0.0)
            state.latency_ewma_ms = self._ewma_optional(
                state.latency_ewma_ms, max(0.0, latency_ms)
            )
            if output_tokens > 0:
                state.latency_per_token_ewma_ms = self._ewma_optional(
                    state.latency_per_token_ewma_ms,
                    max(0.0, latency_ms) / output_tokens,
                )
            if state.status in {"failed", "degraded", "recovering"}:
                state.recovery_successes += 1
                state.status = (
                    "healthy" if state.recovery_successes >= 2 else "recovering"
                )
            else:
                state.status = "healthy"
            state.cooldown_until = 0.0
            state.last_status_code = None
            self._prune_runtime_key_locked(key)

    def record_failure(self, lease: RouteLease, status_code: int) -> None:
        """Release a lease and update only route-affecting failures."""
        now = self._clock()
        with self._lock:
            key = self._lease_key(lease)
            state = self._runtime.get(key)
            if state is None:
                return
            state.inflight = max(0, state.inflight - 1)
            state.failures += 1
            state.last_status_code = status_code

            # Request/schema errors apply to every credential and endpoint. They
            # consume an attempt but must not poison this route's health.
            if status_code in {400, 405, 409, 422}:
                self._prune_runtime_key_locked(key)
                return

            state.consecutive_failures += 1
            state.recovery_successes = 0
            state.error_ewma = self._ewma(state.error_ewma, 1.0)
            has_sibling = self._has_available_locked(
                lease.route.provider,
                lease.model_id,
                now,
                exclude_route_id=lease.route.route_id,
            )
            if status_code in {401, 402, 403}:
                state.status = "failed"
                state.cooldown_until = now + self._AUTH_COOLDOWN_SECONDS
            elif status_code == 404:
                state.status = "degraded"
                state.cooldown_until = (
                    now + self._FAILURE_COOLDOWN_SECONDS
                    if has_sibling
                    else 0.0
                )
            elif status_code == 429:
                state.status = "degraded"
                state.cooldown_until = (
                    now + self._THROTTLE_COOLDOWN_SECONDS
                    if has_sibling
                    else 0.0
                )
            elif status_code == 0 or status_code >= 500:
                should_cooldown = has_sibling and state.consecutive_failures >= 2
                state.status = "failed" if should_cooldown else "degraded"
                state.cooldown_until = (
                    now + self._FAILURE_COOLDOWN_SECONDS
                    if should_cooldown
                    else 0.0
                )
            self._prune_runtime_key_locked(key)

    def has_available(
        self,
        provider: str,
        model_id: str,
        *,
        exclude_route_id: str | None = None,
        exclude_generation: int | None = None,
    ) -> bool:
        provider = canonical_provider(provider)
        now = self._clock()
        with self._lock:
            return self._has_available_locked(
                provider,
                model_id,
                now,
                exclude_route_id=exclude_route_id,
                exclude_generation=exclude_generation,
            )

    def snapshot(self) -> list[dict[str, Any]]:
        """Return secret-free route configuration and runtime health."""
        now = self._clock()
        with self._lock:
            adaptive_weights: dict[str, float] = {}
            for route_ids in self._by_provider.values():
                priorities = {
                    self._routes[route_id].priority
                    for route_id in route_ids
                    if self._routes[route_id].enabled
                }
                for priority in priorities:
                    peers = [
                        self._routes[route_id]
                        for route_id in route_ids
                        if self._routes[route_id].enabled
                        and self._routes[route_id].priority == priority
                    ]
                    for route, weight in zip(
                        peers,
                        self._adaptive_weights(peers),
                        strict=True,
                    ):
                        adaptive_weights[route.route_id] = (
                            weight if self._is_eligible(route, now) else 0.0
                        )
            result = []
            for route in sorted(
                self._routes.values(),
                key=lambda item: (item.provider, item.priority, item.route_id),
            ):
                state = self._active_state(route.route_id)
                result.append(
                    {
                        "route_id": route.route_id,
                        "provider": route.provider,
                        "endpoint": route.endpoint,
                        "auth_type": route.auth_type,
                        "region": route.region,
                        "allowed_models": sorted(route.allowed_models),
                        "weight": route.weight,
                        "adaptive_weight": round(
                            adaptive_weights.get(route.route_id, 0.0),
                            6,
                        ),
                        "priority": route.priority,
                        "enabled": route.enabled,
                        "max_concurrency": route.max_concurrency,
                        "capacity_group": route.capacity_group,
                        "capacity_limit": route.capacity_limit,
                        "connect_timeout": route.connect_timeout,
                        "read_timeout": route.read_timeout,
                        "max_connections": route.max_connections,
                        "max_connections_per_host": (
                            route.max_connections_per_host
                        ),
                        "keepalive_timeout": route.keepalive_timeout,
                        "has_credentials": bool(route.credentials),
                        "status": state.status,
                        "inflight": state.inflight,
                        "selected": state.selected,
                        "successes": state.successes,
                        "failures": state.failures,
                        "error_ewma": round(state.error_ewma, 6),
                        "latency_ewma_ms": state.latency_ewma_ms,
                        "latency_per_token_ewma_ms": (
                            state.latency_per_token_ewma_ms
                        ),
                        "cooldown_remaining_seconds": max(
                            0.0, state.cooldown_until - now
                        ),
                        "last_status_code": state.last_status_code,
                    }
                )
            return result

    def _is_eligible(self, route: ProviderRoute, now: float) -> bool:
        state = self._active_state(route.route_id)
        if state.cooldown_until > now:
            return False
        if state.inflight >= route.max_concurrency:
            return False
        if route.capacity_group and route.capacity_limit:
            group_inflight = sum(
                self._runtime[key].inflight
                for key, item in self._generation_routes.items()
                if item.capacity_group == route.capacity_group
            )
            if group_inflight >= route.capacity_limit:
                return False
        return True

    def _has_available_locked(
        self,
        provider: str,
        model_id: str,
        now: float,
        *,
        exclude_route_id: str | None = None,
        exclude_generation: int | None = None,
    ) -> bool:
        return any(
            not (
                route_id == exclude_route_id
                and (
                    exclude_generation is None
                    or self._active_generations[route_id]
                    == exclude_generation
                )
            )
            and (route := self._routes[route_id]).enabled
            and route.supports_model(model_id)
            and self._is_eligible(route, now)
            for route_id in self._by_provider.get(provider, [])
        )

    def _adaptive_weights(self, routes: list[ProviderRoute]) -> list[float]:
        token_latencies = [
            self._active_state(route.route_id).latency_per_token_ewma_ms
            for route in routes
            if self._active_state(
                route.route_id
            ).latency_per_token_ewma_ms is not None
        ]
        request_latencies = [
            self._active_state(route.route_id).latency_ewma_ms
            for route in routes
            if self._active_state(route.route_id).latency_ewma_ms is not None
        ]
        baseline = (
            min(token_latencies)
            if token_latencies
            else (min(request_latencies) if request_latencies else None)
        )

        weights: list[float] = []
        for route in routes:
            state = self._active_state(route.route_id)
            reliability = max(0.05, 1.0 - state.error_ewma)
            observed = (
                state.latency_per_token_ewma_ms
                if token_latencies
                else state.latency_ewma_ms
            )
            latency_factor = (
                1.0
                if baseline is None or observed is None or observed <= 0
                else max(0.1, baseline / observed)
            )
            utilization = state.inflight / route.max_concurrency
            capacity_factor = max(0.1, 1.0 - utilization)
            recovery_factor = (
                self._RECOVERY_FACTOR
                if state.status == "recovering"
                else 1.0
            )
            weights.append(
                max(
                    0.0001,
                    route.weight
                    * reliability
                    * latency_factor
                    * capacity_factor
                    * recovery_factor,
                )
            )
        return weights

    def _active_state(self, route_id: str) -> RouteRuntime:
        return self._runtime[
            (route_id, self._active_generations[route_id])
        ]

    @staticmethod
    def _lease_key(lease: RouteLease) -> tuple[str, int]:
        return (lease.route.route_id, lease.generation)

    def _prune_runtime_key_locked(self, key: tuple[str, int]) -> None:
        if self._active_generations.get(key[0]) == key[1]:
            return
        state = self._runtime.get(key)
        if state is not None and state.inflight == 0:
            self._runtime.pop(key, None)
            self._generation_routes.pop(key, None)

    def _prune_retired_locked(self) -> None:
        for key, state in list(self._runtime.items()):
            if (
                self._active_generations.get(key[0]) != key[1]
                and state.inflight == 0
            ):
                self._runtime.pop(key, None)
                self._generation_routes.pop(key, None)

    def _ewma(self, previous: float, observation: float) -> float:
        return (
            self._EWMA_ALPHA * observation
            + (1.0 - self._EWMA_ALPHA) * previous
        )

    def _ewma_optional(
        self, previous: float | None, observation: float
    ) -> float:
        if previous is None:
            return observation
        return self._ewma(previous, observation)
