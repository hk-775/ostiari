"""Level 2+3 semantic efficiency engine — ML-based token waste detection and model right-sizing.

Provides:
- Prompt complexity scoring and model right-sizing recommendations
- Output utilization analysis (are responses being truncated or ignored?)
- Prompt compression detection (could the prompt be shorter?)
- Task-model matching using the existing TaskClassifier
- Historical pattern learning for per-user optimization

Integrates with TaskClassifier, SmartRoutingStrategy, and CostTracker.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.gateway.models import (
    ModelRecommendation,
    UsageRecord,
)
from src.gateway.task_classifier import TaskClassifier

if TYPE_CHECKING:
    from src.gateway.cost_tracker import CostTracker
    from src.gateway.model_leaderboard import ModelLeaderboard
    from src.gateway.model_registry import ModelRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt complexity tiers
# ---------------------------------------------------------------------------

class ComplexityTier:
    TRIVIAL = "trivial"
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"
    EXPERT = "expert"


# Map complexity → minimum model tier needed
COMPLEXITY_MODEL_MAP = {
    ComplexityTier.TRIVIAL: 1,    # nova-micro, nova-lite
    ComplexityTier.SIMPLE: 3,     # haiku, gpt-4o-mini
    ComplexityTier.MODERATE: 5,   # sonnet, gpt-4o
    ComplexityTier.COMPLEX: 6,    # deepseek-r1, o4-mini
    ComplexityTier.EXPERT: 7,     # opus
}

# Model name → tier mapping
MODEL_TIER_MAP = {
    "nova-micro": 1,
    "nova-lite": 2,
    "claude-haiku": 3,
    "gpt-4o-mini": 3,
    "nova-pro": 4,
    "claude-sonnet": 5,
    "gpt-4o": 5,
    "deepseek-r1": 6,
    "o4-mini": 6,
    "claude-opus": 7,
}

# Approximate cost multipliers relative to cheapest tier
MODEL_COST_MULTIPLIER = {
    1: 1.0,
    2: 2.0,
    3: 10.0,
    4: 15.0,
    5: 50.0,
    6: 70.0,
    7: 250.0,
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PromptAnalysis:
    """Result of analyzing a single prompt for efficiency."""

    prompt_length_tokens: int
    complexity: str
    task_type: str
    task_confidence: float
    recommended_model_tier: int
    actual_model_tier: int | None
    is_overprovisioned: bool
    compression_opportunity: float
    system_prompt_ratio: float
    history_token_count: int
    redundancy_indicators: list[str]


@dataclass
class OutputAnalysis:
    """Result of analyzing output utilization."""

    avg_completion_tokens: float
    max_tokens_set: bool
    estimated_utilization: float
    recommendation: str | None


@dataclass
class UserEfficiencyProfile:
    """Learned efficiency profile for a user based on historical patterns."""

    user_id: str
    dominant_task_type: str
    avg_complexity: str
    typical_model: str
    optimal_model: str
    estimated_monthly_savings: float
    patterns: list[str]
    updated_at: datetime


@dataclass
class SemanticReport:
    """Full semantic efficiency analysis report."""

    prompt_analyses: list[PromptAnalysis]
    output_analysis: OutputAnalysis
    model_recommendations: list[ModelRecommendation]
    user_profile: UserEfficiencyProfile | None
    waste_summary: dict


# ---------------------------------------------------------------------------
# SemanticEfficiencyEngine
# ---------------------------------------------------------------------------

class SemanticEfficiencyEngine:
    """Analyzes token efficiency using semantic understanding of prompts and responses."""

    def __init__(
        self,
        task_classifier: TaskClassifier,
        cost_tracker: CostTracker,
        model_registry: ModelRegistry | None = None,
        leaderboard: ModelLeaderboard | None = None,
    ) -> None:
        self._classifier = task_classifier
        self._cost_tracker = cost_tracker
        self._model_registry = model_registry
        self._leaderboard = leaderboard
        self._user_profiles: dict[str, UserEfficiencyProfile] = {}

    # ------------------------------------------------------------------
    # Prompt analysis
    # ------------------------------------------------------------------

    def analyze_prompt(
        self,
        messages: list[dict],
        model: str | None = None,
    ) -> PromptAnalysis:
        """Analyze a prompt for efficiency before it reaches the provider."""
        # Extract components
        system_tokens = 0
        history_tokens = 0
        user_tokens = 0

        for msg in messages:
            content = msg.get("content", "")
            token_estimate = max(1, len(content) // 4)
            role = msg.get("role", "user")
            if role == "system":
                system_tokens += token_estimate
            elif role == "assistant":
                history_tokens += token_estimate
            else:
                user_tokens += token_estimate

        total_tokens = system_tokens + history_tokens + user_tokens

        # Classify the user's actual question (last user message)
        last_user_msg = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                last_user_msg = msg.get("content", "")
                break

        classification = self._classifier.classify(last_user_msg)

        # Assess complexity
        complexity = self._assess_complexity(
            last_user_msg, classification.task_type, total_tokens,
        )

        # Determine model tier needed vs actual
        recommended_tier = COMPLEXITY_MODEL_MAP.get(complexity, 5)
        actual_tier = self._get_model_tier(model) if model else None
        is_overprovisioned = (
            actual_tier is not None and actual_tier > recommended_tier + 1
        )

        # Compression opportunity
        compression = self._estimate_compression(messages, system_tokens, history_tokens, total_tokens)

        # System prompt ratio
        system_ratio = system_tokens / total_tokens if total_tokens > 0 else 0.0

        # Redundancy detection
        redundancy = self._detect_redundancy(messages)

        return PromptAnalysis(
            prompt_length_tokens=total_tokens,
            complexity=complexity,
            task_type=classification.task_type,
            task_confidence=classification.confidence,
            recommended_model_tier=recommended_tier,
            actual_model_tier=actual_tier,
            is_overprovisioned=is_overprovisioned,
            compression_opportunity=round(compression, 2),
            system_prompt_ratio=round(system_ratio, 4),
            history_token_count=history_tokens,
            redundancy_indicators=redundancy,
        )

    # ------------------------------------------------------------------
    # Output utilization analysis
    # ------------------------------------------------------------------

    def analyze_output_utilization(
        self,
        records: list[UsageRecord],
    ) -> OutputAnalysis:
        if not records:
            return OutputAnalysis(
                avg_completion_tokens=0.0,
                max_tokens_set=False,
                estimated_utilization=0.0,
                recommendation=None,
            )

        avg_completion = sum(r.completion_tokens for r in records) / len(records)

        # Detect if responses are consistently very short relative to typical model output
        short_responses = sum(1 for r in records if r.completion_tokens < 50)
        short_ratio = short_responses / len(records)

        # Detect if responses are consistently hitting what looks like a max_tokens cap
        completion_tokens = [r.completion_tokens for r in records]
        if completion_tokens:
            max_comp = max(completion_tokens)
            near_max = sum(1 for ct in completion_tokens if ct > max_comp * 0.95 and ct > 100)
            truncation_ratio = near_max / len(records)
        else:
            truncation_ratio = 0.0

        utilization = 1.0 - short_ratio * 0.5 - truncation_ratio * 0.3

        recommendation = None
        if short_ratio > 0.7:
            recommendation = (
                f"{short_ratio:.0%} of responses are under 50 tokens. "
                f"Set max_tokens to limit output and reduce costs, or use a cheaper model."
            )
        elif truncation_ratio > 0.3:
            recommendation = (
                f"{truncation_ratio:.0%} of responses appear truncated. "
                f"Increase max_tokens or restructure prompts to get complete answers."
            )

        return OutputAnalysis(
            avg_completion_tokens=round(avg_completion, 2),
            max_tokens_set=False,
            estimated_utilization=round(max(0.0, min(1.0, utilization)), 4),
            recommendation=recommendation,
        )

    # ------------------------------------------------------------------
    # User profile learning
    # ------------------------------------------------------------------

    @staticmethod
    def _profile_key(user_id: str, tenant_id: str | None) -> str:
        if tenant_id is None:
            return user_id
        return f"tenant:{len(tenant_id)}:{tenant_id}:user:{len(user_id)}:{user_id}"

    def build_user_profile(
        self,
        user_id: str,
        tenant_id: str | None = None,
    ) -> UserEfficiencyProfile:
        records = [
            r
            for r in self._cost_tracker._records
            if r.user_id == user_id
            and (tenant_id is None or r.tenant_id == tenant_id)
        ]
        profile_key = self._profile_key(user_id, tenant_id)
        if not records:
            profile = UserEfficiencyProfile(
                user_id=user_id,
                dominant_task_type="unknown",
                avg_complexity=ComplexityTier.MODERATE,
                typical_model="unknown",
                optimal_model="unknown",
                estimated_monthly_savings=0.0,
                patterns=[],
                updated_at=datetime.now(timezone.utc),
            )
            self._user_profiles[profile_key] = profile
            return profile

        # Find dominant model
        model_counts: dict[str, int] = defaultdict(int)
        for r in records:
            model_counts[r.model] += 1
        typical_model = max(model_counts, key=model_counts.get)

        # Estimate complexity from average prompt size
        avg_prompt = sum(r.prompt_tokens for r in records) / len(records)
        avg_complexity = self._tokens_to_complexity(avg_prompt)

        # Determine optimal model tier
        optimal_tier = COMPLEXITY_MODEL_MAP.get(avg_complexity, 5)
        optimal_model = self._tier_to_model_name(optimal_tier)

        # Estimate savings
        actual_tier = self._get_model_tier(typical_model) or 5
        if actual_tier > optimal_tier:
            actual_cost_mult = MODEL_COST_MULTIPLIER.get(actual_tier, 50.0)
            optimal_cost_mult = MODEL_COST_MULTIPLIER.get(optimal_tier, 10.0)
            total_cost = sum(r.cost for r in records)
            savings_ratio = 1.0 - (optimal_cost_mult / actual_cost_mult)
            estimated_savings = total_cost * savings_ratio
        else:
            estimated_savings = 0.0

        # Detect patterns
        patterns = self._detect_user_patterns(records)

        profile = UserEfficiencyProfile(
            user_id=user_id,
            dominant_task_type=self._dominant_task_type(records),
            avg_complexity=avg_complexity,
            typical_model=typical_model,
            optimal_model=optimal_model,
            estimated_monthly_savings=round(estimated_savings, 2),
            patterns=patterns,
            updated_at=datetime.now(timezone.utc),
        )
        self._user_profiles[profile_key] = profile
        return profile

    def get_user_profile(
        self,
        user_id: str,
        tenant_id: str | None = None,
    ) -> UserEfficiencyProfile | None:
        return self._user_profiles.get(
            self._profile_key(user_id, tenant_id)
        )

    # ------------------------------------------------------------------
    # Full semantic report
    # ------------------------------------------------------------------

    def generate_report(
        self,
        user_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
    ) -> SemanticReport:
        records = self._cost_tracker._records
        if tenant_id is not None:
            records = [r for r in records if r.tenant_id == tenant_id]
        if user_id:
            records = [r for r in records if r.user_id == user_id]
        if project_id:
            records = [r for r in records if r.project_id == project_id]

        output_analysis = self.analyze_output_utilization(records)

        # Generate model recommendations from usage patterns
        recommendations = self._generate_semantic_recommendations(records)

        # Build user profile if user_id specified
        profile = None
        if user_id:
            profile = self.build_user_profile(
                user_id,
                tenant_id=tenant_id,
            )

        # Waste summary
        waste_summary = self._compute_waste_summary(records)

        return SemanticReport(
            prompt_analyses=[],
            output_analysis=output_analysis,
            model_recommendations=recommendations,
            user_profile=profile,
            waste_summary=waste_summary,
        )

    # ------------------------------------------------------------------
    # Internal — complexity assessment
    # ------------------------------------------------------------------

    def _assess_complexity(
        self,
        user_message: str,
        task_type: str,
        total_prompt_tokens: int,
    ) -> str:
        score = 0.0

        # Length-based scoring
        msg_len = len(user_message)
        if msg_len < 50:
            score += 1.0
        elif msg_len < 200:
            score += 2.0
        elif msg_len < 500:
            score += 3.0
        elif msg_len < 2000:
            score += 4.0
        else:
            score += 5.0

        # Task type scoring
        task_complexity = {
            "summarization": 2.0,
            "general": 2.5,
            "creative_writing": 3.0,
            "coding": 4.0,
            "reasoning": 4.5,
            "math": 4.5,
        }
        score += task_complexity.get(task_type, 3.0)

        # Structural complexity indicators
        if "step by step" in user_message.lower() or "step-by-step" in user_message.lower():
            score += 1.0
        if "compare" in user_message.lower() and "contrast" in user_message.lower():
            score += 1.0
        if user_message.count("?") > 3:
            score += 1.0
        if "```" in user_message:
            score += 1.5

        # Normalize to tier
        if score <= 3.0:
            return ComplexityTier.TRIVIAL
        if score <= 5.0:
            return ComplexityTier.SIMPLE
        if score <= 7.0:
            return ComplexityTier.MODERATE
        if score <= 9.0:
            return ComplexityTier.COMPLEX
        return ComplexityTier.EXPERT

    def _tokens_to_complexity(self, avg_prompt_tokens: float) -> str:
        if avg_prompt_tokens < 100:
            return ComplexityTier.TRIVIAL
        if avg_prompt_tokens < 500:
            return ComplexityTier.SIMPLE
        if avg_prompt_tokens < 1500:
            return ComplexityTier.MODERATE
        if avg_prompt_tokens < 4000:
            return ComplexityTier.COMPLEX
        return ComplexityTier.EXPERT

    # ------------------------------------------------------------------
    # Internal — compression estimation
    # ------------------------------------------------------------------

    def _estimate_compression(
        self,
        messages: list[dict],
        system_tokens: int,
        history_tokens: int,
        total_tokens: int,
    ) -> float:
        if total_tokens == 0:
            return 0.0

        savings = 0.0

        # System prompt >30% of total → likely compressible
        if system_tokens > 0 and system_tokens / total_tokens > 0.3:
            savings += (system_tokens / total_tokens - 0.3) * 0.5

        # Long conversation history → could truncate older turns
        assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
        if len(assistant_msgs) > 5:
            excess_ratio = (len(assistant_msgs) - 5) / len(assistant_msgs)
            savings += excess_ratio * 0.3

        # History dominates the prompt
        if history_tokens > 0 and history_tokens / total_tokens > 0.6:
            savings += 0.1

        return min(0.7, savings)

    # ------------------------------------------------------------------
    # Internal — redundancy detection
    # ------------------------------------------------------------------

    def _detect_redundancy(self, messages: list[dict]) -> list[str]:
        indicators: list[str] = []

        # Check for repeated system prompts in multi-turn
        system_msgs = [m.get("content", "") for m in messages if m.get("role") == "system"]
        if len(system_msgs) > 1:
            indicators.append("multiple_system_prompts")

        # Check for very similar consecutive user messages
        user_msgs = [m.get("content", "") for m in messages if m.get("role") == "user"]
        for i in range(1, len(user_msgs)):
            if user_msgs[i] == user_msgs[i - 1]:
                indicators.append("duplicate_user_message")
                break
            overlap = self._string_overlap(user_msgs[i - 1], user_msgs[i])
            if overlap > 0.8:
                indicators.append("near_duplicate_user_message")
                break

        # Check if conversation history is excessively long
        if len(messages) > 20:
            indicators.append("excessive_conversation_length")

        return indicators

    def _string_overlap(self, a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        longer = max(len(a), len(b))
        if longer == 0:
            return 0.0
        common = sum(1 for ca, cb in zip(a, b) if ca == cb)
        return common / longer

    # ------------------------------------------------------------------
    # Internal — user pattern detection
    # ------------------------------------------------------------------

    def _dominant_task_type(self, records: list[UsageRecord]) -> str:
        """The most common classified task type across ``records``.

        Records with an empty ``task_type`` are skipped rather than bucketed:
        "" means the request was never classified (written before the field
        existed, or routed by a path that does not classify), and counting it
        as a task type would be inventing a result. When nothing is classified
        the answer is ``"unknown"`` — the same value the no-records path
        returns, so callers cannot tell "no data" from "no classification",
        which is correct: in both cases there is nothing to report.

        Ties go to the first task type seen, matching how ``typical_model`` is
        picked in :meth:`build_user_profile`.
        """
        counts: dict[str, int] = defaultdict(int)
        for r in records:
            task_type = getattr(r, "task_type", "")
            if task_type:
                counts[task_type] += 1
        if not counts:
            return "unknown"
        return max(counts, key=lambda t: counts[t])

    def _detect_user_patterns(self, records: list[UsageRecord]) -> list[str]:
        patterns: list[str] = []

        if not records:
            return patterns

        # Always uses the most expensive model
        model_costs: dict[str, int] = defaultdict(int)
        for r in records:
            tier = self._get_model_tier(r.model) or 0
            model_costs[r.model] = max(model_costs[r.model], tier)

        if model_costs:
            most_used = max(model_costs, key=lambda m: sum(1 for r in records if r.model == m))
            most_used_tier = self._get_model_tier(most_used) or 0
            if most_used_tier >= 7:
                patterns.append("always_uses_most_expensive_model")
            elif most_used_tier >= 6:
                patterns.append("defaults_to_expensive_models")

        # Consistently short responses from expensive models
        expensive_records = [
            r for r in records
            if (self._get_model_tier(r.model) or 0) >= 6
        ]
        if expensive_records:
            avg_comp = sum(r.completion_tokens for r in expensive_records) / len(expensive_records)
            if avg_comp < 100:
                patterns.append("short_responses_from_expensive_models")

        # High prompt-to-completion ratio (bloated prompts)
        total_prompt = sum(r.prompt_tokens for r in records)
        total_comp = sum(r.completion_tokens for r in records)
        if total_prompt > 0 and total_comp / total_prompt < 0.05:
            patterns.append("extremely_low_output_ratio")

        # No cache usage
        total_cached = sum(r.cached_tokens for r in records)
        if total_cached == 0 and len(records) > 10:
            patterns.append("zero_cache_utilization")

        return patterns

    # ------------------------------------------------------------------
    # Internal — semantic recommendations
    # ------------------------------------------------------------------

    def _generate_semantic_recommendations(
        self,
        records: list[UsageRecord],
    ) -> list[ModelRecommendation]:
        recommendations: list[ModelRecommendation] = []

        model_groups: dict[str, list[UsageRecord]] = defaultdict(list)
        for r in records:
            model_groups[r.model].append(r)

        for model, recs in model_groups.items():
            actual_tier = self._get_model_tier(model) or 5
            if actual_tier < 5:
                continue

            avg_prompt = sum(r.prompt_tokens for r in recs) / len(recs)
            complexity = self._tokens_to_complexity(avg_prompt)
            needed_tier = COMPLEXITY_MODEL_MAP.get(complexity, 5)

            if actual_tier > needed_tier + 1:
                target_model = self._tier_to_model_name(needed_tier)
                actual_mult = MODEL_COST_MULTIPLIER.get(actual_tier, 50.0)
                needed_mult = MODEL_COST_MULTIPLIER.get(needed_tier, 10.0)
                savings = (1.0 - needed_mult / actual_mult) * 100

                quality_impact = "minimal" if actual_tier - needed_tier <= 2 else "moderate"

                recommendations.append(ModelRecommendation(
                    current_model=model,
                    recommended_model=target_model,
                    task_type=complexity,
                    estimated_savings_pct=round(savings, 1),
                    quality_impact=quality_impact,
                    reason=(
                        f"Prompt complexity is '{complexity}' (avg {avg_prompt:.0f} tokens) "
                        f"which only requires tier-{needed_tier} models. "
                        f"Currently using tier-{actual_tier} ({model})."
                    ),
                ))

        return recommendations

    # ------------------------------------------------------------------
    # Internal — waste summary
    # ------------------------------------------------------------------

    def _compute_waste_summary(self, records: list[UsageRecord]) -> dict:
        if not records:
            return {
                "total_cost": 0.0,
                "estimated_wasted_cost": 0.0,
                "waste_pct": 0.0,
                "waste_categories": {},
            }

        total_cost = sum(r.cost for r in records)
        waste = 0.0
        categories: dict[str, float] = {}

        # Overprovisioned model waste
        for r in records:
            actual_tier = self._get_model_tier(r.model) or 5
            complexity = self._tokens_to_complexity(r.prompt_tokens)
            needed_tier = COMPLEXITY_MODEL_MAP.get(complexity, 5)

            if actual_tier > needed_tier + 1:
                actual_mult = MODEL_COST_MULTIPLIER.get(actual_tier, 50.0)
                needed_mult = MODEL_COST_MULTIPLIER.get(needed_tier, 10.0)
                wasted = r.cost * (1.0 - needed_mult / actual_mult)
                waste += wasted
                categories["model_overprovisioning"] = categories.get("model_overprovisioning", 0.0) + wasted

        waste_pct = (waste / total_cost * 100) if total_cost > 0 else 0.0

        return {
            "total_cost": round(total_cost, 4),
            "estimated_wasted_cost": round(waste, 4),
            "waste_pct": round(waste_pct, 1),
            "waste_categories": {k: round(v, 4) for k, v in categories.items()},
        }

    # ------------------------------------------------------------------
    # Internal — model helpers
    # ------------------------------------------------------------------

    def _get_model_tier(self, model_name: str) -> int | None:
        for name, tier in MODEL_TIER_MAP.items():
            if name in model_name or model_name in name:
                return tier
        return None

    def _tier_to_model_name(self, tier: int) -> str:
        for name, t in MODEL_TIER_MAP.items():
            if t == tier:
                return name
        return "claude-sonnet"
