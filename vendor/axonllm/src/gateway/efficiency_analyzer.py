"""Level 1 token efficiency analyzer — ratio-based heuristics on existing UsageRecord data.

Computes per-user and per-project efficiency metrics, detects waste patterns,
raises alerts, and compares users against their project peers.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.gateway.models import (
    EfficiencyAlert,
    EfficiencyGrade,
    EfficiencyMetrics,
    EfficiencyReport,
    ModelRecommendation,
    UsageRecord,
)

if TYPE_CHECKING:
    from src.gateway.cost_tracker import CostTracker

logger = logging.getLogger(__name__)


def _as_aware(ts: datetime) -> datetime:
    """Coerce a timestamp to timezone-aware UTC.

    UsageRecords reach the analyzer from mixed sources — some write naive
    timestamps (datetime.utcnow()), others tz-aware ones (datetime.now(timezone.utc),
    or datetime.fromisoformat on persisted ISO strings). Comparing a naive and an
    aware datetime raises TypeError, so normalize before any sort/subtraction.
    """
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


# ---------------------------------------------------------------------------
# Thresholds (configurable defaults)
# ---------------------------------------------------------------------------

DEFAULT_THRESHOLDS = {
    "low_completion_prompt_ratio": 0.05,
    "low_cache_utilization": 0.10,
    "high_expensive_model_ratio": 0.80,
    "high_duplicate_rate": 0.15,
    "high_token_velocity": 50_000.0,
    "high_avg_prompt_tokens": 4000,
    "peer_deviation_factor": 2.0,
}

# Models considered "expensive" — Opus-class pricing
EXPENSIVE_MODELS = {
    "claude-opus",
    "claude-opus-4-20250514",
    "us.anthropic.claude-opus-4-6-v1",
    "gpt-4",
    "gpt-4-turbo",
}

# Model tiers for right-sizing recommendations (cheapest first)
MODEL_TIERS = [
    {"name": "nova-micro", "tier": 1, "capabilities": ["chat"]},
    {"name": "nova-lite", "tier": 2, "capabilities": ["chat"]},
    {"name": "claude-haiku", "tier": 3, "capabilities": ["chat", "streaming"]},
    {"name": "gpt-4o-mini", "tier": 3, "capabilities": ["chat", "streaming"]},
    {"name": "nova-pro", "tier": 4, "capabilities": ["chat", "vision"]},
    {"name": "claude-sonnet", "tier": 5, "capabilities": ["chat", "vision", "streaming"]},
    {"name": "gpt-4o", "tier": 5, "capabilities": ["chat", "vision", "streaming"]},
    {"name": "deepseek-r1", "tier": 6, "capabilities": ["chat", "reasoning"]},
    {"name": "o4-mini", "tier": 6, "capabilities": ["chat", "reasoning"]},
    {"name": "claude-opus", "tier": 7, "capabilities": ["chat", "vision", "streaming"]},
]


# ---------------------------------------------------------------------------
# EfficiencyAnalyzer
# ---------------------------------------------------------------------------


class EfficiencyAnalyzer:
    """Computes token efficiency metrics from UsageRecord data.

    Operates entirely on the existing records in CostTracker — no new data
    collection required.
    """

    def __init__(
        self,
        cost_tracker: CostTracker,
        thresholds: dict | None = None,
    ) -> None:
        self._cost_tracker = cost_tracker
        self._thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze_user(
        self,
        user_id: str,
        tenant_id: str | None = None,
    ) -> EfficiencyReport:
        records = [
            r
            for r in self._records_for_tenant(tenant_id)
            if r.user_id == user_id
        ]
        if not records:
            return self._empty_report(user_id, "user")

        metrics = self._compute_metrics(records, user_id, "user")
        alerts = self._generate_alerts(metrics, records)
        recommendations = self._generate_recommendations(records)
        peer_comparison = self._compute_peer_comparison(
            user_id,
            records,
            tenant_id=tenant_id,
        )

        return EfficiencyReport(
            metrics=metrics,
            alerts=alerts,
            recommendations=recommendations,
            peer_comparison=peer_comparison,
        )

    def analyze_project(
        self,
        project_id: str,
        tenant_id: str | None = None,
    ) -> EfficiencyReport:
        records = [
            r
            for r in self._records_for_tenant(tenant_id)
            if r.project_id == project_id
        ]
        if not records:
            return self._empty_report(project_id, "project")

        metrics = self._compute_metrics(records, project_id, "project")
        alerts = self._generate_alerts(metrics, records)
        recommendations = self._generate_recommendations(records)
        peer_comparison = self._compute_project_user_comparison(project_id, records)

        return EfficiencyReport(
            metrics=metrics,
            alerts=alerts,
            recommendations=recommendations,
            peer_comparison=peer_comparison,
        )

    def get_all_user_metrics(
        self,
        tenant_id: str | None = None,
    ) -> list[EfficiencyMetrics]:
        users: dict[str, list[UsageRecord]] = defaultdict(list)
        for r in self._records_for_tenant(tenant_id):
            users[r.user_id].append(r)
        return [self._compute_metrics(recs, uid, "user") for uid, recs in users.items()]

    def _records_for_tenant(
        self,
        tenant_id: str | None,
    ) -> list[UsageRecord]:
        records = self._cost_tracker._records
        if tenant_id is None:
            return records
        return [record for record in records if record.tenant_id == tenant_id]

    # ------------------------------------------------------------------
    # Core metrics computation
    # ------------------------------------------------------------------

    def _compute_metrics(
        self,
        records: list[UsageRecord],
        entity_id: str,
        entity_type: str,
    ) -> EfficiencyMetrics:
        total_requests = len(records)
        if total_requests == 0:
            return self._empty_metrics(entity_id, entity_type)

        total_prompt = sum(r.prompt_tokens for r in records)
        total_completion = sum(r.completion_tokens for r in records)
        total_cached = sum(r.cached_tokens for r in records)
        total_cost = sum(r.cost for r in records)

        # Completion/Prompt ratio
        completion_prompt_ratio = (
            total_completion / total_prompt if total_prompt > 0 else 0.0
        )

        # Cache utilization rate
        cache_utilization = (
            total_cached / total_prompt if total_prompt > 0 else 0.0
        )

        # Average cost per request
        avg_cost = total_cost / total_requests

        # Expensive model ratio
        expensive_count = sum(
            1 for r in records if r.model in EXPENSIVE_MODELS
        )
        expensive_model_ratio = expensive_count / total_requests

        # Token velocity (tokens per hour)
        token_velocity = self._compute_token_velocity(records)

        # Duplicate request rate
        duplicate_rate = self._compute_duplicate_rate(records)

        avg_prompt_tokens = total_prompt / total_requests
        avg_completion_tokens = total_completion / total_requests

        # Compute overall score and grade
        score = self._compute_efficiency_score(
            completion_prompt_ratio,
            cache_utilization,
            expensive_model_ratio,
            duplicate_rate,
            avg_prompt_tokens,
        )
        grade = self._score_to_grade(score)

        return EfficiencyMetrics(
            entity_id=entity_id,
            entity_type=entity_type,
            completion_prompt_ratio=round(completion_prompt_ratio, 4),
            cache_utilization_rate=round(cache_utilization, 4),
            avg_cost_per_request=round(avg_cost, 6),
            expensive_model_ratio=round(expensive_model_ratio, 4),
            token_velocity_per_hour=round(token_velocity, 2),
            duplicate_request_rate=round(duplicate_rate, 4),
            avg_prompt_tokens=round(avg_prompt_tokens, 2),
            avg_completion_tokens=round(avg_completion_tokens, 2),
            total_requests=total_requests,
            total_cost=round(total_cost, 6),
            grade=grade,
            score=round(score, 2),
        )

    # ------------------------------------------------------------------
    # Efficiency score (0-100, higher is better)
    # ------------------------------------------------------------------

    def _compute_efficiency_score(
        self,
        completion_prompt_ratio: float,
        cache_utilization: float,
        expensive_model_ratio: float,
        duplicate_rate: float,
        avg_prompt_tokens: float,
    ) -> float:
        score = 100.0

        # Penalize very low completion/prompt ratio (sending huge prompts for tiny answers)
        if completion_prompt_ratio < self._thresholds["low_completion_prompt_ratio"]:
            score -= 25.0
        elif completion_prompt_ratio < 0.1:
            score -= 10.0

        # Reward cache utilization
        if cache_utilization > 0.3:
            score += 5.0
        elif cache_utilization < self._thresholds["low_cache_utilization"]:
            score -= 10.0

        # Penalize overuse of expensive models
        if expensive_model_ratio > self._thresholds["high_expensive_model_ratio"]:
            score -= 20.0
        elif expensive_model_ratio > 0.5:
            score -= 10.0

        # Penalize duplicate requests
        if duplicate_rate > self._thresholds["high_duplicate_rate"]:
            score -= 20.0
        elif duplicate_rate > 0.05:
            score -= 5.0

        # Penalize bloated prompts
        if avg_prompt_tokens > self._thresholds["high_avg_prompt_tokens"]:
            score -= 15.0
        elif avg_prompt_tokens > 2000:
            score -= 5.0

        return max(0.0, min(100.0, score))

    def _score_to_grade(self, score: float) -> EfficiencyGrade:
        if score >= 85:
            return EfficiencyGrade.EXCELLENT
        if score >= 70:
            return EfficiencyGrade.GOOD
        if score >= 50:
            return EfficiencyGrade.FAIR
        if score >= 30:
            return EfficiencyGrade.POOR
        return EfficiencyGrade.WASTEFUL

    # ------------------------------------------------------------------
    # Alert generation
    # ------------------------------------------------------------------

    def _generate_alerts(
        self,
        metrics: EfficiencyMetrics,
        records: list[UsageRecord],
    ) -> list[EfficiencyAlert]:
        alerts: list[EfficiencyAlert] = []
        now = datetime.now(timezone.utc)

        if metrics.completion_prompt_ratio < self._thresholds["low_completion_prompt_ratio"]:
            alerts.append(EfficiencyAlert(
                entity_id=metrics.entity_id,
                entity_type=metrics.entity_type,
                alert_type="low_completion_prompt_ratio",
                severity="warning",
                message=(
                    f"Completion/prompt ratio is {metrics.completion_prompt_ratio:.2%} — "
                    f"large prompts are generating very small responses. "
                    f"Consider reducing prompt size or system prompt length."
                ),
                metric_value=metrics.completion_prompt_ratio,
                threshold=self._thresholds["low_completion_prompt_ratio"],
                timestamp=now,
            ))

        if metrics.cache_utilization_rate < self._thresholds["low_cache_utilization"] and metrics.total_requests >= 5:
            alerts.append(EfficiencyAlert(
                entity_id=metrics.entity_id,
                entity_type=metrics.entity_type,
                alert_type="low_cache_utilization",
                severity="info",
                message=(
                    f"Cache utilization is {metrics.cache_utilization_rate:.2%}. "
                    f"Enable prompt caching to reduce costs on repeated system prompts."
                ),
                metric_value=metrics.cache_utilization_rate,
                threshold=self._thresholds["low_cache_utilization"],
                timestamp=now,
            ))

        if metrics.expensive_model_ratio > self._thresholds["high_expensive_model_ratio"]:
            alerts.append(EfficiencyAlert(
                entity_id=metrics.entity_id,
                entity_type=metrics.entity_type,
                alert_type="high_expensive_model_usage",
                severity="warning",
                message=(
                    f"{metrics.expensive_model_ratio:.0%} of requests use expensive models (Opus/GPT-4). "
                    f"Consider using Sonnet or Haiku for simpler tasks."
                ),
                metric_value=metrics.expensive_model_ratio,
                threshold=self._thresholds["high_expensive_model_ratio"],
                timestamp=now,
            ))

        if metrics.duplicate_request_rate > self._thresholds["high_duplicate_rate"]:
            alerts.append(EfficiencyAlert(
                entity_id=metrics.entity_id,
                entity_type=metrics.entity_type,
                alert_type="high_duplicate_requests",
                severity="critical",
                message=(
                    f"{metrics.duplicate_request_rate:.0%} of requests appear to be duplicates. "
                    f"This may indicate retry loops, copy-paste resubmissions, or missing client-side caching."
                ),
                metric_value=metrics.duplicate_request_rate,
                threshold=self._thresholds["high_duplicate_rate"],
                timestamp=now,
            ))

        if metrics.token_velocity_per_hour > self._thresholds["high_token_velocity"]:
            alerts.append(EfficiencyAlert(
                entity_id=metrics.entity_id,
                entity_type=metrics.entity_type,
                alert_type="high_token_velocity",
                severity="critical",
                message=(
                    f"Token velocity is {metrics.token_velocity_per_hour:,.0f} tokens/hour — "
                    f"possible runaway automation or unthrottled batch job."
                ),
                metric_value=metrics.token_velocity_per_hour,
                threshold=self._thresholds["high_token_velocity"],
                timestamp=now,
            ))

        if metrics.avg_prompt_tokens > self._thresholds["high_avg_prompt_tokens"]:
            alerts.append(EfficiencyAlert(
                entity_id=metrics.entity_id,
                entity_type=metrics.entity_type,
                alert_type="bloated_prompts",
                severity="warning",
                message=(
                    f"Average prompt size is {metrics.avg_prompt_tokens:,.0f} tokens. "
                    f"Consider trimming conversation history or reducing system prompt length."
                ),
                metric_value=metrics.avg_prompt_tokens,
                threshold=self._thresholds["high_avg_prompt_tokens"],
                timestamp=now,
            ))

        return alerts

    # ------------------------------------------------------------------
    # Model right-sizing recommendations
    # ------------------------------------------------------------------

    def _generate_recommendations(
        self,
        records: list[UsageRecord],
    ) -> list[ModelRecommendation]:
        recommendations: list[ModelRecommendation] = []

        model_usage: dict[str, list[UsageRecord]] = defaultdict(list)
        for r in records:
            model_usage[r.model].append(r)

        for model, recs in model_usage.items():
            tier = self._get_model_tier(model)
            if tier is None or tier <= 4:
                continue

            avg_completion = sum(r.completion_tokens for r in recs) / len(recs)
            avg_prompt = sum(r.prompt_tokens for r in recs) / len(recs)

            # Short responses from expensive models → suggest downgrade
            if avg_completion < 200 and tier >= 6:
                cheaper = self._find_cheaper_model(tier, ["chat"])
                if cheaper:
                    savings = self._estimate_savings(model, cheaper, recs)
                    recommendations.append(ModelRecommendation(
                        current_model=model,
                        recommended_model=cheaper,
                        task_type="short_response",
                        estimated_savings_pct=round(savings, 1),
                        quality_impact="minimal",
                        reason=(
                            f"Average response is {avg_completion:.0f} tokens — "
                            f"a cheaper model can handle short answers."
                        ),
                    ))

            # Small prompts + small responses on expensive models
            if avg_prompt < 500 and avg_completion < 500 and tier >= 5:
                cheaper = self._find_cheaper_model(tier, ["chat"])
                if cheaper:
                    savings = self._estimate_savings(model, cheaper, recs)
                    recommendations.append(ModelRecommendation(
                        current_model=model,
                        recommended_model=cheaper,
                        task_type="simple_task",
                        estimated_savings_pct=round(savings, 1),
                        quality_impact="low",
                        reason=(
                            f"Both prompts ({avg_prompt:.0f} tokens) and responses "
                            f"({avg_completion:.0f} tokens) are small — task complexity "
                            f"likely doesn't require {model}."
                        ),
                    ))

        return recommendations

    # ------------------------------------------------------------------
    # Peer comparison
    # ------------------------------------------------------------------

    def _compute_peer_comparison(
        self,
        user_id: str,
        user_records: list[UsageRecord],
        *,
        tenant_id: str | None,
    ) -> dict:
        projects = {r.project_id for r in user_records}
        all_records = self._records_for_tenant(tenant_id)

        peer_records: dict[str, list[UsageRecord]] = defaultdict(list)
        for r in all_records:
            if r.project_id in projects and r.user_id != user_id:
                peer_records[r.user_id].append(r)

        if not peer_records:
            return {"peers_found": 0, "percentile": None, "vs_avg": None}

        peer_avg_costs = [
            sum(recs_item.cost for recs_item in recs) / len(recs)
            for recs in peer_records.values()
        ]
        user_avg_cost = sum(r.cost for r in user_records) / len(user_records)
        peer_mean = sum(peer_avg_costs) / len(peer_avg_costs) if peer_avg_costs else 0.0

        # Percentile: what % of peers have higher avg cost
        cheaper_peers = sum(1 for c in peer_avg_costs if c > user_avg_cost)
        percentile = (cheaper_peers / len(peer_avg_costs) * 100) if peer_avg_costs else 50.0

        vs_avg = ((user_avg_cost - peer_mean) / peer_mean * 100) if peer_mean > 0 else 0.0

        return {
            "peers_found": len(peer_records),
            "percentile": round(percentile, 1),
            "user_avg_cost_per_request": round(user_avg_cost, 6),
            "peer_avg_cost_per_request": round(peer_mean, 6),
            "vs_avg_pct": round(vs_avg, 1),
        }

    def _compute_project_user_comparison(
        self,
        project_id: str,
        records: list[UsageRecord],
    ) -> dict:
        user_records: dict[str, list[UsageRecord]] = defaultdict(list)
        for r in records:
            user_records[r.user_id].append(r)

        user_costs = {}
        for uid, recs in user_records.items():
            user_costs[uid] = {
                "avg_cost_per_request": round(sum(r.cost for r in recs) / len(recs), 6),
                "total_requests": len(recs),
                "total_cost": round(sum(r.cost for r in recs), 6),
            }

        avg_costs = [v["avg_cost_per_request"] for v in user_costs.values()]
        project_mean = sum(avg_costs) / len(avg_costs) if avg_costs else 0.0

        outliers = [
            uid for uid, v in user_costs.items()
            if v["avg_cost_per_request"] > project_mean * self._thresholds["peer_deviation_factor"]
        ]

        return {
            "user_count": len(user_costs),
            "project_avg_cost_per_request": round(project_mean, 6),
            "users": user_costs,
            "outlier_users": outliers,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_token_velocity(self, records: list[UsageRecord]) -> float:
        if len(records) < 2:
            return 0.0
        timestamps = sorted(_as_aware(r.timestamp) for r in records)
        time_span = (timestamps[-1] - timestamps[0]).total_seconds()
        if time_span <= 0:
            return 0.0
        total_tokens = sum(r.total_tokens for r in records)
        hours = time_span / 3600.0
        return total_tokens / hours

    def _compute_duplicate_rate(self, records: list[UsageRecord]) -> float:
        if len(records) < 2:
            return 0.0

        # Group records into 5-minute windows by (model, prompt_tokens, completion_tokens)
        fingerprints: dict[str, list[datetime]] = defaultdict(list)
        for r in records:
            key = f"{r.model}:{r.prompt_tokens}:{r.completion_tokens}"
            fingerprints[key].append(_as_aware(r.timestamp))

        duplicates = 0
        for key, timestamps in fingerprints.items():
            if len(timestamps) < 2:
                continue
            sorted_ts = sorted(timestamps)
            for i in range(1, len(sorted_ts)):
                if (sorted_ts[i] - sorted_ts[i - 1]).total_seconds() < 300:
                    duplicates += 1

        return duplicates / len(records)

    def _get_model_tier(self, model_name: str) -> int | None:
        for entry in MODEL_TIERS:
            if entry["name"] in model_name or model_name in entry["name"]:
                return entry["tier"]
        return None

    def _find_cheaper_model(self, current_tier: int, required_capabilities: list[str]) -> str | None:
        for entry in MODEL_TIERS:
            if entry["tier"] < current_tier:
                if all(cap in entry["capabilities"] for cap in required_capabilities):
                    return entry["name"]
        return None

    def _estimate_savings(
        self,
        current_model: str,
        recommended_model: str,
        records: list[UsageRecord],
    ) -> float:
        current_tier = self._get_model_tier(current_model) or 5
        recommended_tier = self._get_model_tier(recommended_model) or 3
        tier_diff = current_tier - recommended_tier
        return min(90.0, tier_diff * 20.0)

    def _empty_metrics(self, entity_id: str, entity_type: str) -> EfficiencyMetrics:
        return EfficiencyMetrics(
            entity_id=entity_id,
            entity_type=entity_type,
            completion_prompt_ratio=0.0,
            cache_utilization_rate=0.0,
            avg_cost_per_request=0.0,
            expensive_model_ratio=0.0,
            token_velocity_per_hour=0.0,
            duplicate_request_rate=0.0,
            avg_prompt_tokens=0.0,
            avg_completion_tokens=0.0,
            total_requests=0,
            total_cost=0.0,
            grade=EfficiencyGrade.GOOD,
            score=100.0,
        )

    def _empty_report(self, entity_id: str, entity_type: str) -> EfficiencyReport:
        return EfficiencyReport(
            metrics=self._empty_metrics(entity_id, entity_type),
            alerts=[],
            recommendations=[],
            peer_comparison={"peers_found": 0, "percentile": None, "vs_avg": None},
        )
