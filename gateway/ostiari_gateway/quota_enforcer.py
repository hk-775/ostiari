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

    def configure(self, config: dict[str, Any]) -> None:
        """Update quota config (pushed from control plane)."""
        self._config = QuotaConfig(
            rate_limit_rpm=config.get("rate_limit_rpm"),
            budget_limit_usd=config.get("budget_limit_usd"),
            max_tokens_per_request=config.get("max_tokens_per_request"),
            allowed_models=config.get("allowed_models"),
            pricing=config.get("pricing"),
        )
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

    def check(self, model: str | None = None, estimated_cost: float | None = None) -> QuotaDecision:
        """Check if the current request is allowed under quota limits.

        Args:
            model: Model being requested (for allowlist check)
            estimated_cost: Estimated cost of this request (for budget projection)
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

        # Budget check with projection
        if self._config.budget_limit_usd is not None:
            projected = self._total_spend + (estimated_cost or 0)
            if projected >= self._config.budget_limit_usd:
                return QuotaDecision(
                    allowed=False,
                    reason=f"Budget would be exceeded: ${projected:.4f} projected / ${self._config.budget_limit_usd:.2f} limit",
                    limit_type="budget",
                )

        # Model allowlist check
        if self._config.allowed_models is not None and model:
            if model not in self._config.allowed_models:
                return QuotaDecision(
                    allowed=False,
                    reason=f"Model '{model}' not in allowed list: {self._config.allowed_models}",
                    limit_type="model_restriction",
                )

        return QuotaDecision(allowed=True)

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

    def record_spend(self, cost_usd: float) -> None:
        """Record spend and fire alerts if thresholds crossed."""
        self._total_spend += cost_usd
        self._check_alert_thresholds()

    def get_status(self) -> dict[str, Any]:
        """Get current quota status for reporting."""
        self._prune_old_requests()
        status: dict[str, Any] = {
            "current_rpm": len(self._request_times),
            "current_spend": round(self._total_spend, 4),
        }
        if self._config:
            status["rate_limit_rpm"] = self._config.rate_limit_rpm
            status["budget_limit_usd"] = self._config.budget_limit_usd
            status["max_tokens_per_request"] = self._config.max_tokens_per_request
            status["allowed_models"] = self._config.allowed_models
            if self._config.budget_limit_usd:
                status["budget_pct_used"] = round(
                    (self._total_spend / self._config.budget_limit_usd) * 100, 1
                )
            status["pricing_models"] = len(self._config.pricing) + len(DEFAULT_PRICING)
        return status

    def reset_spend(self) -> None:
        """Reset spend counter (e.g., at start of billing period)."""
        self._total_spend = 0.0
        self._alerted_thresholds.clear()

    def _get_pricing(self, model: str) -> dict[str, float]:
        """Get pricing for a model from config or defaults."""
        if self._config and self._config.pricing:
            if model in self._config.pricing:
                return self._config.pricing[model]
        if model in DEFAULT_PRICING:
            return DEFAULT_PRICING[model]
        # Fuzzy match
        for key, pricing in DEFAULT_PRICING.items():
            if key in model or model in key:
                return pricing
        # Fallback: assume mid-range pricing
        return {"input": 0.003, "output": 0.015}

    def _check_alert_thresholds(self) -> None:
        """Fire alert callbacks when budget thresholds are crossed."""
        if not self._config or not self._config.budget_limit_usd:
            return
        pct = self._total_spend / self._config.budget_limit_usd
        for threshold in BUDGET_ALERT_THRESHOLDS:
            if pct >= threshold and threshold not in self._alerted_thresholds:
                self._alerted_thresholds.add(threshold)
                label = f"{int(threshold * 100)}%"
                log.warning(
                    "Budget alert [%s]: $%.4f / $%.2f (%s used)",
                    label, self._total_spend, self._config.budget_limit_usd, label,
                )
                for cb in self._alert_callbacks:
                    try:
                        cb(label, self._total_spend, self._config.budget_limit_usd)
                    except Exception:
                        pass

    def _prune_old_requests(self) -> None:
        """Remove requests older than the sliding window."""
        cutoff = time.monotonic() - self._window_seconds
        while self._request_times and self._request_times[0] < cutoff:
            self._request_times.popleft()
