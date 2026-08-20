"""Smart routing strategy — selects models based on prompt classification."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from src.gateway.cost_tracker import CostTracker
from src.gateway.feedback_tracker import FeedbackTracker
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_leaderboard import ModelLeaderboard
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import (
    FeedbackRecord,
    ProviderModelMapping,
    SmartRoutingDecision,
    TokenPricing,
)
from src.gateway.routing import NoHealthyProviderError, RoutingStrategyBase
from src.gateway.task_classifier import TaskClassifier

logger = logging.getLogger(__name__)


class NoCandidateModelsError(Exception):
    """Raised when no candidate models remain after filtering."""


class SmartRoutingStrategy(RoutingStrategyBase):
    """Routing strategy that selects models based on prompt classification.

    When used as a per-model strategy (via select()), it acts as a simple
    health-aware provider selector. The full smart routing pipeline is
    accessed via select_model().
    """

    def __init__(
        self,
        classifier: TaskClassifier,
        leaderboard: ModelLeaderboard,
        model_registry: ModelRegistry,
        health_tracker: ProviderHealthTracker,
        cost_tracker: CostTracker,
        feedback_tracker: FeedbackTracker,
        confidence_threshold: float = 0.3,
        cost_quality_tradeoff: float = 0.3,
        default_model: str = "claude-sonnet",
        pricing_config: dict[str, dict[str, TokenPricing]] | None = None,
    ) -> None:
        self.classifier = classifier
        self.leaderboard = leaderboard
        self.model_registry = model_registry
        self.health_tracker = health_tracker
        self.cost_tracker = cost_tracker
        self.feedback_tracker = feedback_tracker
        self.confidence_threshold = confidence_threshold
        self.cost_quality_tradeoff = cost_quality_tradeoff
        self.default_model = default_model
        # Falls back to the tracker's own table when not passed explicitly, so a
        # caller that already wired pricing into CostTracker gets cost-aware
        # scoring without a second argument.
        if pricing_config is None:
            pricing_config = getattr(cost_tracker, "pricing_config", None) or {}
        self.pricing_config = pricing_config

    def select(
        self,
        providers: list[ProviderModelMapping],
        health_tracker: ProviderHealthTracker,
    ) -> ProviderModelMapping:
        """RoutingStrategyBase interface — selects among providers for an already-chosen model.

        Picks the first healthy provider (round-robin among healthy).
        """
        healthy = self._healthy_providers(providers, health_tracker)
        if not healthy:
            raise NoHealthyProviderError("No healthy providers available")
        return healthy[0]

    async def select_model(
        self,
        prompt: str,
        allowed_models: set[str] | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
        *,
        tenant_id: str | None = None,
    ) -> SmartRoutingDecision:
        """Full smart routing: classify prompt, score models, select best.

        Steps:
        1. Classify prompt
        2. Check confidence threshold
        3. Get leaderboard rankings for task type
        4. Filter by allowed_models
        5. Filter by health (at least one healthy provider)
        6. Filter by context window (estimate prompt tokens)
        7. Filter by budget
        8. Apply cost-quality tradeoff scoring
        9. Select top candidate
        10. Record feedback
        11. Return decision
        """
        # Step 1: Classify prompt
        classification = self.classifier.classify(prompt)
        task_type = classification.task_type
        confidence = classification.confidence

        candidates_considered: list[dict[str, Any]] = []

        # Step 2: Check confidence threshold
        if confidence < self.confidence_threshold:
            # Fallback to default model
            decision = SmartRoutingDecision(
                task_type=task_type,
                confidence=confidence,
                selected_model=self.default_model,
                benchmark_score=0.0,
                candidates_considered=candidates_considered,
                used_fallback=True,
                cost_quality_tradeoff=self.cost_quality_tradeoff,
            )
            await self._record_feedback(decision)
            return decision

        # Step 3: Get leaderboard rankings
        rankings = self.leaderboard.get_rankings(task_type)
        if not rankings:
            # No rankings for this task type — fallback
            decision = SmartRoutingDecision(
                task_type=task_type,
                confidence=confidence,
                selected_model=self.default_model,
                benchmark_score=0.0,
                candidates_considered=candidates_considered,
                used_fallback=True,
                cost_quality_tradeoff=self.cost_quality_tradeoff,
            )
            await self._record_feedback(decision)
            return decision

        # Build candidate list from rankings. These entries are heterogeneous by
        # design (model name, scores, filter reason, flags), so they are typed
        # Any rather than inferred — storing an optional cost otherwise widens
        # the value type to object and every later read of a score fails.
        candidates: list[dict[str, Any]] = []
        for model_score in rankings:
            model_name = model_score.model_name
            entry: dict[str, Any] = {
                "model": model_name,
                "benchmark_score": model_score.score,
            }

            # Step 4: Filter by allowed_models
            if allowed_models is not None and model_name not in allowed_models:
                entry["filtered_reason"] = "not_in_allowed_models"
                candidates_considered.append(entry)
                continue

            # Check model exists in registry
            if model_name not in self.model_registry.models:
                entry["filtered_reason"] = "not_in_registry"
                candidates_considered.append(entry)
                continue

            # Step 5: Filter by health
            model_config = self.model_registry.models[model_name]
            providers = model_config.providers
            has_healthy = any(
                self.health_tracker.is_healthy(p.provider) for p in providers
            )
            if not has_healthy:
                entry["filtered_reason"] = "all_providers_unhealthy"
                candidates_considered.append(entry)
                continue

            # Step 6: Filter by context window
            estimated_tokens = self._estimate_token_count(prompt)
            max_context = model_config.max_context_tokens
            if max_context is not None and max_context < estimated_tokens:
                entry["filtered_reason"] = "context_window_too_small"
                candidates_considered.append(entry)
                continue

            # Step 7: Filter by budget
            if project_id is not None:
                budget_status = await self.cost_tracker.check_budget(
                    project_id,
                    tenant_id=tenant_id,
                )
                if budget_status.is_over_budget:
                    entry["filtered_reason"] = "over_budget"
                    candidates_considered.append(entry)
                    continue

            if user_id is not None:
                user_budget = await self.cost_tracker.check_user_budget(
                    user_id,
                    tenant_id=tenant_id,
                )
                if user_budget.is_over_budget:
                    entry["filtered_reason"] = "over_budget"
                    candidates_considered.append(entry)
                    continue

            # Model passed all filters
            entry["passed"] = True
            candidates.append(entry)
            candidates_considered.append(entry)

        # Step 8: Score candidates with composite score
        if not candidates:
            raise NoCandidateModelsError(
                "No candidate models remain after filtering"
            )

        # Compute cost per token for each candidate. None means "not priced".
        for candidate in candidates:
            model_name = candidate["model"]
            model_config = self.model_registry.models[model_name]
            candidate["cost_per_token"] = self._get_model_cost(model_config)

        max_benchmark = max(c["benchmark_score"] for c in candidates)
        known_costs = [
            c["cost_per_token"] for c in candidates if c["cost_per_token"] is not None
        ]
        # Normalize against the priced candidates only — including unpriced ones
        # as 0.0 would deflate every other model's normalized cost.
        max_cost = max(known_costs) if known_costs else 0.0
        # Avoid division by zero when nothing is priced, or everything is free.
        if max_cost == 0:
            max_cost = 1.0

        # An unpriced candidate is scored at the mean of the known costs rather
        # than as free, so a missing price neither rewards nor penalizes a model.
        # Without this the cheapest-looking candidate is whichever one nobody
        # entered a price for.
        fallback_cost = sum(known_costs) / len(known_costs) if known_costs else 0.0

        for candidate in candidates:
            cost = candidate["cost_per_token"]
            candidate["composite_score"] = self._compute_composite_score(
                candidate["benchmark_score"],
                fallback_cost if cost is None else cost,
                max_benchmark,
                max_cost,
            )
            if cost is None:
                candidate["cost_estimated"] = True

        # Step 9: Select top candidate
        best = max(candidates, key=lambda c: c["composite_score"])
        selected_model = best["model"]
        benchmark_score = best["benchmark_score"]

        decision = SmartRoutingDecision(
            task_type=task_type,
            confidence=confidence,
            selected_model=selected_model,
            benchmark_score=benchmark_score,
            candidates_considered=candidates_considered,
            used_fallback=False,
            cost_quality_tradeoff=self.cost_quality_tradeoff,
        )

        # Step 10: Record feedback
        await self._record_feedback(decision)

        return decision

    def _estimate_token_count(self, prompt: str) -> int:
        """Rough token estimation: ~4 chars per token for English text."""
        return max(1, len(prompt) // 4)

    def _compute_composite_score(
        self,
        benchmark_score: float,
        cost_per_token: float,
        max_benchmark: float,
        max_cost: float,
    ) -> float:
        """Compute composite score using cost-quality tradeoff formula.

        composite = (1 - tradeoff) * normalized_benchmark + tradeoff * (1 - normalized_cost)
        """
        norm_benchmark = benchmark_score / max_benchmark if max_benchmark > 0 else 0.0
        norm_cost = cost_per_token / max_cost if max_cost > 0 else 0.0
        return (1 - self.cost_quality_tradeoff) * norm_benchmark + self.cost_quality_tradeoff * (1 - norm_cost)

    def _resolve_pricing(self, mapping: ProviderModelMapping) -> TokenPricing | None:
        """Find pricing for one provider mapping, or None if it is unknown.

        An inline ``pricing:`` block in models.yaml wins, since it is the more
        specific declaration. Otherwise this looks the mapping up in the shared
        pricing table, which is keyed by provider and *provider-side* model id —
        the same lookup CostTracker performs when billing the request, so the
        cost used for routing and the cost actually charged cannot disagree.
        """
        if mapping.pricing is not None and mapping.pricing.is_billable:
            return mapping.pricing
        pricing = self.pricing_config.get(mapping.provider, {}).get(
            mapping.model_id
        )
        if pricing is None or not pricing.is_billable:
            return None
        return pricing

    def _get_model_cost(self, model_config) -> float | None:
        """Average cost per token across a model's providers.

        Returns ``None`` — not 0.0 — when no provider has pricing. The
        distinction matters: 0.0 means "free" and would make an unpriced model
        the cheapest possible candidate, so a model would win for being
        *unmeasured* rather than for being cheap. That is not a hypothetical:
        6 of the 49 provider entries in the shipped config are unpriced because
        the provider publishes no rate for the id, and scoring the general task
        type that way hands it to claude-haiku (benchmark 78) over
        claude-sonnet (90).

        Providers that do have pricing are averaged and the unpriced ones are
        skipped, matching the previous behaviour for partially-priced models.
        """
        costs = []
        for provider in model_config.providers:
            pricing = self._resolve_pricing(provider)
            if pricing is not None:
                avg = (pricing.prompt_token_cost + pricing.completion_token_cost) / 2
                costs.append(avg)
        if costs:
            return sum(costs) / len(costs)
        return None

    async def _record_feedback(self, decision: SmartRoutingDecision) -> None:
        """Record a feedback entry for the routing decision."""
        feedback = FeedbackRecord(
            request_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc),
            task_type=decision.task_type,
            confidence=decision.confidence,
            selected_model=decision.selected_model,
            benchmark_score=decision.benchmark_score,
        )
        await self.feedback_tracker.record_async(feedback)
