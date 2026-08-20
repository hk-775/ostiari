"""Quota enforcer — bridges policy hierarchy limits into the request pipeline.

Takes a ResolvedPolicy (from the hierarchy walk) and enforces:
- rate_limit_rpm: dynamic per-project RPM from org/BU/project/env chain
- budget_limit: blocks requests when projected spend exceeds hierarchy budget
- max_tokens_per_request: caps the max_tokens parameter
- allowed_models: rejects models not in the intersection
- allowed_providers: rejects providers not in the intersection
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import TYPE_CHECKING

from src.gateway.config import DEFAULT_CONFIG
from src.gateway.rate_limiter import consume_shared_rate_limit
from src.gateway.striped_lock import StripedLock

if TYPE_CHECKING:
    from src.gateway.models import Project, ResolvedPolicy
    from src.gateway.persistence import DynamoPersistence


@dataclass
class QuotaDecision:
    """Result of a quota enforcement check."""

    allowed: bool = True
    reason: str = ""
    limit_type: str = ""
    limit_value: float | int | None = None
    current_value: float | int | None = None
    reservation: BudgetReservation | None = None
    status_code: int = 429
    error_code: str | None = None


@dataclass(frozen=True)
class BudgetReservation:
    """Durable authorization to spend an estimated amount for one request."""

    request_id: str
    counters: tuple[tuple[str, str, float], ...]
    amount: float
    epochs: tuple[tuple[str, int], ...] = ()


BUDGET_ALERT_THRESHOLDS = [0.8, 0.9, 1.0]


# Scope name for this enforcer's shared spend counters. Deliberately distinct
# from CostTracker's "project" scope: both record the same cost on the same
# request, so pointing them at one key would double every charge. They stay two
# counters that happen to agree, which is what they already were per-process.
SPEND_SCOPE = "quota"
POLICY_RATE_LIMIT_NAMESPACE = "policy"

# How stale the spend figure `check_budget` reads may be. `record_spend` already
# adopts the fleet total for free from its own write, so this only matters for
# spend an instance did not serve itself: without a refresh, an instance that has
# not billed a project since starting reads its own $0 and admits a request
# against an exhausted budget — once per instance, and again after every deploy.
#
# The window bounds the overshoot instead of eliminating it. At worst a project
# overspends by whatever the fleet can bill in two seconds, rather than by a
# whole budget per instance. Eliminating it entirely would mean a consistent read
# on every request, which puts DynamoDB on the hot path of every call the gateway
# proxies — the cost the cache elsewhere in this codebase exists to avoid.
SPEND_REFRESH_SECONDS = 2.0
RESERVATION_CLEANUP_SECONDS = 60.0


def _normalize_tenant_id(tenant_id: str | None) -> str | None:
    if tenant_id is None:
        return None
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be None or a non-empty string")
    return tenant_id


def _tenant_scoped_project_id(
    tenant_id: str | None,
    project_id: str,
) -> str:
    """Return an opaque counter key, retaining raw IDs in legacy mode."""
    if tenant_id is None:
        return project_id
    return (
        f"tenant:{len(tenant_id)}:{tenant_id}:"
        f"project:{len(project_id)}:{project_id}"
    )


def _tenant_scoped_user_id(
    tenant_id: str | None,
    user_id: str,
) -> str:
    """Match CostTracker's tenant-qualified user counter identity."""
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id must be a non-empty string")
    if tenant_id is None:
        return user_id
    return (
        f"tenant:{len(tenant_id)}:{tenant_id}:"
        f"user:{len(user_id)}:{user_id}"
    )


def _project_scope(
    tenant_id: str | None,
    project_id: str,
    project: Project | None,
) -> tuple[str | None, int | None]:
    tenant_id = _normalize_tenant_id(tenant_id)
    if project is None:
        return tenant_id, None
    if project.project_id != project_id:
        raise ValueError("project does not match project_id")

    project_tenant_id = _normalize_tenant_id(project.tenant_id)
    if tenant_id is None:
        tenant_id = project_tenant_id
    elif project_tenant_id != tenant_id:
        raise ValueError("project tenant_id does not match tenant_id")
    return tenant_id, project.rate_limit_rpm


def _effective_rate_limit(
    policy: ResolvedPolicy,
    project_rate_limit: int | None,
) -> int | None:
    limits = [
        limit
        for limit in (policy.rate_limit_rpm, project_rate_limit)
        if limit is not None
    ]
    return min(limits) if limits else None


class QuotaEnforcer:
    """Enforces resolved policy limits on incoming requests.

    State is keyed by tenant and project. ``tenant_id=None`` is the explicit
    legacy namespace, preserving existing single-tenant callers.

    Spend is tracked in DynamoDB when persistence is supplied, because this is
    the class that actually blocks requests: ``check_budget`` is what returns
    ``allowed=False``, and reading a per-process counter there turned a $100
    budget into $100 *per instance* — roughly $200 at the shipped
    ``desired_count=2`` and up to $1000 fully scaled out. Hierarchy-derived RPM
    uses the shared fixed-window contract whenever persistence is enabled; an
    unavailable shared limiter denies instead of reverting to local capacity.
    """

    def __init__(self, persistence: DynamoPersistence | None = None) -> None:
        self._request_windows: dict[str, list[datetime]] = {}
        self._spend_tracker: dict[str, float] = {}
        self._spend_epochs: dict[str, int] = {}
        self._alerted_thresholds: dict[str, set[float]] = {}
        self._alert_callbacks: list = []
        # Per-scope locks: tenant-qualified projects proceed independently.
        self._locks = StripedLock()
        self._persistence = persistence
        # When each project's shared counter was last read. Only consulted for
        # projects this instance has not billed recently; a write refreshes the
        # figure at no cost, so it stamps this too.
        self._spend_read_at: dict[str, float] = {}
        # Single-process fallback for deployments without DynamoDB. The durable
        # path uses DynamoDB transactions; this lock closes the equivalent race
        # between concurrent coroutines on one gateway.
        self._local_budget_lock = asyncio.Lock()
        self._local_user_spend: dict[str, float] = {}
        self._local_reservations: dict[str, tuple[BudgetReservation, bool]] = {}
        self._reservation_cleanup_at: dict[str, float] = {}

    @property
    def _shares_spend(self) -> bool:
        return self._persistence is not None and self._persistence.enabled

    async def _read_shared_spend(
        self,
        scope: str,
        ident: str,
    ) -> tuple[float, int | None] | None:
        getter = getattr(self._persistence, "get_spend_state", None)
        if callable(getter):
            state = await getter(scope, ident)
            if state is None:
                return None
            return float(state.total), int(state.epoch)
        total = await self._persistence.get_spend(scope, ident)
        return (float(total), None) if total is not None else None

    def _adopt_shared_spend(
        self,
        scope_id: str,
        total: float,
        epoch: int,
    ) -> None:
        current_epoch = self._spend_epochs.get(scope_id, 0)
        if epoch < current_epoch:
            return
        if epoch > current_epoch:
            self._spend_epochs[scope_id] = epoch
            self._spend_tracker[scope_id] = total
            self._alerted_thresholds.pop(scope_id, None)
            return
        self._spend_tracker[scope_id] = max(
            total,
            self._spend_tracker.get(scope_id, 0.0),
        )

    def on_budget_alert(self, callback) -> None:
        """Register a callback for budget threshold alerts.

        Callback signature: callback(
            project_id,
            threshold_pct,
            current_spend,
            budget_limit,
            tenant_id,
            billing_epoch,
        )
        """
        self._alert_callbacks.append(callback)

    async def _fire_budget_alerts(
        self,
        *,
        scope_id: str,
        project_id: str,
        tenant_id: str | None,
        thresholds,
        current_spend: float,
        budget_limit: float,
        billing_epoch: int,
    ) -> None:
        alerted = self._alerted_thresholds.get(scope_id, set())
        for threshold in thresholds:
            if threshold in alerted:
                continue
            alerted.add(threshold)
            for callback in self._alert_callbacks:
                result = callback(
                    project_id,
                    threshold,
                    current_spend,
                    budget_limit,
                    tenant_id,
                    billing_epoch,
                )
                if inspect.isawaitable(result):
                    await result
        self._alerted_thresholds[scope_id] = alerted

    async def check_rate_limit(
        self,
        project_id: str,
        policy: ResolvedPolicy,
        *,
        tenant_id: str | None = None,
        project: Project | None = None,
    ) -> QuotaDecision:
        """Enforce the strictest hierarchy and canonical project RPM."""
        tenant_id, project_rate_limit = _project_scope(
            tenant_id,
            project_id,
            project,
        )
        limit = _effective_rate_limit(policy, project_rate_limit)
        if limit is None:
            return QuotaDecision(allowed=True)

        now = datetime.now(timezone.utc)
        if self._persistence is not None and self._persistence.enabled:
            result = await consume_shared_rate_limit(
                self._persistence,
                namespace=POLICY_RATE_LIMIT_NAMESPACE,
                tenant_id=tenant_id,
                user_id=None,
                project_id=project_id,
                user_limit=None,
                project_limit=limit,
                window_seconds=60,
                now=now,
            )
            if result.allowed:
                return QuotaDecision(allowed=True)
            return QuotaDecision(
                allowed=False,
                reason=f"Policy rate limit exceeded or unavailable: {limit} RPM",
                limit_type="rate_limit_rpm",
                limit_value=limit,
                current_value=limit,
            )

        scope_id = _tenant_scoped_project_id(tenant_id, project_id)
        async with self._locks.acquire(scope_id):
            window = timedelta(seconds=60)
            cutoff = now - window

            timestamps = self._request_windows.get(scope_id, [])
            timestamps = [ts for ts in timestamps if ts > cutoff]

            if len(timestamps) >= limit:
                return QuotaDecision(
                    allowed=False,
                    reason=f"Policy rate limit exceeded: {limit} RPM",
                    limit_type="rate_limit_rpm",
                    limit_value=limit,
                    current_value=len(timestamps),
                )

            timestamps.append(now)
            self._request_windows[scope_id] = timestamps
            return QuotaDecision(allowed=True)

    async def refresh_spend(
        self,
        project_id: str,
        *,
        tenant_id: str | None = None,
    ) -> None:
        """Pull the shared counter if this instance's figure may be stale.

        Cheap and idempotent: a no-op without persistence, within
        ``SPEND_REFRESH_SECONDS`` of the last read, or if the read fails. Called
        from ``enforce_all`` before ``check_budget`` so the gate does not decide
        against a number that predates other instances' spending.

        The counter is only ever moved forward. A read that returns less than the
        local figure is stale — this instance's own last write is newer than what
        the read observed — and adopting it would hand back budget already spent.
        """
        tenant_id = _normalize_tenant_id(tenant_id)
        if not self._shares_spend:
            return
        scope_id = _tenant_scoped_project_id(tenant_id, project_id)
        now = monotonic()
        if now - self._spend_read_at.get(scope_id, 0.0) < SPEND_REFRESH_SECONDS:
            return
        self._spend_read_at[scope_id] = now
        shared = await self._read_shared_spend(SPEND_SCOPE, scope_id)
        if shared is None:
            return
        total, epoch = shared
        if epoch is None:
            if total > self._spend_tracker.get(scope_id, 0.0):
                self._spend_tracker[scope_id] = total
            return
        self._adopt_shared_spend(scope_id, total, epoch)

    def check_budget(
        self,
        project_id: str,
        estimated_cost: float,
        policy: ResolvedPolicy,
        *,
        tenant_id: str | None = None,
    ) -> QuotaDecision:
        """Check if request would exceed the policy's budget_limit.

        Reads the local figure, which ``record_spend`` and ``refresh_spend`` keep
        aligned with the fleet-wide counter. Stays synchronous because it is also
        called directly (``POST /admin/quotas/simulate``); callers on the request
        path go through ``enforce_all``, which refreshes first.
        """
        if policy.budget_limit is None:
            return QuotaDecision(allowed=True)

        tenant_id = _normalize_tenant_id(tenant_id)
        scope_id = _tenant_scoped_project_id(tenant_id, project_id)
        current_spend = self._spend_tracker.get(scope_id, 0.0)
        projected = current_spend + estimated_cost

        if projected > policy.budget_limit:
            return QuotaDecision(
                allowed=False,
                reason=f"Budget limit exceeded: ${projected:.4f} > ${policy.budget_limit:.2f}",
                limit_type="budget_limit",
                limit_value=policy.budget_limit,
                current_value=current_spend,
            )

        return QuotaDecision(allowed=True)

    async def reserve_budget(
        self,
        *,
        request_id: str,
        project_id: str,
        user_id: str,
        estimated_cost: float,
        project_budget_limit: float | None,
        user_budget_limit: float | None,
        tenant_id: str | None = None,
        project_current_spend: float = 0.0,
        user_current_spend: float = 0.0,
    ) -> QuotaDecision:
        """Atomically authorize estimated spend across project and user limits."""
        tenant_id = _normalize_tenant_id(tenant_id)
        limits = [
            limit
            for limit in (project_budget_limit, user_budget_limit)
            if limit is not None
        ]
        if not limits:
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

        project_scope = _tenant_scoped_project_id(
            tenant_id,
            project_id,
        )
        user_scope = _tenant_scoped_user_id(tenant_id, user_id)
        counters: list[tuple[str, str, float]] = []
        if project_budget_limit is not None:
            counters.append(
                (SPEND_SCOPE, project_scope, project_budget_limit)
            )
            # CostTracker's project total reports the same spend. Reserving and
            # finalizing it in this transaction keeps both counters on the same
            # billing epoch and lets usage recording skip a second ADD.
            if self._shares_spend:
                counters.append(
                    ("project", project_scope, project_budget_limit)
                )
        if user_budget_limit is not None:
            counters.append(("user", user_scope, user_budget_limit))
        reservation = BudgetReservation(
            request_id=request_id,
            counters=tuple(counters),
            amount=estimated_cost,
        )

        if self._shares_spend:
            primary_scope, primary_ident, _limit = min(
                counters,
                key=lambda counter: (counter[0], counter[1]),
            )
            cleanup_key = f"{primary_scope}:{primary_ident}"
            cleanup_now = monotonic()
            if (
                cleanup_now
                - self._reservation_cleanup_at.get(cleanup_key, 0.0)
                >= RESERVATION_CLEANUP_SECONDS
            ):
                self._reservation_cleanup_at[cleanup_key] = cleanup_now
                cleanup = getattr(
                    self._persistence,
                    "release_expired_budget_reservations",
                    None,
                )
                if callable(cleanup):
                    await cleanup(
                        primary_scope=primary_scope,
                        primary_ident=primary_ident,
                    )
            reserve = getattr(self._persistence, "reserve_budget", None)
            if not callable(reserve):
                return QuotaDecision(
                    allowed=False,
                    reason="Atomic budget enforcement is unavailable.",
                    limit_type="budget_backend_unavailable",
                    status_code=503,
                )
            result = await reserve(
                request_id=request_id,
                reservations=counters,
                amount=estimated_cost,
            )
            if result is None:
                return QuotaDecision(
                    allowed=False,
                    reason="Atomic budget enforcement is unavailable.",
                    limit_type="budget_backend_unavailable",
                    status_code=503,
                )
            if not result.allowed:
                denied_scope = result.denied_scope or SPEND_SCOPE
                denied_limit = next(
                    (
                        limit
                        for scope, _ident, limit in counters
                        if scope == denied_scope
                    ),
                    None,
                )
                current = result.totals.get(denied_scope, 0.0)
                label = (
                    "User budget limit"
                    if denied_scope == "user"
                    else "Project budget limit"
                )
                return QuotaDecision(
                    allowed=False,
                    reason=(
                        f"{label} exceeded: "
                        f"${current + estimated_cost:.4f} > "
                        f"${float(denied_limit or 0):.2f}"
                    ),
                    limit_type=(
                        "user_budget_limit"
                        if denied_scope == "user"
                        else "budget_limit"
                    ),
                    limit_value=denied_limit,
                    current_value=current,
                    error_code="budget_exceeded",
                )
            if result.state == "finalized":
                return QuotaDecision(
                    allowed=False,
                    reason="The budget reservation was already finalized.",
                    limit_type="budget_request_replayed",
                )
            reservation = BudgetReservation(
                request_id=request_id,
                counters=tuple(counters),
                amount=estimated_cost,
                epochs=tuple(sorted(result.epochs.items())),
            )
            if SPEND_SCOPE in result.totals:
                quota_epoch = result.epochs.get(SPEND_SCOPE)
                if quota_epoch is None:
                    self._spend_tracker[project_scope] = max(
                        result.totals[SPEND_SCOPE],
                        self._spend_tracker.get(project_scope, 0.0),
                    )
                else:
                    self._adopt_shared_spend(
                        project_scope,
                        result.totals[SPEND_SCOPE],
                        quota_epoch,
                    )
            return QuotaDecision(
                allowed=True,
                reservation=reservation,
            )

        # Supported single-node mode: make the check-and-increment indivisible
        # across coroutines, then reconcile the same reservation on completion.
        async with self._local_budget_lock:
            previous = self._local_reservations.get(request_id)
            if previous is not None:
                prior, finalized = previous
                if prior != reservation or finalized:
                    return QuotaDecision(
                        allowed=False,
                        reason="The budget reservation conflicts or was replayed.",
                        limit_type="budget_request_replayed",
                    )
                return QuotaDecision(
                    allowed=True,
                    reservation=reservation,
                )

            self._spend_tracker[project_scope] = max(
                self._spend_tracker.get(project_scope, 0.0),
                project_current_spend,
            )
            self._local_user_spend[user_scope] = max(
                self._local_user_spend.get(user_scope, 0.0),
                user_current_spend,
            )
            for scope, _ident, limit in counters:
                current = (
                    self._local_user_spend.get(user_scope, 0.0)
                    if scope == "user"
                    else self._spend_tracker.get(project_scope, 0.0)
                )
                if current + estimated_cost > limit:
                    return QuotaDecision(
                        allowed=False,
                        reason=(
                            f"{'User' if scope == 'user' else 'Project'} "
                            "budget limit exceeded: "
                            f"${current + estimated_cost:.4f} > ${limit:.2f}"
                        ),
                        limit_type=(
                            "user_budget_limit"
                            if scope == "user"
                            else "budget_limit"
                        ),
                        limit_value=limit,
                        current_value=current,
                    )
            if project_budget_limit is not None:
                self._spend_tracker[project_scope] += estimated_cost
            if user_budget_limit is not None:
                self._local_user_spend[user_scope] += estimated_cost
            self._local_reservations[request_id] = (reservation, False)
        return QuotaDecision(allowed=True, reservation=reservation)

    async def finalize_budget(
        self,
        reservation: BudgetReservation,
        actual_cost: float,
        *,
        tenant_id: str | None = None,
        project_id: str,
    ) -> dict[str, float] | None:
        """Reconcile a reservation to actual spend exactly once."""
        tenant_id = _normalize_tenant_id(tenant_id)
        project_scope = _tenant_scoped_project_id(
            tenant_id,
            project_id,
        )
        if self._shares_spend:
            finalize = getattr(
                self._persistence,
                "finalize_budget_reservation",
                None,
            )
            if not callable(finalize):
                return None
            result = await finalize(
                request_id=reservation.request_id,
                reservations=list(reservation.counters),
                reserved_amount=reservation.amount,
                actual_cost=actual_cost,
            )
            if result is None:
                return None
            if SPEND_SCOPE in result.totals:
                quota_epoch = result.epochs.get(SPEND_SCOPE)
                if quota_epoch is None:
                    self._spend_tracker[project_scope] = max(
                        result.totals[SPEND_SCOPE],
                        self._spend_tracker.get(project_scope, 0.0),
                    )
                else:
                    self._adopt_shared_spend(
                        project_scope,
                        result.totals[SPEND_SCOPE],
                        quota_epoch,
                    )
            quota_limit = next(
                (
                    limit
                    for scope, _ident, limit in reservation.counters
                    if scope == SPEND_SCOPE
                ),
                None,
            )
            if (
                quota_limit is not None
                and result.crossed_thresholds
                and self._alert_callbacks
            ):
                await self._fire_budget_alerts(
                    scope_id=project_scope,
                    project_id=project_id,
                    tenant_id=tenant_id,
                    thresholds=result.crossed_thresholds,
                    current_spend=result.totals.get(
                        SPEND_SCOPE,
                        0.0,
                    ),
                    budget_limit=quota_limit,
                    billing_epoch=result.epochs.get(
                        SPEND_SCOPE,
                        self._spend_epochs.get(project_scope, 0),
                    ),
                )
            return result.totals

        async with self._local_budget_lock:
            existing = self._local_reservations.get(
                reservation.request_id
            )
            if existing is None or existing[0] != reservation:
                return None
            if existing[1]:
                return {
                    SPEND_SCOPE: self._spend_tracker.get(
                        project_scope,
                        0.0,
                    )
                }
            delta = actual_cost - reservation.amount
            for scope, ident, _limit in reservation.counters:
                if scope == "user":
                    self._local_user_spend[ident] = max(
                        0.0,
                        self._local_user_spend.get(ident, 0.0) + delta,
                    )
                else:
                    self._spend_tracker[ident] = max(
                        0.0,
                        self._spend_tracker.get(ident, 0.0) + delta,
                    )
            self._local_reservations[reservation.request_id] = (
                reservation,
                True,
            )
            totals = {
                scope: (
                    self._local_user_spend.get(ident, 0.0)
                    if scope == "user"
                    else self._spend_tracker.get(ident, 0.0)
                )
                for scope, ident, _limit in reservation.counters
            }
            quota_limit = next(
                (
                    limit
                    for scope, _ident, limit in reservation.counters
                    if scope == SPEND_SCOPE
                ),
                None,
            )
            if quota_limit is not None and self._alert_callbacks:
                current = totals.get(SPEND_SCOPE, 0.0)
                await self._fire_budget_alerts(
                    scope_id=project_scope,
                    project_id=project_id,
                    tenant_id=tenant_id,
                    thresholds=(
                        threshold
                        for threshold in BUDGET_ALERT_THRESHOLDS
                        if current >= quota_limit * threshold
                    ),
                    current_spend=current,
                    budget_limit=quota_limit,
                    billing_epoch=self._spend_epochs.get(
                        project_scope,
                        0,
                    ),
                )
            return totals

    async def release_budget(
        self,
        reservation: BudgetReservation,
        *,
        tenant_id: str | None = None,
        project_id: str,
    ) -> bool:
        """Return reserved capacity when no billable provider call occurred."""
        totals = await self.finalize_budget(
            reservation,
            0.0,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        return totals is not None

    def check_max_tokens(
        self, requested_max_tokens: int | None, policy: ResolvedPolicy
    ) -> QuotaDecision:
        """Check if requested max_tokens exceeds policy limit."""
        if policy.max_tokens_per_request is None:
            return QuotaDecision(allowed=True)

        if requested_max_tokens is None:
            return QuotaDecision(allowed=True)

        if requested_max_tokens > policy.max_tokens_per_request:
            return QuotaDecision(
                allowed=False,
                reason=f"max_tokens {requested_max_tokens} exceeds policy limit {policy.max_tokens_per_request}",
                limit_type="max_tokens_per_request",
                limit_value=policy.max_tokens_per_request,
                current_value=requested_max_tokens,
            )

        return QuotaDecision(allowed=True)

    def check_model_allowed(
        self, model: str, policy: ResolvedPolicy
    ) -> QuotaDecision:
        """Check if the requested model is allowed by the policy hierarchy."""
        if policy.allowed_models is None:
            return QuotaDecision(allowed=True)

        if model not in policy.allowed_models:
            return QuotaDecision(
                allowed=False,
                reason=f"Model '{model}' not in allowed models: {policy.allowed_models}",
                limit_type="allowed_models",
            )

        return QuotaDecision(allowed=True)

    def check_provider_allowed(
        self, provider: str, policy: ResolvedPolicy
    ) -> QuotaDecision:
        """Check if the requested provider is allowed by the policy hierarchy."""
        if policy.allowed_providers is None:
            return QuotaDecision(allowed=True)

        if provider not in policy.allowed_providers:
            return QuotaDecision(
                allowed=False,
                reason=f"Provider '{provider}' not in allowed providers: {policy.allowed_providers}",
                limit_type="allowed_providers",
            )

        return QuotaDecision(allowed=True)

    async def enforce_all(
        self,
        project_id: str,
        model: str,
        provider: str | None,
        max_tokens: int | None,
        estimated_cost: float,
        policy: ResolvedPolicy,
        *,
        tenant_id: str | None = None,
        project: Project | None = None,
    ) -> QuotaDecision:
        """Run all quota checks. Returns first failure or allowed."""
        tenant_id, _ = _project_scope(tenant_id, project_id, project)
        rate_decision = await self.check_rate_limit(
            project_id,
            policy,
            tenant_id=tenant_id,
            project=project,
        )
        if not rate_decision.allowed:
            return rate_decision

        # Align with the fleet before deciding. Skipped entirely when the policy
        # sets no budget, so gateways that do not use hierarchy budgets never pay
        # for it.
        if policy.budget_limit is not None:
            await self.refresh_spend(project_id, tenant_id=tenant_id)

        checks = [
            self.check_budget(
                project_id,
                estimated_cost,
                policy,
                tenant_id=tenant_id,
            ),
            self.check_max_tokens(max_tokens, policy),
            self.check_model_allowed(model, policy),
        ]
        if provider:
            checks.append(self.check_provider_allowed(provider, policy))

        for decision in checks:
            if not decision.allowed:
                return decision

        return QuotaDecision(allowed=True)

    async def record_spend(
        self,
        project_id: str,
        cost: float,
        budget_limit: float | None = None,
        *,
        share: bool = True,
        tenant_id: str | None = None,
    ) -> None:
        """Record spend for budget tracking. Fires alerts at threshold crossings.

        With persistence configured the cost goes into a shared counter whose
        atomic ``ADD`` returns the fleet-wide total, so the number
        ``check_budget`` reads next includes every other instance's spend. One
        write and no extra read on the request path.

        The shared write happens *outside* the per-project lock. Holding a lock
        across a network round trip would cap a single project at one request per
        round trip — around 100/s — for no benefit: ``ADD`` is already atomic, and
        because it returns a value that includes exactly this caller's cost, every
        concurrent caller gets a distinct ``(total - cost, total]`` interval. Two
        requests billing $2 and $3 against $8 see [8, 10] and [10, 13] in some
        order: no gap and no overlap, so a threshold still falls inside exactly
        one of them and fires exactly once. The lock is only taken for the local
        dict and the alerted-threshold set, which are read-modify-write.

        ``share=False`` skips the shared counter for spend that every instance
        fabricates identically at startup (the demo seed); see
        ``CostTracker.record_usage``.
        """
        tenant_id = _normalize_tenant_id(tenant_id)
        scope_id = _tenant_scoped_project_id(tenant_id, project_id)
        total: float | None = None
        shared_epoch: int | None = None
        if share and self._shares_spend:
            add_state = getattr(self._persistence, "add_spend_state", None)
            if callable(add_state):
                state = await add_state(SPEND_SCOPE, scope_id, cost)
                if state is not None:
                    total = float(state.total)
                    shared_epoch = int(state.epoch)
            else:
                total = await self._persistence.add_spend(
                    SPEND_SCOPE,
                    scope_id,
                    cost,
                )

        async with self._locks.acquire(scope_id):
            if total is not None:
                # Both figures move to the fleet view together. Leaving `prev`
                # local while `new_spend` jumped to the fleet total would make the
                # threshold comparison below span an interval this instance never
                # actually crossed, firing every alert at once on its first
                # request.
                prev = total - cost
                new_spend = total
                if shared_epoch is None:
                    # Never let the local counter go backwards: responses can
                    # arrive out of order.
                    self._spend_tracker[scope_id] = max(
                        new_spend,
                        self._spend_tracker.get(scope_id, 0.0),
                    )
                else:
                    current_epoch = self._spend_epochs.get(scope_id, 0)
                    if shared_epoch < current_epoch:
                        # This ADD completed in the billing cycle that a
                        # concurrent reset just closed.
                        prev = self._spend_tracker.get(scope_id, 0.0)
                        new_spend = prev
                    elif shared_epoch > current_epoch:
                        self._spend_epochs[scope_id] = shared_epoch
                        self._spend_tracker[scope_id] = new_spend
                        self._alerted_thresholds.pop(scope_id, None)
                    else:
                        self._spend_tracker[scope_id] = max(
                            new_spend,
                            self._spend_tracker.get(scope_id, 0.0),
                        )
                # Deliberately does NOT stamp `_spend_read_at`. Treating a write
                # as a read looks like a free optimization — the ADD just returned
                # the total — but it makes the refresh interval unreachable for
                # any project under continuous traffic: every request would push
                # the stamp forward, so `refresh_spend` would only ever fire for
                # idle projects, which is exactly backwards. A busy project is the
                # one whose fleet total moves between requests. The saving was at
                # most one read per project per interval; the cost was the fix not
                # working where it matters.
            else:
                # No shared counter, or the write failed — fall back to
                # accumulating locally, i.e. the old per-instance behaviour.
                prev = self._spend_tracker.get(scope_id, 0.0)
                new_spend = prev + cost
                self._spend_tracker[scope_id] = new_spend

            if budget_limit and budget_limit > 0 and self._alert_callbacks:
                alerted = self._alerted_thresholds.get(scope_id, set())
                crossed = []
                for threshold in BUDGET_ALERT_THRESHOLDS:
                    if threshold in alerted:
                        continue
                    trigger_at = budget_limit * threshold
                    if prev < trigger_at <= new_spend:
                        crossed.append(threshold)
                await self._fire_budget_alerts(
                    scope_id=scope_id,
                    project_id=project_id,
                    tenant_id=tenant_id,
                    thresholds=crossed,
                    current_spend=new_spend,
                    budget_limit=budget_limit,
                    billing_epoch=self._spend_epochs.get(scope_id, 0),
                )

    def get_spend(
        self,
        project_id: str,
        *,
        tenant_id: str | None = None,
    ) -> float:
        """Get this instance's tracked spend for a project.

        Fleet-accurate once the instance has served a request for the project
        since starting, because ``record_spend`` adopts the shared total. Use
        ``current_spend`` where the answer must be right regardless.
        """
        tenant_id = _normalize_tenant_id(tenant_id)
        scope_id = _tenant_scoped_project_id(tenant_id, project_id)
        return self._spend_tracker.get(scope_id, 0.0)

    async def current_spend(
        self,
        project_id: str,
        *,
        tenant_id: str | None = None,
    ) -> float:
        """Fleet-wide spend for a project, read through to the shared counter.

        For admin reads, which happen once per operator request rather than per
        API call, so the extra read is affordable — and where a stale figure is
        the whole problem: an instance that has not served this project since
        starting reports 0 from ``get_spend`` while another is already blocking
        requests against it.

        Falls back to the local figure if there is no shared counter or it cannot
        be read.
        """
        tenant_id = _normalize_tenant_id(tenant_id)
        scope_id = _tenant_scoped_project_id(tenant_id, project_id)
        if self._shares_spend:
            shared = await self._read_shared_spend(SPEND_SCOPE, scope_id)
            if shared is not None:
                total, epoch = shared
                if epoch is None:
                    self._spend_tracker[scope_id] = total
                else:
                    self._adopt_shared_spend(scope_id, total, epoch)
                return self._spend_tracker.get(scope_id, total)
        return self.get_spend(project_id, tenant_id=tenant_id)

    async def reset_spend(
        self,
        project_id: str,
        *,
        tenant_id: str | None = None,
    ) -> bool:
        """Reset spend tracking for a project (e.g., at billing cycle reset).

        Returns whether the reset is fleet-wide. False means the shared counter
        still holds the old value and only this instance was cleared, so the
        project stays blocked on every other instance — the caller must not
        report an unqualified success.

        Local state is cleared either way: a partial reset is still better than
        none, and the shared counter is re-read on the next admin request.
        """
        tenant_id = _normalize_tenant_id(tenant_id)
        scope_id = _tenant_scoped_project_id(tenant_id, project_id)
        def _clear_local(epoch: int | None = None) -> None:
            self._spend_tracker.pop(scope_id, None)
            self._alerted_thresholds.pop(scope_id, None)
            self._spend_read_at.pop(scope_id, None)
            if epoch is not None:
                self._spend_epochs[scope_id] = epoch

        if not self._shares_spend:
            _clear_local(self._spend_epochs.get(scope_id, 0) + 1)
            return True
        reset_many = getattr(
            self._persistence,
            "reset_spend_counters",
            None,
        )
        if callable(reset_many):
            states = await reset_many(
                [
                    (SPEND_SCOPE, scope_id),
                    ("project", scope_id),
                ]
            )
            quota_state = (
                states.get(SPEND_SCOPE)
                if states is not None
                else None
            )
            _clear_local(
                int(quota_state.epoch)
                if quota_state is not None
                else None
            )
            return states is not None
        _clear_local()
        return await self._persistence.reset_spend(SPEND_SCOPE, scope_id)

    async def adopt_fleet_spend(
        self,
        project_ids,
        *,
        tenant_id: str | None = None,
    ) -> None:
        """Seed the local counters from the shared ones at startup.

        Without this, a restarted or newly scaled-out instance believes every
        project has spent nothing until it happens to serve a request for it —
        so the first request after a deploy is admitted against a budget the
        fleet had already exhausted, and ``GET /admin/quotas`` reports $0 spend
        for a project that is over its limit.
        """
        tenant_id = _normalize_tenant_id(tenant_id)
        if not self._shares_spend:
            return
        for project_id in project_ids:
            scope_id = _tenant_scoped_project_id(tenant_id, project_id)
            shared = await self._read_shared_spend(SPEND_SCOPE, scope_id)
            if shared is None:
                continue
            total, epoch = shared
            if epoch is None:
                self._spend_tracker[scope_id] = total
            else:
                self._adopt_shared_spend(scope_id, total, epoch)

    def cap_max_tokens(self, requested: int | None, policy: ResolvedPolicy) -> int | None:
        """Return a bounded output size without turning a ceiling into a default."""
        if policy.max_tokens_per_request is None:
            return requested
        if requested is None:
            return min(
                DEFAULT_CONFIG.adapter.default_max_tokens,
                policy.max_tokens_per_request,
            )
        return min(requested, policy.max_tokens_per_request)
