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

from collections.abc import Iterable, Mapping
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
    governed: bool = False           # observed through an enforcement point


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
    status: str                      # governed | discovered | registered_off_gateway | governed_unseen
    sources: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    gateways: list[str] = field(default_factory=list)
    governed_gateways: list[str] = field(default_factory=list)
    call_count: int = 0
    confidence: float = 0.0
    registered: bool = False         # is it in the agents registry?
    assigned_gateway: str = ""
    governed_observed: bool = False


def _norm(agent_id: str) -> str:
    """Canonicalize an id for matching (lowercase, strip a2a. prefix, trim)."""
    a = (agent_id or "").strip().lower()
    if a.startswith("a2a."):
        a = a[len("a2a."):]
    return a


def reconcile(
    collectors: Iterable[Collector],
    known_agent_ids: Iterable[str] | Mapping[str, str],
) -> list[DiscoveredAgent]:
    """Diff SEEN (union of all collectors) against KNOWN (the registry).

    Returns one DiscoveredAgent per distinct agent, classified:
      - governed:              registered and seen through its assigned gateway
      - discovered:            seen and not registered
      - registered_off_gateway: registered, but only seen outside its gateway
      - governed_unseen:       registered but never seen (stale / decommissioned?)
    """
    if isinstance(known_agent_ids, Mapping):
        known = {
            _norm(agent_id): (agent_id, gateway_id)
            for agent_id, gateway_id in known_agent_ids.items()
        }
    else:
        known = {_norm(agent_id): (agent_id, "") for agent_id in known_agent_ids}

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
                if s.governed and gw not in da.governed_gateways:
                    da.governed_gateways.append(gw)
            da.governed_observed = da.governed_observed or s.governed
            da.call_count += s.call_count
            da.confidence = max(da.confidence, s.confidence)

    # Classify against the registry.
    results: list[DiscoveredAgent] = []
    for key, da in merged.items():
        if key in known:
            original, assigned_gateway = known[key]
            da.agent_id = original
            da.registered = True
            da.assigned_gateway = assigned_gateway
            routed_through_assignment = (
                da.governed_observed
                and (
                    not assigned_gateway
                    or assigned_gateway in da.governed_gateways
                )
            )
            da.status = "governed" if routed_through_assignment else "registered_off_gateway"
        else:
            da.status = "discovered"
            da.registered = False
        results.append(da)

    # Registered-but-never-seen (stale) — surface these too.
    seen_keys = set(merged.keys())
    for key, (original, assigned_gateway) in known.items():
        if key not in seen_keys:
            results.append(DiscoveredAgent(
                agent_id=original, status="governed_unseen",
                registered=True, assigned_gateway=assigned_gateway, confidence=1.0,
                evidence=["registered in the agents registry; not seen by any source"],
            ))

    # Shadow AI first, then registered agents still bypassing their gateway.
    order = {
        "discovered": 0,
        "registered_off_gateway": 1,
        "governed": 2,
        "governed_unseen": 3,
    }
    results.sort(key=lambda d: (order.get(d.status, 9), -d.confidence, -d.call_count))
    return results
