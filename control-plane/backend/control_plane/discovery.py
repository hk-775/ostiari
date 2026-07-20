"""Agent discovery — reconcile agents SEEN across signal sources against the
agents we KNOW (the registry), and surface the gap (shadow AI).

Design principle (see docs/internal/agent-discovery-plan.md):
  - Discovery reads signals on infrastructure WE control (our own gateway
    traces, AWS CloudTrail, Secrets Manager access, billing, resource
    inventories). It NEVER touches the agent — you can't touch one you don't
    know exists. Changing an agent's endpoint is *onboarding*, not discovery.
  - Every source is a pluggable Collector emitting a common Sighting. The
    reconciliation engine consumes a list of collectors, so adding a source
    (real CloudTrail, etc.) is additive — no engine change.

This module is source-agnostic. Concrete collectors live in
discovery_collectors.py.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Sighting:
    """One observation that an agent exists, from one source.

    agent_id is the best identity the source could provide (may be a real
    X-Agent-Id, an IAM principal, or a heuristic label). confidence reflects how
    sure the source is that this is a distinct, governable agent.
    """

    agent_id: str
    source: str                      # "gateway-traces" | "cloudtrail" | "secrets" | "billing" | "resources"
    evidence: str = ""               # human-readable why-we-think-this
    last_seen: str = ""              # ISO ts if known
    gateways: list[str] = field(default_factory=list)
    call_count: int = 0
    confidence: float = 1.0          # 0..1


class Collector(Protocol):
    """A discovery source. Emits sightings from infrastructure we control."""

    @property
    def source(self) -> str: ...

    def collect(self) -> list[Sighting]:
        """Return current sightings. Must never raise — return [] on failure."""
        ...


@dataclass
class DiscoveredAgent:
    """A reconciled agent: merged sightings + its governance status."""

    agent_id: str
    status: str                      # "governed" | "discovered" | "governed_unseen"
    sources: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    gateways: list[str] = field(default_factory=list)
    call_count: int = 0
    confidence: float = 0.0
    registered: bool = False         # is it in the agents registry?


def _norm(agent_id: str) -> str:
    """Canonicalize an id for matching (lowercase, strip a2a. prefix, trim)."""
    a = (agent_id or "").strip().lower()
    if a.startswith("a2a."):
        a = a[len("a2a."):]
    return a


def reconcile(
    collectors: Iterable[Collector],
    known_agent_ids: Iterable[str],
) -> list[DiscoveredAgent]:
    """Diff SEEN (union of all collectors) against KNOWN (the registry).

    Returns one DiscoveredAgent per distinct agent, classified:
      - governed:        seen AND registered
      - discovered:      seen AND NOT registered   ← the shadow-AI gap
      - governed_unseen: registered but never seen (stale / decommissioned?)
    """
    known = {_norm(k): k for k in known_agent_ids}

    # Merge sightings across sources, keyed by normalized id.
    merged: dict[str, DiscoveredAgent] = {}
    for collector in collectors:
        try:
            sightings = collector.collect()
        except Exception:  # noqa: BLE001 — a bad source must not break discovery
            sightings = []
        for s in sightings:
            key = _norm(s.agent_id)
            if not key:
                continue
            da = merged.get(key)
            if da is None:
                da = DiscoveredAgent(agent_id=s.agent_id, status="discovered")
                merged[key] = da
            if s.source not in da.sources:
                da.sources.append(s.source)
            if s.evidence:
                da.evidence.append(f"[{s.source}] {s.evidence}")
            for gw in s.gateways:
                if gw not in da.gateways:
                    da.gateways.append(gw)
            da.call_count += s.call_count
            da.confidence = max(da.confidence, s.confidence)

    # Classify against the registry.
    results: list[DiscoveredAgent] = []
    for key, da in merged.items():
        if key in known:
            da.status = "governed"
            da.registered = True
        else:
            da.status = "discovered"
            da.registered = False
        results.append(da)

    # Registered-but-never-seen (stale) — surface these too.
    seen_keys = set(merged.keys())
    for key, original in known.items():
        if key not in seen_keys:
            results.append(DiscoveredAgent(
                agent_id=original, status="governed_unseen",
                registered=True, confidence=1.0,
                evidence=["registered in the agents registry; not seen by any source"],
            ))

    # Shadow AI first (most actionable), then governed, then stale.
    order = {"discovered": 0, "governed": 1, "governed_unseen": 2}
    results.sort(key=lambda d: (order.get(d.status, 9), -d.confidence, -d.call_count))
    return results
