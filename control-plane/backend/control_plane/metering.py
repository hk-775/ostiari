"""Metering — the billing lens over governed activity.

Cost tracking (routers/costs.py) answers "how much did the LLM spend?".
Metering answers "how many governed tool calls did each team/agent make, and
what tier does that put them in?" — the plumbing every usage-based commercial
model needs. It counts UsageRecord rows (one per governed call) rather than
summing dollars.
"""

from __future__ import annotations

from typing import Any

# Monthly governed-call thresholds per tier. A subject sits in the highest tier
# whose floor it has crossed.
TIERS: list[tuple[str, int]] = [
    ("free", 0),
    ("pro", 50_000),
    ("enterprise", 500_000),
]


def tier_for(calls: int) -> str:
    """Return the tier name for a given governed-call count."""
    name = TIERS[0][0]
    for tier_name, floor in TIERS:
        if calls >= floor:
            name = tier_name
    return name


def next_tier(calls: int) -> dict[str, Any] | None:
    """Return the next tier up and how many calls remain to reach it (or None)."""
    for tier_name, floor in TIERS:
        if calls < floor:
            return {"tier": tier_name, "calls_to_next": floor - calls}
    return None


def summarize(records: list[Any], group_by: str = "agent") -> dict[str, Any]:
    """Aggregate governed-call metering from UsageRecord-like rows.

    group_by: "agent" | "gateway" | "tool" — the dimension to break down by.
    Each row is expected to expose agent_id, gateway_id, action, total_tokens.
    """
    key_attr = {
        "agent": "agent_id",
        "gateway": "gateway_id",
        "tool": "action",
    }.get(group_by, "agent_id")

    groups: dict[str, dict[str, Any]] = {}
    total_calls = 0
    total_tokens = 0
    for r in records:
        total_calls += 1
        total_tokens += getattr(r, "total_tokens", 0) or 0
        key = getattr(r, key_attr, "") or "unknown"
        g = groups.setdefault(key, {"key": key, "calls": 0, "tokens": 0})
        g["calls"] += 1
        g["tokens"] += getattr(r, "total_tokens", 0) or 0

    breakdown = []
    for g in sorted(groups.values(), key=lambda x: x["calls"], reverse=True):
        breakdown.append({
            **g,
            "tier": tier_for(g["calls"]),
            "next_tier": next_tier(g["calls"]),
        })

    return {
        "group_by": group_by,
        "total_governed_calls": total_calls,
        "total_tokens": total_tokens,
        "distinct_subjects": len(groups),
        "overall_tier": tier_for(total_calls),
        "breakdown": breakdown,
    }


def to_csv(summary: dict[str, Any]) -> str:
    """Render a metering summary as CSV (for invoicing/finance export)."""
    lines = [f"{summary['group_by']},governed_calls,tokens,tier"]
    for row in summary["breakdown"]:
        lines.append(f"{row['key']},{row['calls']},{row['tokens']},{row['tier']}")
    return "\n".join(lines) + "\n"
