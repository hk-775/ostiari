"""ROI / savings calculation.

Turns the blocks Ostiari already records into a board-ready damage-prevented
estimate: "we blocked N unsafe actions worth ~$X." Each blocked action is
assigned an estimated incident cost by matching its action name against a
configurable cost model (fnmatch patterns, most-specific first), scaled by the
risk score so a barely-over-threshold block is worth less than a max-risk one.

This is intentionally an *estimate* — the cost assumptions are inputs the CIO
sets. The point is a defensible, transparent monthly ROI figure, not precision.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# Default incident-cost model (USD) per blocked action, fnmatch patterns.
# Order matters: the first matching pattern wins, so list specific before broad.
DEFAULT_COST_MODEL: list[tuple[str, float]] = [
    ("*.delete_repo", 250_000.0),
    ("db_delete", 500_000.0),
    ("*drop*", 500_000.0),
    ("*delete*", 120_000.0),
    ("deploy", 200_000.0),
    ("send_email", 5_000.0),
    ("*.post", 8_000.0),
    ("execute_code", 75_000.0),
    ("transfer*", 300_000.0),
    ("a2a.*", 40_000.0),
]
DEFAULT_FALLBACK_COST = 10_000.0


@dataclass
class ActionSaving:
    action: str
    count: int = 0
    unit_cost: float = 0.0          # estimated incident cost for this action type
    prevented_usd: float = 0.0      # risk-weighted sum across occurrences
    max_score: int = 0


@dataclass
class RoiReport:
    blocked_count: int = 0
    distinct_actions: int = 0
    total_prevented_usd: float = 0.0
    fallback_cost: float = DEFAULT_FALLBACK_COST
    actions: list[ActionSaving] = field(default_factory=list)


def _cost_for(action: str, model: list[tuple[str, float]], fallback: float) -> float:
    for pattern, cost in model:
        if fnmatch.fnmatch(action, pattern):
            return cost
    return fallback


def compute_roi(
    traces: Iterable[dict[str, Any]],
    *,
    cost_model: list[tuple[str, float]] | None = None,
    fallback_cost: float = DEFAULT_FALLBACK_COST,
    weight_by_score: bool = True,
) -> RoiReport:
    """Estimate damage prevented from blocked (and would-block) trace events.

    A trace counts as prevented if tier == "block" or it carries would_block
    (shadow mode's "would have blocked"). Each occurrence contributes
    unit_cost * (score/100) when weighting by score, else unit_cost.
    """
    model = cost_model if cost_model is not None else DEFAULT_COST_MODEL
    by_action: dict[str, ActionSaving] = {}

    for t in traces:
        blocked = t.get("tier") == "block" or t.get("would_block")
        if not blocked:
            continue
        action = t.get("action", "unknown")
        score = int(t.get("score") or 0)
        unit = _cost_for(action, model, fallback_cost)
        weight = (score / 100.0) if (weight_by_score and score > 0) else 1.0

        entry = by_action.get(action)
        if entry is None:
            entry = ActionSaving(action=action, unit_cost=unit)
            by_action[action] = entry
        entry.count += 1
        entry.prevented_usd += unit * weight
        entry.max_score = max(entry.max_score, score)

    actions = sorted(by_action.values(), key=lambda a: a.prevented_usd, reverse=True)
    for a in actions:
        a.prevented_usd = round(a.prevented_usd, 2)

    return RoiReport(
        blocked_count=sum(a.count for a in actions),
        distinct_actions=len(actions),
        total_prevented_usd=round(sum(a.prevented_usd for a in actions), 2),
        fallback_cost=fallback_cost,
        actions=actions,
    )
