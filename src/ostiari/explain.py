"""Decision explainability.

Every allow / intervene / block decision should be answerable: *why?* The
gateway already collects the signals that produced the score (parameter risk,
anomalies, policy adjustments) — this turns them into a structured, ordered
explanation an operator or auditor can read, and a one-line summary.

Used to enrich the sidecar's response and as compliance/audit evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Factor:
    """One contributor to the decision."""

    source: str            # e.g. "parameter-risk", "anomaly:loop", "policy"
    points: int            # signed contribution to the 0-100 score
    description: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class DecisionExplanation:
    action: str
    tier: str
    score: int
    factors: list[Factor] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "tier": self.tier,
            "score": self.score,
            "summary": self.summary,
            "factors": [
                {"source": f.source, "points": f.points,
                 "description": f.description, "detail": f.detail}
                for f in self.factors
            ],
        }


def _summarize(action: str, tier: str, score: int, factors: list[Factor]) -> str:
    if not factors:
        base = f"'{action}' scored {score} → {tier}"
        return base + " (no risk factors — baseline)."
    top = factors[0]
    verb = {"allow": "allowed", "intervene": "flagged for human approval",
            "block": "blocked"}.get(tier, tier)
    lead = f"'{action}' {verb} (score {score})."
    if tier == "allow":
        return lead + f" Minor factors, chiefly: {top.description}."
    return lead + f" Primary driver: {top.description}."


def explain(result: Any) -> DecisionExplanation:
    """Build a DecisionExplanation from a ValidationResult (or duck-typed result).

    Orders factors by absolute contribution (biggest driver first) so the
    summary and UI lead with what mattered most.
    """
    action = getattr(result, "action", "")
    # `or` already falls through a missing/empty original_tier, but a duck-typed
    # result can carry tier=None explicitly, which would reach DecisionExplanation
    # as None where a str is declared. Coerce to the same "allow" default.
    tier = str(getattr(result, "original_tier", None)
               or getattr(result, "tier", None) or "allow")
    score = int(getattr(result, "score", 0) or 0)

    factors: list[Factor] = []
    for s in getattr(result, "signals", []) or []:
        factors.append(Factor(
            source=getattr(s, "source", "unknown"),
            points=int(getattr(s, "score_contribution", 0) or 0),
            description=getattr(s, "description", ""),
            detail=dict(getattr(s, "metadata", {}) or {}),
        ))
    factors.sort(key=lambda f: abs(f.points), reverse=True)

    return DecisionExplanation(
        action=action, tier=tier, score=score, factors=factors,
        summary=_summarize(action, tier, score, factors),
    )
