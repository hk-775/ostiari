"""Quota enforcer — rate limits, budget enforcement, and cost tracking.

Reconciled with AxonLLM's quota enforcement:
- Per-model pricing table (pushed from control plane)
- Local cost calculation (not 0.0 anymore)
- Pre-request budget projection (blocks BEFORE overspend)
- Max tokens silent cap
- Alert thresholds (80%, 90%, 100%)
- Token estimation for pre-request projection
"""

import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any

log = logging.getLogger("ostiari.sidecar.quota")

BUDGET_ALERT_THRESHOLDS = [0.8, 0.9, 1.0]

# Default pricing per 1k tokens (fallback if no pricing config pushed)
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"input": 0.003, "output": 0.015},
    "claude-haiku-4-5": {"input": 0.0008, "output": 0.004},
    "claude-opus-4-6": {"input": 0.015, "output": 0.075},
    "gpt-4o": {"input": 0.0025, "output": 0.01},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "o4-mini": {"input": 0.0011, "output": 0.0044},
    "command-r-plus": {"input": 0.003, "output": 0.015},
    "gemini-2.5-flash": {"input": 0.000075, "output": 0.0003},
}

# Average tokens per request (heuristic for pre-request estimation)
AVG_INPUT_TOKENS = 800
AVG_OUTPUT_TOKENS = 400


class QuotaConfig:
    """Quota configuration pushed from control plane."""

    def __init__(
        self,
        rate_limit_rpm: int | None = None,
        budget_limit_usd: float | None = None,
        max_tokens_per_request: int | None = None,
        allowed_models: list[str] | None = None,
        pricing: dict[str, dict[str, float]] | None = None,
    ) -> None:
        self.rate_limit_rpm = rate_limit_rpm
        self.budget_limit_usd = budget_limit_usd
        self.max_tokens_per_request = max_tokens_per_request
        self.allowed_models = allowed_models
        self.pricing = pricing or {}


class QuotaDecision:
    """Result of a quota check."""

    def __init__(self, allowed: bool = True, reason: str = "", limit_type: str = "") -> None:
        self.allowed = allowed
        self.reason = reason
        self.limit_type = limit_type
        # Set when check(reserve=True) booked an in-flight budget reservation;
        # pass it back to record_spend() to release it.
        self.reservation_id: int | None = None


class QuotaEnforcer:
    """Enforces rate limits, budgets, and model restrictions.

    Mirrors AxonLLM's enforcement with:
    - Pre-request cost projection
    - Alert thresholds at 80/90/100%
    - Per-model pricing for accurate cost tracking
    - Silent max_tokens cap
    """

    def __init__(self) -> None:
        self._config: QuotaConfig | None = None
        self._request_times: deque[float] = deque()
        self._total_spend: float = 0.0
        self._window_seconds: float = 60.0
        self._alerted_thresholds: set[float] = set()
        self._alert_callbacks: list[Callable[[str, float, float], None]] = []
        # In-flight budget reservations: reservation_id -> (amount, monotonic_ts).
        # A reservation is booked at check(reserve=True) time and released at
        # record_spend(). Counting live reservations in the budget projection
        # closes the TOCTOU window where many concurrent LLM calls each read the
        # same stale _total_spend (before any awaited upstream call settles) and
        # all pass the budget gate, overshooting budget_limit_usd. Reservations
        # self-expire after a TTL so a request that errors before record_spend
        # can't leak one (the projection just stays briefly conservative — the
        # safe direction for a hard budget).
        self._reservations: dict[int, tuple[float, float]] = {}
        self._reservation_seq: int = 0
        self._reservation_ttl: float = 120.0
        # Optional Redis-backed shared store. When attached, budget spend is
        # tracked fleet-wide (atomic reserve/adjust) instead of per-process, so a
        # scaled fleet enforces one budget rather than N×. Rate limiting is
        # handled by the middleware's own shared path; here it only affects
        # budget. None = per-process (unchanged behavior).
        self._store: Any = None
        # Reservation ids that were reserved in the SHARED store (vs local-only),
        # so record_spend/release reconcile the right place.
        self._shared_reservations: set[int] = set()
        # Stable per-process budget key; the control plane can override via a
        # config field so multiple gateways share (or partition) one budget.
        self._budget_key: str = "gateway"

    def attach_shared_store(self, store: Any) -> None:
        """Attach (or clear) the Redis-backed shared store. Safe to call with None."""
        self._store = store

    def configure(self, config: dict[str, Any]) -> None:
        """Update quota config (pushed from control plane)."""
        self._config = QuotaConfig(
            rate_limit_rpm=config.get("rate_limit_rpm"),
            budget_limit_usd=config.get("budget_limit_usd"),
            max_tokens_per_request=config.get("max_tokens_per_request"),
            allowed_models=config.get("allowed_models"),
            pricing=config.get("pricing"),
        )
        # Optional shared-budget key: gateways sharing this key share one
        # fleet-wide budget in Redis. Defaults to "gateway".
        self._budget_key = str(config.get("budget_key") or self._budget_key)
        log.info(
            "Quota configured: rpm=%s, budget=$%s, max_tokens=%s, models=%s, pricing_models=%d",
            self._config.rate_limit_rpm,
            self._config.budget_limit_usd,
            self._config.max_tokens_per_request,
            self._config.allowed_models,
            len(self._config.pricing),
        )

    def on_budget_alert(self, callback: Callable[[str, float, float], None]) -> None:
        """Register a callback for budget threshold alerts.

        Callback receives: (threshold_label, current_spend, budget_limit)
        """
        self._alert_callbacks.append(callback)

    def check(
        self,
        model: str | None = None,
        estimated_cost: float | None = None,
        reserve: bool = False,
    ) -> QuotaDecision:
        """Check if the current request is allowed under quota limits.

        Args:
            model: Model being requested (for allowlist check)
            estimated_cost: Estimated cost of this request (for budget projection)
            reserve: When True and the check passes on budget, atomically book a
                budget reservation for ``estimated_cost`` (returned as
                ``decision.reservation_id``). The caller MUST later call
                ``record_spend(actual, reservation_id=...)`` to release it. This
                makes concurrent LLM calls see each other's in-flight spend and
                prevents overshooting a hard budget across the awaited upstream
                call. No await occurs between the projection and the booking, so
                the reserve is atomic under asyncio.
        """
        if self._config is None:
            return QuotaDecision(allowed=True)

        # Rate limit check
        if self._config.rate_limit_rpm is not None:
            self._prune_old_requests()
            if len(self._request_times) >= self._config.rate_limit_rpm:
                return QuotaDecision(
                    allowed=False,
                    reason=f"Rate limit exceeded: {self._config.rate_limit_rpm} requests/min",
                    limit_type="rate_limit",
                )

        # Budget check with projection.
        #  - Shared store attached: budget lives in Redis (fleet-wide). A
        #    reserving check does an ATOMIC reserve there (check+add in one Lua
        #    op); a non-reserving check reads the shared spend and projects.
        #  - No store: in-process projection incl. local in-flight reservations
        #    (closes the single-process TOCTOU; unchanged behavior).
        est = estimated_cost or 0
        limit = self._config.budget_limit_usd
        _shared_reserved = False
        if limit is not None:
            if self._store is not None:
                if reserve and est > 0:
                    if not self._store.budget_reserve(self._budget_key, est, limit):
                        return QuotaDecision(
                            allowed=False,
                            reason=f"Budget would be exceeded (fleet): ${limit:.2f} limit",
                            limit_type="budget",
                        )
                    _shared_reserved = True
                else:
                    projected = self._store.budget_spend(self._budget_key) + est
                    if projected >= limit:
                        return QuotaDecision(
                            allowed=False,
                            reason=f"Budget would be exceeded (fleet): ${projected:.4f} / ${limit:.2f} limit",
                            limit_type="budget",
                        )
            else:
                projected = self._total_spend + self._reserved_total() + est
                if projected >= limit:
                    return QuotaDecision(
                        allowed=False,
                        reason=f"Budget would be exceeded: ${projected:.4f} projected / ${limit:.2f} limit",
                        limit_type="budget",
                    )

        # Model allowlist check
        if (self._config.allowed_models is not None and model
                and model not in self._config.allowed_models):
            return QuotaDecision(
                allowed=False,
                reason=f"Model '{model}' not in allowed list: {self._config.allowed_models}",
                limit_type="model_restriction",
            )

        decision = QuotaDecision(allowed=True)
        # Book a reservation so record_spend can reconcile estimate→actual.
        # Shared: the amount is already added to Redis (atomic reserve above);
        # we just remember it. Local: track in-flight for the projection above.
        # No await between projection and booking, so it's atomic under asyncio.
        if reserve and limit is not None and est > 0:
            self._reservation_seq += 1
            rid = self._reservation_seq
            self._reservations[rid] = (est, time.monotonic())
            if _shared_reserved:
                self._shared_reservations.add(rid)
            decision.reservation_id = rid
        return decision

    def _reserved_total(self) -> float:
        """Sum of live (non-expired) budget reservations; prunes expired ones."""
        now = time.monotonic()
        expired = [rid for rid, (_, ts) in self._reservations.items()
                   if now - ts > self._reservation_ttl]
        for rid in expired:
            del self._reservations[rid]
        return sum(amount for amount, _ in self._reservations.values())

    def cap_max_tokens(self, requested: int) -> int:
        """Return effective max_tokens — capped by quota if configured."""
        if self._config is None or self._config.max_tokens_per_request is None:
            return requested
        return min(requested, self._config.max_tokens_per_request)

    def estimate_cost(self, model: str, input_tokens: int | None = None, output_tokens: int | None = None) -> float:
        """Estimate the cost of a request.

        Uses pricing config if available, falls back to defaults.
        If token counts not provided, uses heuristic averages.
        """
        input_tok = input_tokens or AVG_INPUT_TOKENS
        output_tok = output_tokens or AVG_OUTPUT_TOKENS

        pricing = self._get_pricing(model)
        return (input_tok * pricing["input"] / 1000) + (output_tok * pricing["output"] / 1000)

    def calculate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Calculate actual cost after a request completes."""
        pricing = self._get_pricing(model)
        return (input_tokens * pricing["input"] / 1000) + (output_tokens * pricing["output"] / 1000)

    def record_request(self) -> None:
        """Record that a request was made (for rate limiting)."""
        self._request_times.append(time.monotonic())

    def record_spend(self, cost_usd: float, reservation_id: int | None = None) -> None:
        """Record actual spend and fire alerts if thresholds crossed.

        If a reservation_id from check(reserve=True) is supplied, release that
        reservation — its estimated amount stops counting toward the projection
        now that the real cost is booked here.

        With a shared store, a shared reservation already added the *estimate* to
        Redis; here we adjust by (actual - estimate) so the fleet total reflects
        the real cost. Without a store, we book the actual into local spend.
        """
        est = 0.0
        if reservation_id is not None:
            resv = self._reservations.pop(reservation_id, None)
            if resv is not None:
                est = resv[0]
        if reservation_id is not None and reservation_id in self._shared_reservations:
            self._shared_reservations.discard(reservation_id)
            if self._store is not None:
                # Reconcile the pre-added estimate to the actual cost.
                self._store.budget_adjust(self._budget_key, cost_usd - est)
        elif self._store is not None:
            # No shared reservation (e.g. a spend recorded without reserve=True) —
            # add the actual cost to the fleet total directly.
            self._store.budget_adjust(self._budget_key, cost_usd)
        else:
            self._total_spend += cost_usd
        self._check_alert_thresholds()

    def release_reservation(self, reservation_id: int | None) -> None:
        """Release a budget reservation without recording spend (request failed
        before it incurred cost). Safe to call with None or an unknown id."""
        if reservation_id is None:
            return
        resv = self._reservations.pop(reservation_id, None)
        if reservation_id in self._shared_reservations:
            self._shared_reservations.discard(reservation_id)
            if self._store is not None and resv is not None:
                # Subtract the estimate we optimistically added at reserve time.
                self._store.budget_adjust(self._budget_key, -resv[0])

    def get_status(self) -> dict[str, Any]:
        """Get current quota status for reporting."""
        self._prune_old_requests()
        spend = self._store.budget_spend(self._budget_key) if self._store is not None else self._total_spend
        status: dict[str, Any] = {
            "current_rpm": len(self._request_times),
            "current_spend": round(spend, 4),
            "spend_scope": "fleet" if self._store is not None else "process",
        }
        if self._config:
            status["rate_limit_rpm"] = self._config.rate_limit_rpm
            status["budget_limit_usd"] = self._config.budget_limit_usd
            status["max_tokens_per_request"] = self._config.max_tokens_per_request
            status["allowed_models"] = self._config.allowed_models
            if self._config.budget_limit_usd:
                status["budget_pct_used"] = round(
                    (spend / self._config.budget_limit_usd) * 100, 1
                )
            status["pricing_models"] = len(self._config.pricing) + len(DEFAULT_PRICING)
        return status

    def reset_spend(self) -> None:
        """Reset spend counter (e.g., at start of billing period)."""
        self._total_spend = 0.0
        self._alerted_thresholds.clear()
        if self._store is not None:
            self._store.budget_reset(self._budget_key)

    def _get_pricing(self, model: str) -> dict[str, float]:
        """Get pricing for a model, preferring what the control plane pushed.

        Exact matches win over fuzzy ones in BOTH tables before either table's
        fuzzy pass runs — otherwise a pushed "gpt-4o-mini" could lose to a fuzzy
        hit on the built-in "gpt-4o", which is 16x more expensive.
        """
        pushed = self._config.pricing if (self._config and self._config.pricing) else {}
        if model in pushed:
            return pushed[model]
        if model in DEFAULT_PRICING:
            return DEFAULT_PRICING[model]
        # Fuzzy match — pushed prices first, since the operator set them
        # explicitly and they cover models the built-in table doesn't know
        # (Bedrock ids, in particular, are only ever reachable this way).
        for table in (pushed, DEFAULT_PRICING):
            for key, pricing in table.items():
                if key in model or model in key:
                    return pricing
        # Fallback: assume mid-range pricing
        return {"input": 0.003, "output": 0.015}

    def _check_alert_thresholds(self) -> None:
        """Fire alert callbacks when budget thresholds are crossed."""
        if not self._config or not self._config.budget_limit_usd:
            return
        # Read spend the same way get_status() does. Using _total_spend directly
        # meant that with a shared store (Redis) — where spend is booked to Redis
        # and _total_spend stays 0.0 — the percentage was always 0 and no alert
        # ever fired. Fleet deployments are exactly where alerting matters most.
        spend = self._store.budget_spend(self._budget_key) if self._store is not None else self._total_spend
        pct = spend / self._config.budget_limit_usd
        for threshold in BUDGET_ALERT_THRESHOLDS:
            if pct >= threshold and threshold not in self._alerted_thresholds:
                self._alerted_thresholds.add(threshold)
                label = f"{int(threshold * 100)}%"
                log.warning(
                    "Budget alert [%s]: $%.4f / $%.2f (%s used)",
                    label, spend, self._config.budget_limit_usd, label,
                )
                for cb in self._alert_callbacks:
                    try:
                        cb(label, spend, self._config.budget_limit_usd)
                    except Exception as e:  # noqa: BLE001
                        # One bad callback must not stop the others firing or
                        # break the call that triggered the alert. Logged rather
                        # than suppressed outright: a silently-broken budget
                        # alert is exactly the failure nobody notices.
                        log.warning("Budget alert callback failed: %s", e)

    def _prune_old_requests(self) -> None:
        """Remove requests older than the sliding window."""
        cutoff = time.monotonic() - self._window_seconds
        while self._request_times and self._request_times[0] < cutoff:
            self._request_times.popleft()
