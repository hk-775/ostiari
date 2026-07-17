"""Behavior-derived trust scoring.

Cross-agent delegation uses per-agent trust scores that today are static and
human-configured. This module derives a 0-100 score from how an agent has
*actually* behaved — its recent tool-call traces — so trust reflects reality.

Design principles:
  - Shadow-first: this only *computes* scores. Whether they're enforced is an
    opt-in decision made elsewhere; here we just report configured vs derived.
  - Explainable: the score is a simple, auditable formula, not a black box.
  - Stable: low-volume agents stay near the neutral baseline (we don't tank an
    agent's trust off one or two events).
"""

from __future__ import annotations

from typing import Any

BASELINE = 50          # neutral score for an agent with no history
FULL_TRUST = 100
# Minimum traces before behavior meaningfully moves the score away from
# baseline (avoids overreacting to tiny samples).
CONFIDENCE_FLOOR = 5

# Penalty weights (points subtracted from a clean 100 at full confidence).
_W_BLOCK = 60          # fraction of actions blocked
_W_INTERVENE = 25      # fraction needing human review
_W_RISK = 30           # average risk score (0-1 normalized)


def derive_score(traces: list[dict[str, Any]]) -> int:
    """Compute a 0-100 trust score from an agent's recent traces.

    Fewer than CONFIDENCE_FLOOR traces blends toward the neutral baseline so a
    thin sample can't swing trust to an extreme.
    """
    n = len(traces)
    if n == 0:
        return BASELINE

    blocks = sum(1 for t in traces if t.get("tier") == "block")
    intervenes = sum(1 for t in traces if t.get("tier") == "intervene")
    risk_vals = [t.get("score") or 0 for t in traces if isinstance(t.get("score"), int)]
    avg_risk = (sum(risk_vals) / len(risk_vals) / 100.0) if risk_vals else 0.0

    block_rate = blocks / n
    intervene_rate = intervenes / n

    raw = FULL_TRUST - (
        _W_BLOCK * block_rate
        + _W_INTERVENE * intervene_rate
        + _W_RISK * avg_risk
    )
    raw = max(0.0, min(100.0, raw))

    # Confidence blend: with < CONFIDENCE_FLOOR traces, pull toward baseline.
    confidence = min(1.0, n / CONFIDENCE_FLOOR)
    blended = confidence * raw + (1 - confidence) * BASELINE
    return round(blended)


def score_agents(
    traces: list[dict[str, Any]], configured: dict[str, int] | None = None
) -> list[dict[str, Any]]:
    """Group traces by agent and return derived vs configured scores + delta.

    ``configured`` is the current static trust_scores map (from the gateway
    policy). Each row shows what enforcement *would* change if derived scores
    were applied — the shadow view.
    """
    configured = configured or {}
    by_agent: dict[str, list[dict[str, Any]]] = {}
    for t in traces:
        aid = t.get("agent_id") or "unknown"
        by_agent.setdefault(aid, []).append(t)

    rows = []
    for aid, ts in by_agent.items():
        derived = derive_score(ts)
        conf = configured.get(aid)
        rows.append({
            "agent_id": aid,
            "derived_score": derived,
            "configured_score": conf,
            "delta": (derived - conf) if conf is not None else None,
            "sample_size": len(ts),
        })
    return sorted(rows, key=lambda r: r["derived_score"])
