"""Per-agent tool authorization — policy-based, least privilege.

Each agent is granted access to specific tools. Any tool not explicitly
granted is denied. This is NOT JWT/token authorization — it's a policy rule
that controls which agent can access which tool.

Design:
  - Default: DENY ALL (least privilege principle)
  - Grants are pushed from the control plane per agent
  - The sidecar checks grants BEFORE policy evaluation
  - If an agent is not registered, it gets DEFAULT grants (configurable)

Future:
  - JWT authorization will be layered on top
  - A JWT claim (e.g., role: "admin") can override agent grants
  - JWT overrides policy, not the other way around
  - This allows service accounts to bypass per-agent restrictions
"""

import logging
from typing import Any

log = logging.getLogger("ostiari.sidecar.agent_auth")


class AgentGrants:
    """Per-agent tool + model + provider access grants with budget caps."""

    def __init__(
        self,
        agent_id: str,
        allowed_tools: list[str],
        allowed_models: list[str] | None = None,
        allowed_providers: list[str] | None = None,
        budget_usd: float | None = None,
        description: str = "",
    ) -> None:
        self.agent_id = agent_id
        self.allowed_tools = set(allowed_tools)
        self.allowed_models = set(allowed_models) if allowed_models else None
        self.allowed_providers = set(allowed_providers) if allowed_providers else None
        self.budget_usd = budget_usd
        self.spend_usd: float = 0.0
        self.description = description

    def can_access(self, tool: str) -> bool:
        if "*" in self.allowed_tools:
            return True
        if tool in self.allowed_tools:
            return True
        for grant in self.allowed_tools:
            if grant.endswith(".*"):
                prefix = grant[:-2]
                if tool.startswith(prefix + "."):
                    return True
        return False

    def can_use_model(self, model: str) -> bool:
        if self.allowed_models is None:
            return True
        if "*" in self.allowed_models:
            return True
        if model in self.allowed_models:
            return True
        for pattern in self.allowed_models:
            if pattern.endswith(".*"):
                prefix = pattern[:-2]
                if model.startswith(prefix):
                    return True
        return False

    def can_use_provider(self, provider: str) -> bool:
        if self.allowed_providers is None:
            return True
        if "*" in self.allowed_providers:
            return True
        return provider.lower() in {p.lower() for p in self.allowed_providers}

    def check_budget(self) -> bool:
        if self.budget_usd is None:
            return True
        return self.spend_usd < self.budget_usd

    def record_spend(self, cost: float) -> None:
        self.spend_usd += cost

    def budget_remaining(self) -> float | None:
        if self.budget_usd is None:
            return None
        return max(0.0, self.budget_usd - self.spend_usd)


class AgentAuthPolicy:
    """Manages per-agent tool authorization.

    Least privilege: if an agent is not registered, it gets default_grants.
    If default_grants is empty, unregistered agents are denied all tools.
    """

    def __init__(self) -> None:
        self._grants: dict[str, AgentGrants] = {}
        self._default_grants: list[str] = []  # empty = deny unregistered agents
        self._enabled: bool = False

    def configure(self, config: dict[str, Any]) -> None:
        """Configure agent authorization from control plane push.

        Config format:
        {
            "enabled": true,
            "default_grants": ["db_query"],
            "default_models": ["*"],
            "default_providers": ["*"],
            "agents": {
                "research-agent": {
                    "allowed_tools": ["web_search", "file_read"],
                    "allowed_models": ["claude-haiku-4-5", "gpt-4o-mini"],
                    "allowed_providers": ["anthropic", "openai"],
                    "budget_usd": 10.00,
                    "description": "Cheap models only"
                },
                "ops-agent": {
                    "allowed_tools": ["*"],
                    "allowed_models": ["*"],
                    "allowed_providers": ["*"],
                    "description": "Full access"
                },
                "gov-bot": {
                    "allowed_tools": ["*"],
                    "allowed_providers": ["bedrock"],
                    "description": "AWS only — no data leaves the account"
                }
            }
        }
        """
        self._enabled = config.get("enabled", False)
        self._default_grants = config.get("default_grants", [])
        self._default_models: list[str] = config.get("default_models", ["*"])
        self._default_providers: list[str] = config.get("default_providers", ["*"])

        # Preserve spend from previous config (survive hot-reload)
        old_spend: dict[str, float] = {aid: g.spend_usd for aid, g in self._grants.items()}
        self._grants.clear()

        agents = config.get("agents", {})
        for agent_id, agent_config in agents.items():
            grants = AgentGrants(
                agent_id=agent_id,
                allowed_tools=agent_config.get("allowed_tools", []),
                allowed_models=agent_config.get("allowed_models"),
                allowed_providers=agent_config.get("allowed_providers"),
                budget_usd=agent_config.get("budget_usd"),
                description=agent_config.get("description", ""),
            )
            grants.spend_usd = old_spend.get(agent_id, 0.0)
            self._grants[agent_id] = grants

        log.info(
            "Agent auth configured: enabled=%s, %d agents registered",
            self._enabled, len(self._grants),
        )

    def check(self, agent_id: str, tool: str) -> tuple[bool, str]:
        """Check if an agent is authorized to access a tool.

        Returns: (allowed, reason)
        """
        if not self._enabled:
            return True, ""

        grants = self._grants.get(agent_id)

        if grants is None:
            if not self._default_grants:
                return False, f"Agent '{agent_id}' not registered and no default grants configured (least privilege)"
            default = AgentGrants(agent_id=agent_id, allowed_tools=self._default_grants, allowed_models=self._default_models)
            if default.can_access(tool):
                return True, ""
            return False, f"Agent '{agent_id}' (unregistered) not authorized for tool '{tool}'. Default grants: {self._default_grants}"

        if grants.can_access(tool):
            return True, ""
        return False, f"Agent '{agent_id}' not authorized for tool '{tool}'. Granted: {sorted(grants.allowed_tools)}"

    def check_model(self, agent_id: str, model: str) -> tuple[bool, str]:
        """Check if an agent is authorized to use a specific model.

        Returns: (allowed, reason)
        """
        if not self._enabled:
            return True, ""

        grants = self._grants.get(agent_id)

        if grants is None:
            default = AgentGrants(agent_id=agent_id, allowed_tools=[], allowed_models=self._default_models)
            if default.can_use_model(model):
                return True, ""
            return False, f"Agent '{agent_id}' (unregistered) not authorized for model '{model}'"

        if grants.can_use_model(model):
            return True, ""
        return False, f"Agent '{agent_id}' not authorized for model '{model}'. Allowed: {sorted(grants.allowed_models or set())}"

    def check_provider(self, agent_id: str, provider: str) -> tuple[bool, str]:
        """Check if an agent is authorized to use a specific provider.

        Provider examples: "anthropic", "openai", "bedrock", "azure", "vertex", "cohere"
        Returns: (allowed, reason)
        """
        if not self._enabled:
            return True, ""

        grants = self._grants.get(agent_id)

        if grants is None:
            default = AgentGrants(agent_id=agent_id, allowed_tools=[], allowed_providers=self._default_providers)
            if default.can_use_provider(provider):
                return True, ""
            return False, f"Agent '{agent_id}' (unregistered) not authorized for provider '{provider}'"

        if grants.can_use_provider(provider):
            return True, ""
        return False, f"Agent '{agent_id}' not authorized for provider '{provider}'. Allowed: {sorted(grants.allowed_providers or set())}"

    def check_budget(self, agent_id: str) -> tuple[bool, str]:
        """Check if an agent has remaining budget.

        Returns: (allowed, reason)
        """
        if not self._enabled:
            return True, ""

        grants = self._grants.get(agent_id)
        if grants is None:
            return True, ""

        if not grants.check_budget():
            return False, f"Agent '{agent_id}' budget exhausted (${grants.spend_usd:.4f} / ${grants.budget_usd:.2f})"
        return True, ""

    def record_agent_spend(self, agent_id: str, cost_usd: float) -> None:
        """Record spend against an agent's budget."""
        grants = self._grants.get(agent_id)
        if grants:
            grants.record_spend(cost_usd)
            if grants.budget_usd and grants.spend_usd >= grants.budget_usd * 0.9:
                log.warning("Agent '%s' at %.0f%% of budget ($%.4f / $%.2f)",
                            agent_id, (grants.spend_usd / grants.budget_usd) * 100,
                            grants.spend_usd, grants.budget_usd)

    def get_spend_snapshot(self) -> dict[str, float]:
        """Get current spend for all agents (for persistence)."""
        return {aid: g.spend_usd for aid, g in self._grants.items() if g.spend_usd > 0}

    def restore_spend(self, snapshot: dict[str, float]) -> None:
        """Restore spend from a persisted snapshot (e.g., from Control Plane)."""
        for agent_id, spend in snapshot.items():
            if agent_id in self._grants:
                self._grants[agent_id].spend_usd = spend
        log.info("Restored spend for %d agents", len(snapshot))

    def get_agent_grants(self, agent_id: str) -> list[str]:
        """Get the list of tools an agent is authorized to use."""
        grants = self._grants.get(agent_id)
        if grants:
            return sorted(grants.allowed_tools)
        return list(self._default_grants)

    def get_agent_models(self, agent_id: str) -> list[str]:
        """Get the list of models an agent is authorized to use."""
        grants = self._grants.get(agent_id)
        if grants and grants.allowed_models:
            return sorted(grants.allowed_models)
        return list(self._default_models)

    def list_agents(self) -> list[dict[str, Any]]:
        """List all registered agents and their grants."""
        return [
            {
                "agent_id": g.agent_id,
                "allowed_tools": sorted(g.allowed_tools),
                "allowed_models": sorted(g.allowed_models) if g.allowed_models else ["*"],
                "allowed_providers": sorted(g.allowed_providers) if g.allowed_providers else ["*"],
                "budget_usd": g.budget_usd,
                "spend_usd": round(g.spend_usd, 4),
                "budget_remaining_usd": round(g.budget_remaining(), 4) if g.budget_remaining() is not None else None,
                "description": g.description,
            }
            for g in self._grants.values()
        ]

    def get_status(self) -> dict[str, Any]:
        """Get auth policy status."""
        return {
            "enabled": self._enabled,
            "registered_agents": len(self._grants),
            "default_grants": self._default_grants,
            "default_models": self._default_models,
            "default_providers": self._default_providers,
        }
