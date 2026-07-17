"""Cross-agent (A2A) delegation governance.

Tool-level auth answers "can agent A use capability X?". This module answers
the questions that only exist *between* agents:

  1. Delegation edges — may agent A delegate to agent B at all?
  2. Trust threshold — is B trustworthy enough (safety score) to receive work?
  3. Chain depth — has a delegation chain grown suspiciously deep?

It is evaluated at the *calling* gateway before an A2A request is dispatched,
using the propagated delegation chain so decisions have full provenance.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("ostiari.gateway.cross_agent")

# Default trust score for an agent with no explicit score configured.
_DEFAULT_TRUST = 50


class CrossAgentPolicy:
    """Governs agent-to-agent delegation: edges, trust scores, and chain depth."""

    def __init__(self) -> None:
        self._enabled: bool = False
        # caller -> set of callees it may delegate to ("*" = any)
        self._allow: dict[str, set[str]] = {}
        # explicit deny edges (deny wins over allow)
        self._deny: dict[str, set[str]] = {}
        self._trust_scores: dict[str, int] = {}
        self._min_trust: int = 0
        self._max_chain_depth: int | None = None
        self._default_allow: bool = True

    def configure(self, config: dict[str, Any]) -> None:
        """Configure from a control-plane push.

        Config format::

            {
              "enabled": true,
              "default_allow": false,          # least privilege when true edges absent
              "min_trust": 60,                 # block callees below this score
              "max_chain_depth": 4,            # block chains longer than this
              "trust_scores": {"research": 90, "sketchy": 20},
              "edges": {
                "research": {"allow": ["coder", "db"], "deny": ["payments"]},
                "coder": {"allow": ["*"]}
              }
            }
        """
        self._enabled = config.get("enabled", False)
        self._default_allow = config.get("default_allow", True)
        self._min_trust = config.get("min_trust", 0)
        self._max_chain_depth = config.get("max_chain_depth")
        self._trust_scores = dict(config.get("trust_scores", {}))

        self._allow.clear()
        self._deny.clear()
        for caller, edge in config.get("edges", {}).items():
            self._allow[caller] = set(edge.get("allow", []))
            self._deny[caller] = set(edge.get("deny", []))

        log.info(
            "Cross-agent policy configured: enabled=%s, %d edges, min_trust=%s, max_depth=%s",
            self._enabled, len(self._allow), self._min_trust, self._max_chain_depth,
        )

    def trust_score(self, agent_id: str) -> int:
        """Return an agent's configured trust score (default 50)."""
        return self._trust_scores.get(agent_id, _DEFAULT_TRUST)

    def _edge_allowed(self, caller: str, callee: str) -> bool:
        """Apply deny-wins edge rules; fall back to default_allow."""
        deny = self._deny.get(caller, set())
        if callee in deny or "*" in deny:
            return False
        allow = self._allow.get(caller)
        if allow is None:
            return self._default_allow
        return callee in allow or "*" in allow

    def check(
        self, caller: str, callee: str, chain: list[str] | None = None
    ) -> tuple[bool, str]:
        """Decide whether ``caller`` may delegate to ``callee``.

        Returns (allowed, reason). When disabled, always allows.
        """
        if not self._enabled:
            return True, ""

        # Chain depth guard (defense against runaway delegation).
        if (
            self._max_chain_depth is not None
            and chain is not None
            and len(chain) > self._max_chain_depth
        ):
            return False, (
                f"Delegation chain depth {len(chain)} exceeds max "
                f"{self._max_chain_depth}: {'>'.join(chain)}"
            )

        # Edge rule (deny wins, then allow list, then default).
        if not self._edge_allowed(caller, callee):
            return False, f"Delegation '{caller}' -> '{callee}' not permitted by policy"

        # Trust threshold on the callee.
        score = self.trust_score(callee)
        if score < self._min_trust:
            return False, (
                f"Callee '{callee}' trust score {score} below minimum {self._min_trust}"
            )

        return True, ""

    def get_status(self) -> dict[str, Any]:
        """Return current policy state (for the /config/cross-agent endpoint)."""
        return {
            "enabled": self._enabled,
            "default_allow": self._default_allow,
            "min_trust": self._min_trust,
            "max_chain_depth": self._max_chain_depth,
            "trust_scores": dict(self._trust_scores),
            "edges": {
                caller: {
                    "allow": sorted(self._allow.get(caller, set())),
                    "deny": sorted(self._deny.get(caller, set())),
                }
                for caller in set(self._allow) | set(self._deny)
            },
        }
