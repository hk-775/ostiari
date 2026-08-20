"""Cost tracking, budget management, and usage aggregation for the LLM-Router."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import tiktoken

from src.gateway.config import DEFAULT_CONFIG
from src.gateway.models import (
    BudgetStatus,
    TokenPricing,
    UsageBreakdown,
    UsageFilters,
    UsageRecord,
    UsageReport,
)

if TYPE_CHECKING:
    from src.gateway.persistence import DynamoPersistence

logger = logging.getLogger(__name__)

# Sort floor for records with no timestamp, so ordering never compares None to a
# datetime (TypeError) and an undated record is treated as oldest rather than
# newest. tz-aware to match ``CostTracker._as_aware``'s output.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _tenant_scoped_id(
    tenant_id: str | None,
    resource_type: str,
    resource_id: str,
) -> str:
    """Return a collision-safe key while retaining legacy raw identifiers."""
    if not isinstance(resource_id, str) or not resource_id.strip():
        raise ValueError(f"{resource_type}_id must be a non-empty string")
    if tenant_id is None:
        return resource_id
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be None or a non-empty string")
    return (
        f"tenant:{len(tenant_id)}:{tenant_id}:"
        f"{resource_type}:{len(resource_id)}:{resource_id}"
    )


class CostTracker:
    """Records usage, calculates costs, checks budgets, and aggregates usage data.

    Args:
        pricing_config: Nested dict mapping provider -> model -> TokenPricing.
        budgets: Dict mapping project_id -> {"budget_limit": float, "alert_threshold": float}.
    """

    MAX_RECORDS = 100_000

    # How long a fleet-wide usage refresh is reused before the next reader pays
    # for another one.
    #
    # Chosen against the dashboard's own behaviour, not picked round: the traces
    # panel polls every 3s (see admin/static/index.html), so an uncached
    # read-through would scan the usage table ~20 times a minute per open tab,
    # forever. The refresh is a paged scan whose cost grows with total history —
    # ~53 sequential 1MB round-trips at 100k records, about 1.4s — so
    # per-request refreshing degrades into a dashboard that is slower the longer
    # the gateway has run.
    #
    # A ceiling of one scan per 10s makes that cost independent of both poll rate
    # and tab count, and 10s is already finer than the 3s panel can show a human
    # anything meaningful about. Money does not depend on this window at all:
    # ``fleet_spend`` reads the shared counter per call.
    USAGE_SYNC_TTL_SECONDS = 10.0

    def __init__(
        self,
        pricing_config: dict[str, dict[str, TokenPricing]],
        budgets: dict[str, dict] | None = None,
        persistence: DynamoPersistence | None = None,
    ):
        self.pricing_config = pricing_config
        self._records: list[UsageRecord] = []
        # Running spend counters keyed by project_id / user_id. These are the
        # AUTHORITATIVE source for budget checks: O(1) instead of summing the
        # whole record list, and they survive record-list trimming (which used
        # to drop the oldest 50k records and silently under-count budgets).
        self._project_spend: defaultdict[str, float] = defaultdict(float)
        self._user_spend: defaultdict[str, float] = defaultdict(float)
        self._project_spend_epochs: dict[str, int] = {}
        self._user_spend_epochs: dict[str, int] = {}
        self._budgets: dict[str, dict] = budgets or {}
        self._user_budgets: dict[str, dict] = {}
        self._persistence = persistence
        # Monotonic timestamp of the last fleet-wide usage refresh, and the
        # in-flight refresh if any. Negative infinity rather than 0 so the first
        # read always refreshes: time.monotonic() has an arbitrary origin and can
        # legitimately be near 0 early in the process.
        self._last_usage_sync = float("-inf")
        self._usage_sync_task: asyncio.Task | None = None

    def has_pricing(self, provider: str, model: str) -> bool:
        """Return whether a provider/model has a usable billing rate."""
        pricing = self.pricing_config.get(provider, {}).get(model)
        return pricing is not None and pricing.is_billable

    async def _refresh_spend_state(
        self,
        scope: str,
        scope_id: str,
    ) -> None:
        if self._persistence is None or not self._persistence.enabled:
            return
        getter = getattr(self._persistence, "get_spend_state", None)
        if not callable(getter):
            return
        state = await getter(scope, scope_id)
        if state is None:
            return
        totals = (
            self._project_spend
            if scope == "project"
            else self._user_spend
        )
        epochs = (
            self._project_spend_epochs
            if scope == "project"
            else self._user_spend_epochs
        )
        current_epoch = epochs.get(scope_id, 0)
        if state.epoch < current_epoch:
            return
        if state.epoch > current_epoch:
            epochs[scope_id] = int(state.epoch)
            totals[scope_id] = float(state.total)
            return
        totals[scope_id] = max(
            float(state.total),
            totals.get(scope_id, 0.0),
        )

    def clear_project_spend(
        self,
        project_id: str,
        *,
        tenant_id: str | None = None,
    ) -> None:
        """Clear one process's project total after a shared cycle reset."""
        scope_id = _tenant_scoped_id(
            tenant_id,
            "project",
            project_id,
        )
        self._project_spend.pop(scope_id, None)
        self._project_spend_epochs.pop(scope_id, None)

    async def synced_records(self) -> list[UsageRecord]:
        """The record list, refreshed fleet-wide at most once per TTL.

        Lives here rather than on ``AdminAPI`` because it has two callers now —
        the admin aggregates and ``GET /api/users`` — and they must share one
        window and one in-flight scan. Two copies of this logic would mean two
        clocks, so the chat selector and the dashboard could refresh on different
        beats and disagree about who exists, and a burst across both would issue
        two scans where one would do.

        The clock advances only on a refresh that actually happened, so a
        persistence outage retries on the next read rather than serving
        single-instance numbers for a full window after the store recovered.

        Concurrent callers share one scan: the TTL check alone cannot bound
        anything, because it straddles an await and every overlapping request
        passes it before the first one finishes.
        """
        now = time.monotonic()
        if now - self._last_usage_sync < self.USAGE_SYNC_TTL_SECONDS:
            return self._records

        if self._usage_sync_task is None or self._usage_sync_task.done():
            self._usage_sync_task = asyncio.create_task(self.sync_records_from_store())
        try:
            synced = await asyncio.shield(self._usage_sync_task)
        except Exception:
            # A refresh that raised must not fail the caller's page — the local
            # records are still a truthful answer for this instance, which is what
            # these endpoints returned before any of this existed.
            logger.warning("Fleet-wide usage refresh failed", exc_info=True)
            synced = False
        if synced:
            self._last_usage_sync = now
        return self._records

    # ------------------------------------------------------------------
    # Budget / project registration
    # ------------------------------------------------------------------

    def register_project(
        self,
        project_id: str,
        budget_limit: float | None = None,
        alert_threshold: float | None = None,
        *,
        tenant_id: str | None = None,
    ) -> None:
        """Register a project with optional budget limit and alert threshold."""
        scope_id = _tenant_scoped_id(tenant_id, "project", project_id)
        self._budgets[scope_id] = {
            "budget_limit": budget_limit,
            "alert_threshold": alert_threshold,
        }

    def register_user(
        self,
        user_id: str,
        budget_limit: float | None = None,
        alert_threshold: float | None = None,
        *,
        tenant_id: str | None = None,
    ) -> None:
        """Register a user with optional budget limit and alert threshold."""
        scope_id = _tenant_scoped_id(tenant_id, "user", user_id)
        self._user_budgets[scope_id] = {
            "budget_limit": budget_limit,
            "alert_threshold": alert_threshold,
        }

    def get_user_budget(
        self,
        user_id: str,
        *,
        tenant_id: str | None = None,
    ) -> dict:
        """Return budget info for a user, or empty defaults."""
        scope_id = _tenant_scoped_id(tenant_id, "user", user_id)
        return self._user_budgets.get(
            scope_id,
            {"budget_limit": None, "alert_threshold": None},
        )

    async def check_user_budget(
        self,
        user_id: str,
        *,
        tenant_id: str | None = None,
    ) -> BudgetStatus:
        """Check whether a user is within their budget limits."""
        scope_id = _tenant_scoped_id(tenant_id, "user", user_id)
        budget_info = self._user_budgets.get(scope_id, {})
        budget_limit: float | None = budget_info.get("budget_limit")
        alert_threshold: float | None = budget_info.get("alert_threshold")

        if budget_limit is not None or alert_threshold is not None:
            await self._refresh_spend_state("user", scope_id)
        current_spend = self._user_spend.get(scope_id, 0.0)

        is_over_budget = (
            budget_limit is not None and current_spend >= budget_limit
        )
        is_alert_triggered = (
            alert_threshold is not None and current_spend >= alert_threshold
        )

        return BudgetStatus(
            project_id=user_id,
            current_spend=current_spend,
            budget_limit=budget_limit,
            alert_threshold=alert_threshold,
            is_over_budget=is_over_budget,
            is_alert_triggered=is_alert_triggered,
        )


    # ------------------------------------------------------------------
    # Cost calculation
    # ------------------------------------------------------------------

    def calculate_cost(
        self,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int = 0,
        image_tokens: int = 0,
        reasoning_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> float:
        """Calculate cost using per-token pricing config.

        Formula:
          ((prompt_tokens - cached_tokens - cache_creation_tokens) / 1000 * prompt_token_cost)
        + (completion_tokens / 1000 * completion_token_cost)
        + (cached_tokens / 1000 * cached_rate)              [cached_rate = cached_token_cost or prompt_token_cost]
        + (cache_creation_tokens / 1000 * creation_rate)     [creation_rate = cache_creation_token_cost or prompt_token_cost]
        + (image_tokens / 1000 * image_token_cost)           [if configured]
        + (reasoning_tokens / 1000 * reasoning_token_cost)   [if configured]
        + per_request_cost                                   [flat fee per call]

        Returns 0.0 if no pricing is configured for the provider/model.
        """
        provider_pricing = self.pricing_config.get(provider, {})
        pricing: TokenPricing | None = provider_pricing.get(model)
        if pricing is None or not pricing.is_billable:
            return 0.0

        # Determine effective rates with fallback to prompt_token_cost
        cached_rate = pricing.cached_token_cost if pricing.cached_token_cost is not None else pricing.prompt_token_cost
        creation_rate = pricing.cache_creation_token_cost if pricing.cache_creation_token_cost is not None else pricing.prompt_token_cost

        # Subtract cached + creation from prompt to avoid double-billing
        billable_prompt = max(0, prompt_tokens - cached_tokens - cache_creation_tokens)

        cost = (billable_prompt / 1000 * pricing.prompt_token_cost) + (
            completion_tokens / 1000 * pricing.completion_token_cost
        )

        if cached_tokens > 0:
            cost += cached_tokens / 1000 * cached_rate

        if cache_creation_tokens > 0:
            cost += cache_creation_tokens / 1000 * creation_rate

        if image_tokens > 0 and pricing.image_token_cost is not None:
            cost += image_tokens / 1000 * pricing.image_token_cost

        if reasoning_tokens > 0 and pricing.reasoning_token_cost is not None:
            cost += reasoning_tokens / 1000 * pricing.reasoning_token_cost

        cost += pricing.per_request_cost

        return cost

    # ------------------------------------------------------------------
    # Usage recording
    # ------------------------------------------------------------------

    def _bump_spend(self, usage: UsageRecord) -> None:
        """Add one record's cost to the running spend counters."""
        if usage.project_id:
            project_scope = _tenant_scoped_id(
                usage.tenant_id,
                "project",
                usage.project_id,
            )
            self._project_spend[project_scope] += usage.cost
        if usage.user_id:
            user_scope = _tenant_scoped_id(
                usage.tenant_id,
                "user",
                usage.user_id,
            )
            self._user_spend[user_scope] += usage.cost

    async def _bump_spend_fleet_wide(
        self,
        usage: UsageRecord,
        *,
        skip_scopes: frozenset[str] = frozenset(),
    ) -> None:
        """Add this record's cost to the shared counters and adopt the totals.

        The local counters are per-process, so on their own they make a budget
        limit a *per-instance* limit: with the shipped ``desired_count=2`` a $100
        cap admitted roughly $200, because neither task ever saw the other's
        spend. Usage records were always written to DynamoDB — reporting was
        fleet-wide and correct — but enforcement read a number only this process
        had contributed to.

        The atomic ``ADD`` returns the post-update total, so the instance learns
        the fleet figure from a write it is already making rather than from an
        extra read on the request path. Whatever comes back replaces the local
        counter, which also repairs drift: an instance that missed writes while
        DynamoDB was unreachable snaps back to the true total on its next
        success rather than staying permanently low.

        A failed counter update leaves the local total in place, so enforcement
        degrades to per-instance — the old behaviour — instead of to unlimited.
        """
        if self._persistence is None or not self._persistence.enabled:
            return
        if usage.project_id and "project" not in skip_scopes:
            project_scope = _tenant_scoped_id(
                usage.tenant_id,
                "project",
                usage.project_id,
            )
            add_state = getattr(
                self._persistence,
                "add_spend_state",
                None,
            )
            if callable(add_state):
                state = await add_state(
                    "project",
                    project_scope,
                    usage.cost,
                )
                if state is not None:
                    current_epoch = self._project_spend_epochs.get(
                        project_scope,
                        0,
                    )
                    if state.epoch >= current_epoch:
                        if state.epoch > current_epoch:
                            self._project_spend_epochs[project_scope] = (
                                int(state.epoch)
                            )
                            self._project_spend[project_scope] = float(
                                state.total
                            )
                        else:
                            self._project_spend[project_scope] = max(
                                float(state.total),
                                self._project_spend.get(
                                    project_scope,
                                    0.0,
                                ),
                            )
            else:
                total = await self._persistence.add_spend(
                    "project",
                    project_scope,
                    usage.cost,
                )
                if total is not None:
                    self._project_spend[project_scope] = total
        if usage.user_id and "user" not in skip_scopes:
            user_scope = _tenant_scoped_id(
                usage.tenant_id,
                "user",
                usage.user_id,
            )
            add_state = getattr(
                self._persistence,
                "add_spend_state",
                None,
            )
            if callable(add_state):
                state = await add_state(
                    "user",
                    user_scope,
                    usage.cost,
                )
                if state is not None:
                    current_epoch = self._user_spend_epochs.get(
                        user_scope,
                        0,
                    )
                    if state.epoch >= current_epoch:
                        if state.epoch > current_epoch:
                            self._user_spend_epochs[user_scope] = int(
                                state.epoch
                            )
                            self._user_spend[user_scope] = float(
                                state.total
                            )
                        else:
                            self._user_spend[user_scope] = max(
                                float(state.total),
                                self._user_spend.get(user_scope, 0.0),
                            )
            else:
                total = await self._persistence.add_spend(
                    "user",
                    user_scope,
                    usage.cost,
                )
                if total is not None:
                    self._user_spend[user_scope] = total

    def load_records(self, records: list[UsageRecord]) -> None:
        """Rehydrate the in-memory store from persisted usage records.

        Appends records (de-duped by request_id) AND updates the running spend
        counters, so budgets are correct after a restart. Use this instead of
        appending to ``_records`` directly — a raw append would leave the
        counters (which back budget checks) stale.
        """
        existing = {(r.tenant_id, r.request_id) for r in self._records}
        for rec in records:
            record_key = (rec.tenant_id, rec.request_id)
            if record_key in existing:
                continue
            self._records.append(rec)
            existing.add(record_key)
            self._bump_spend(rec)

    async def fleet_spend(
        self,
        scope: str,
        ident: str,
        *,
        tenant_id: str | None = None,
    ) -> float | None:
        """Fleet-wide spend for one project/user, read from the shared counter.

        The exact figure, with no dependence on the record list: the counter is a
        single item that every instance ``ADD``s to as it bills, so one read gets
        the whole fleet's spend. Cheap enough to do per admin request — one
        ``GetItem``, versus the paged scan ``sync_records_from_store`` needs — and
        unlike summing records it has no ceiling, so it stays right after
        ``MAX_RECORDS`` trimming has discarded old history.

        ``scope`` is ``"project"`` or ``"user"``, matching the shared counter keys.

        Deliberately does NOT write the result back into the local counters, even
        though that would look like a free cache warm-up. Those counters are live
        enforcement state, and this is a read path: an earlier version of this
        method assigned to them, which let a single ``GET /admin/projects`` reopen
        a closed budget gate. With ``get_spend`` returning 0.0 for an absent
        counter, a project at $120 against a $100 limit went ``is_over_budget``
        True → False because an operator loaded a page. Both halves are fixed —
        absent now reads as None — but a read that mutates the gate is the wrong
        shape regardless, so the assignment is gone too. ``/admin/users`` made it
        worst: one page load looped over every user and overwrote each counter.

        Returns None when there is no shared counter, it cannot be read, or
        persistence is off. Callers must treat None as "use the local figure"
        rather than as zero — a project that has spent money must never report $0
        because DynamoDB was briefly unavailable.
        """
        if self._persistence is None or not self._persistence.enabled:
            return None
        if scope not in {"project", "user"}:
            raise ValueError("scope must be 'project' or 'user'")
        scope_id = _tenant_scoped_id(tenant_id, scope, ident)
        try:
            return await self._persistence.get_spend(scope, scope_id)
        except Exception:
            logger.warning(
                "Failed to read shared %s spend for %s; falling back to local",
                scope, ident, exc_info=True,
            )
            return None

    async def sync_records_from_store(self) -> bool:
        """Re-read persisted usage records so aggregates cover the whole fleet.

        Admin aggregates sum ``_records``, which only ever contains what *this*
        process served plus what was loaded at startup. On a multi-instance
        deployment that makes every count and total depend on which task the load
        balancer picked: a two-task Fargate deployment returned ``total_cost`` of
        ``0.000132`` and ``0`` on alternating identical requests.

        Deliberately does NOT touch the spend counters, which is the one thing
        separating this from ``load_records``. ``_bump_spend_fleet_wide`` already
        *replaces* each counter with the shared fleet-wide total, so that total
        already includes every instance's spend. Folding these records in on top
        would count the other instances twice and start rejecting requests under a
        budget the project has not reached. Budget enforcement reads the shared
        counter and must keep owning that number; this method only widens the
        history that reporting can see.

        Returns whether the refresh happened, so a caller can tell "synced" from
        "persistence is off or the store could not be read, these numbers are one
        instance's" rather than guessing. A caller that rate-limits itself must
        only advance its clock on True, or one failure serves single-instance
        numbers for a whole window after the store has recovered.
        """
        if self._persistence is None or not self._persistence.enabled:
            return False
        try:
            records = await self._persistence.load_usage_records_or_none()
        except Exception:
            logger.warning(
                "Failed to refresh usage records for admin read; "
                "aggregates cover this instance only",
                exc_info=True,
            )
            return False
        # None is a failed read; [] is a genuinely empty store. Distinguished
        # because load_usage_records swallows its own exceptions and returns [],
        # so treating them alike reported success on every outage.
        if records is None:
            return False

        # Trim the SCANNED side before merging, never the merged list.
        #
        # Two bugs came from trimming afterwards. Local records sit at the head of
        # the merged list, so a tail slice cut exactly the records this instance
        # served whose store write had failed — the data the dedupe below exists to
        # preserve. And when the store held more than MAX_RECORDS, each sync
        # re-appended a different window and trimmed to a different half, so
        # total_requests pinned at MAX_RECORDS//2 while /admin/traces showed a
        # different set of requests every refresh with no new traffic.
        #
        # Keeping the newest scanned records also makes the bound mean something:
        # "the most recent N of fleet history" rather than "whichever N the scan
        # happened to return last".
        room = max(self.MAX_RECORDS - len(self._records), 0)
        if len(records) > room:
            records = sorted(
                records,
                key=lambda r: self._as_aware(r.timestamp) if r.timestamp else _EPOCH,
            )[-room:] if room else []

        # De-dupe against what is already here rather than replacing the list:
        # records this instance served since the last refresh are not in the store
        # yet if their write failed, and dropping them would make a refresh lose
        # data that the un-refreshed path would have shown.
        existing = {(r.tenant_id, r.request_id) for r in self._records}
        for rec in records:
            record_key = (rec.tenant_id, rec.request_id)
            if record_key not in existing:
                self._records.append(rec)
                existing.add(record_key)
        return True

    async def adopt_fleet_spend(
        self,
        project_ids,
        user_ids=(),
        *,
        tenant_id: str | None = None,
    ) -> None:
        """Replace local spend counters with the shared fleet-wide totals.

        Called once at startup, after ``load_records``. Both steps are needed and
        neither replaces the other: ``load_records`` sums the records this
        instance can see, which is what makes budgets survive a restart when
        persistence is off, while the shared counters are the figure enforcement
        must actually compare against.

        The totals are *replaced*, not added to. Adding would double-count —
        every record summed by ``load_records`` was already folded into the
        shared counter when it was first billed, so the two overlap completely
        rather than complementing each other.

        A counter that cannot be read leaves the local sum untouched, so a
        DynamoDB problem at startup degrades to per-instance enforcement rather
        than to a project that looks like it has spent nothing.
        """
        if self._persistence is None or not self._persistence.enabled:
            return
        for project_id in project_ids:
            scope_id = _tenant_scoped_id(
                tenant_id,
                "project",
                project_id,
            )
            getter = getattr(
                self._persistence,
                "get_spend_state",
                None,
            )
            if callable(getter):
                state = await getter("project", scope_id)
                if state is not None:
                    self._project_spend[scope_id] = float(state.total)
                    self._project_spend_epochs[scope_id] = int(state.epoch)
            else:
                total = await self._persistence.get_spend(
                    "project",
                    scope_id,
                )
                if total is not None:
                    self._project_spend[scope_id] = total
        for user_id in user_ids:
            scope_id = _tenant_scoped_id(tenant_id, "user", user_id)
            getter = getattr(
                self._persistence,
                "get_spend_state",
                None,
            )
            if callable(getter):
                state = await getter("user", scope_id)
                if state is not None:
                    self._user_spend[scope_id] = float(state.total)
                    self._user_spend_epochs[scope_id] = int(state.epoch)
            else:
                total = await self._persistence.get_spend(
                    "user",
                    scope_id,
                )
                if total is not None:
                    self._user_spend[scope_id] = total

    async def record_usage(
        self,
        usage: UsageRecord,
        *,
        share: bool = True,
        skip_shared_scopes: frozenset[str] = frozenset(),
    ) -> None:
        """Persist a usage record to the in-memory store.

        ``share=False`` records the cost locally without adding it to the shared
        fleet-wide counter. Used by the demo seed: every instance applies the
        same seed file at startup, so the fabricated spend is already consistent
        across the fleet, and ``ADD`` is not idempotent — sharing it would
        multiply demo spend by the instance count and again by every restart.
        """
        self._records.append(usage)
        # Counters are authoritative for budgets, so update them BEFORE any
        # trimming — trimming the record list must not lose spend.
        self._bump_spend(usage)
        if len(self._records) > self.MAX_RECORDS:
            self._records = self._records[-(self.MAX_RECORDS // 2):]

        # Fire-and-forget DynamoDB write
        if self._persistence is not None and self._persistence.enabled:
            try:
                await self._persistence.save_usage_record(usage)
            except Exception:
                logger.warning(
                    "Failed to persist usage record %s to DynamoDB",
                    usage.request_id,
                    exc_info=True,
                )

        # Fold this cost into the shared counters and adopt the fleet totals, so
        # the next budget check compares against every instance's spend rather
        # than only this process's. Kept separate from the record write above
        # because the two fail independently: a dropped record costs a row of
        # history, a dropped counter update costs budget accuracy.
        if share:
            await self._bump_spend_fleet_wide(
                usage,
                skip_scopes=skip_shared_scopes,
            )

    def adopt_reserved_spend(
        self,
        usage: UsageRecord,
        totals: dict[str, float],
    ) -> None:
        """Adopt shared totals returned by atomic budget finalization."""
        if usage.project_id and "project" in totals:
            project_scope = _tenant_scoped_id(
                usage.tenant_id,
                "project",
                usage.project_id,
            )
            self._project_spend[project_scope] = max(
                totals["project"],
                self._project_spend.get(project_scope, 0.0),
            )
        if usage.user_id and "user" in totals:
            self.adopt_user_spend(
                usage.user_id,
                totals["user"],
                tenant_id=usage.tenant_id,
            )

    def adopt_user_spend(
        self,
        user_id: str,
        total: float,
        *,
        tenant_id: str | None = None,
    ) -> None:
        """Move the local user counter forward to a durable fleet total."""
        user_scope = _tenant_scoped_id(
            tenant_id,
            "user",
            user_id,
        )
        self._user_spend[user_scope] = max(
            total,
            self._user_spend.get(user_scope, 0.0),
        )

    # ------------------------------------------------------------------
    # Token estimation
    # ------------------------------------------------------------------

    async def estimate_tokens(self, text: str, model: str) -> int:
        """Estimate token count using tiktoken when the provider doesn't return usage.

        Falls back to cl100k_base encoding if the model is not recognised by tiktoken.
        """
        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding(DEFAULT_CONFIG.token_estimation.fallback_encoding)
        return len(encoding.encode(text))

    # ------------------------------------------------------------------
    # Budget checking
    # ------------------------------------------------------------------

    async def check_budget(
        self,
        project_id: str,
        *,
        tenant_id: str | None = None,
    ) -> BudgetStatus:
        """Check whether a project is within its budget limits.

        Returns a BudgetStatus with alert/exceeded flags set appropriately.
        If the project has no registered budget, limits are None and flags are False.
        """
        scope_id = _tenant_scoped_id(tenant_id, "project", project_id)
        budget_info = self._budgets.get(scope_id, {})
        budget_limit: float | None = budget_info.get("budget_limit")
        alert_threshold: float | None = budget_info.get("alert_threshold")

        if budget_limit is not None or alert_threshold is not None:
            await self._refresh_spend_state("project", scope_id)
        current_spend = self._project_spend.get(scope_id, 0.0)

        is_over_budget = (
            budget_limit is not None and current_spend >= budget_limit
        )
        is_alert_triggered = (
            alert_threshold is not None and current_spend >= alert_threshold
        )

        return BudgetStatus(
            project_id=project_id,
            current_spend=current_spend,
            budget_limit=budget_limit,
            alert_threshold=alert_threshold,
            is_over_budget=is_over_budget,
            is_alert_triggered=is_alert_triggered,
        )

    # ------------------------------------------------------------------
    # Aggregated usage
    # ------------------------------------------------------------------

    async def get_aggregated_usage(self, filters: UsageFilters) -> UsageReport:
        """Query aggregated usage data with optional filters.

        Filters: time range, provider, model, project_id, user_id.
        Returns a UsageReport with totals and per-provider breakdown.
        """
        filtered = self._apply_filters(filters)

        total_requests = len(filtered)
        total_tokens = sum(r.total_tokens for r in filtered)
        total_cost = sum(r.cost for r in filtered)

        breakdown = self._build_breakdown(filtered)

        return UsageReport(
            total_requests=total_requests,
            total_tokens=total_tokens,
            total_cost=total_cost,
            breakdown=breakdown,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _as_aware(ts: datetime) -> datetime:
        """Coerce a timestamp to tz-aware UTC so time-window filters never mix
        naive and aware datetimes (which raises TypeError). Records may arrive
        naive from older callers or persisted rows; filter bounds may be either.
        """
        if ts is None:
            return ts
        return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts

    def _apply_filters(self, filters: UsageFilters) -> list[UsageRecord]:
        """Return records matching all non-None filter criteria."""
        result = self._records
        if filters.start_time is not None:
            start = self._as_aware(filters.start_time)
            result = [r for r in result if self._as_aware(r.timestamp) >= start]
        if filters.end_time is not None:
            end = self._as_aware(filters.end_time)
            result = [r for r in result if self._as_aware(r.timestamp) <= end]
        if filters.provider is not None:
            result = [r for r in result if r.provider == filters.provider]
        if filters.model is not None:
            result = [r for r in result if r.model == filters.model]
        if filters.project_id is not None:
            result = [r for r in result if r.project_id == filters.project_id]
        if filters.user_id is not None:
            result = [r for r in result if r.user_id == filters.user_id]
        if filters.tenant_id is not None:
            result = [r for r in result if r.tenant_id == filters.tenant_id]
        return result

    def _build_breakdown(self, records: list[UsageRecord]) -> list[UsageBreakdown]:
        """Build usage breakdowns grouped by provider, model, project, and user."""
        breakdowns: list[UsageBreakdown] = []

        for group_by, key_fn in [
            ("provider", lambda r: r.provider),
            ("model", lambda r: r.model),
            ("project", lambda r: r.project_id),
            ("user", lambda r: r.user_id),
        ]:
            groups: dict[str, list[UsageRecord]] = defaultdict(list)
            for r in records:
                groups[key_fn(r)].append(r)
            for group_key, group_records in groups.items():
                breakdowns.append(
                    UsageBreakdown(
                        group_key=group_key,
                        group_by=group_by,
                        requests=len(group_records),
                        tokens=sum(r.total_tokens for r in group_records),
                        cost=sum(r.cost for r in group_records),
                    )
                )

        return breakdowns
