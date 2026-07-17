"""Compliance report generation.

Transforms the governance evidence Ostiari already collects — audit logs,
tool-call traces (risk tier/score, blocks), and configured policies — into a
regulator-shaped report. Frameworks are pluggable; EU AI Act ships first, with
each requirement mapped to concrete Ostiari evidence.

A requirement is scored:
  - "met"        (green)  — evidence present and healthy
  - "partial"    (yellow) — some evidence, but a gap worth noting
  - "unmet"      (red)    — no evidence / control not in place
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Evidence:
    """Aggregated signals extracted from Ostiari data for a reporting period."""

    audit_count: int = 0
    trace_count: int = 0
    blocked_count: int = 0
    intervene_count: int = 0  # human-oversight (HITL) interventions
    policy_count: int = 0
    scored_trace_count: int = 0  # traces that carry a risk score
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Requirement:
    id: str
    title: str
    status: str            # met | partial | unmet
    detail: str
    evidence_refs: list[str] = field(default_factory=list)


# ─── Framework definitions ───────────────────────────────────────────────────
# Each framework is a function taking Evidence and returning a list[Requirement].

def _eu_ai_act(ev: Evidence) -> list[Requirement]:
    reqs: list[Requirement] = []

    # Article 9 — Risk management system
    if ev.policy_count > 0 and ev.scored_trace_count > 0:
        status, detail = "met", (
            f"{ev.policy_count} active policies; {ev.scored_trace_count} risk-scored "
            f"tool calls; {ev.blocked_count} high-risk actions blocked."
        )
    elif ev.policy_count > 0 or ev.scored_trace_count > 0:
        status, detail = "partial", (
            "Risk controls partially in place — either policies or risk scoring "
            "is present, but not both continuously exercised."
        )
    else:
        status, detail = "unmet", "No active policies or risk-scored activity found."
    reqs.append(Requirement(
        "art-9", "Article 9 — Risk management system", status, detail,
        ["policies", "traces.score", "traces.tier=block"],
    ))

    # Article 12 — Record-keeping (logging)
    if ev.audit_count > 0 and ev.trace_count > 0:
        status, detail = "met", (
            f"{ev.audit_count} audit-log entries and {ev.trace_count} tool-call "
            f"traces retained with timestamps."
        )
    elif ev.audit_count > 0 or ev.trace_count > 0:
        status, detail = "partial", "Some records retained, but not both audit and trace streams."
    else:
        status, detail = "unmet", "No audit logs or traces retained for the period."
    reqs.append(Requirement(
        "art-12", "Article 12 — Record-keeping", status, detail,
        ["audit_logs", "traces"],
    ))

    # Article 14 — Human oversight
    if ev.intervene_count > 0:
        status, detail = "met", (
            f"{ev.intervene_count} actions routed to human oversight (intervene tier)."
        )
    elif ev.blocked_count > 0:
        status, detail = "partial", (
            "Automated blocking is active, but no human-in-the-loop interventions "
            "were recorded — confirm an oversight path exists for borderline actions."
        )
    else:
        status, detail = "unmet", "No human-oversight (intervene) mechanism exercised."
    reqs.append(Requirement(
        "art-14", "Article 14 — Human oversight", status, detail,
        ["traces.tier=intervene"],
    ))

    return reqs


FRAMEWORKS: dict[str, Callable[[Evidence], list[Requirement]]] = {
    "eu-ai-act": _eu_ai_act,
}


def list_frameworks() -> list[str]:
    return sorted(FRAMEWORKS)


def build_evidence(
    audit_logs: list[Any], traces: list[dict[str, Any]], policy_count: int
) -> Evidence:
    """Aggregate raw control-plane data into an Evidence summary."""
    blocked = sum(1 for t in traces if t.get("tier") == "block")
    intervene = sum(1 for t in traces if t.get("tier") == "intervene")
    scored = sum(1 for t in traces if isinstance(t.get("score"), int) and t.get("score") is not None)
    return Evidence(
        audit_count=len(audit_logs),
        trace_count=len(traces),
        blocked_count=blocked,
        intervene_count=intervene,
        policy_count=policy_count,
        scored_trace_count=scored,
    )


def generate_report(framework: str, evidence: Evidence) -> dict[str, Any]:
    """Produce a structured compliance report for a framework."""
    fn = FRAMEWORKS.get(framework)
    if fn is None:
        raise ValueError(f"Unknown framework: {framework}")

    reqs = fn(evidence)
    counts = {"met": 0, "partial": 0, "unmet": 0}
    for r in reqs:
        counts[r.status] = counts.get(r.status, 0) + 1

    total = len(reqs) or 1
    # Posture: green only if all met, red if any unmet, else yellow.
    if counts["unmet"] > 0:
        posture = "red"
    elif counts["partial"] > 0:
        posture = "yellow"
    else:
        posture = "green"

    return {
        "framework": framework,
        "posture": posture,
        "score_pct": round(100 * counts["met"] / total, 1),
        "summary": counts,
        "evidence": {
            "audit_count": evidence.audit_count,
            "trace_count": evidence.trace_count,
            "blocked_count": evidence.blocked_count,
            "intervene_count": evidence.intervene_count,
            "policy_count": evidence.policy_count,
        },
        "requirements": [
            {
                "id": r.id, "title": r.title, "status": r.status,
                "detail": r.detail, "evidence_refs": r.evidence_refs,
            }
            for r in reqs
        ],
    }
