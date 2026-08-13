"""Per-agent tool authorization — policy-based, least privilege.

Each agent is granted access to specific tools. Any tool not explicitly
granted is denied. This complements JWT/token authentication: the gateway first
binds a request to the verified token identity, then this policy decides which
tools, models, and providers that identity may use.

Design:
  - Default: DENY ALL (least privilege principle)
  - Grants are pushed from the control plane per agent
  - The sidecar checks grants BEFORE policy evaluation
  - If an agent is not registered, it gets DEFAULT grants (configurable)

Authentication never overrides these grants. A valid identity without an
explicit grant is still denied.
"""

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("ostiari.sidecar.agent_auth")


@dataclass(frozen=True)
class AgentQuotaDecision:
    """Result of an agent-level LLM quota check."""

    allowed: bool
    reason: str = ""
    limit_type: str = ""
    reservation_id: int | None = None


class AgentGrants:
    """Per-agent access grants and runtime quota state."""

    def __init__(
        self,
        agent_id: str,
        allowed_tools: list[str],
        allowed_models: list[str] | None = None,
        allowed_providers: list[str] | None = None,
        budget_usd: float | None = None,
        spend_usd: float = 0.0,
        rate_limit_rpm: int | None = None,
        max_tokens_per_request: int | None = None,
        alert_threshold_pct: int = 90,
        description: str = "",
    ) -> None:
        self.agent_id = agent_id
        self.allowed_tools = set(allowed_tools)
        self.allowed_models = set(allowed_models) if allowed_models else None
        self.allowed_providers = set(allowed_providers) if allowed_providers else None
        self.budget_usd = budget_usd
        self.spend_usd = max(0.0, float(spend_usd))
        self.rate_limit_rpm = rate_limit_rpm
        self.max_tokens_per_request = max_tokens_per_request
        self.alert_threshold_pct = max(1, min(100, int(alert_threshold_pct)))
        self.description = description
        self.request_times: deque[float] = deque()
        self.reservations: dict[int, tuple[float, float]] = {}
        self.reservation_seq = 0
        self.alerted_thresholds: set[int] = set()

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
        return self.spend_usd + self.reserved_spend() < self.budget_usd

    def _prune_requests(self, now: float | None = None) -> None:
        cutoff = (now if now is not None else time.monotonic()) - 60.0
        while self.request_times and self.request_times[0] <= cutoff:
            self.request_times.popleft()

    def _prune_reservations(self, now: float | None = None) -> None:
        cutoff = (now if now is not None else time.monotonic()) - 300.0
        expired = [
            reservation_id
            for reservation_id, (_amount, created_at) in self.reservations.items()
            if created_at <= cutoff
        ]
        for reservation_id in expired:
            self.reservations.pop(reservation_id, None)

    def current_rpm(self) -> int:
        self._prune_requests()
        return len(self.request_times)

    def record_request(self) -> None:
        self.request_times.append(time.monotonic())

    def reserved_spend(self) -> float:
        self._prune_reservations()
        return sum(amount for amount, _created_at in self.reservations.values())

    def check_quota(
        self,
        estimated_cost: float = 0.0,
        *,
        reserve: bool = False,
        count_request: bool = True,
    ) -> AgentQuotaDecision:
        now = time.monotonic()
        self._prune_requests(now)
        self._prune_reservations(now)

        if (
            count_request
            and self.rate_limit_rpm is not None
            and len(self.request_times) >= self.rate_limit_rpm
        ):
            return AgentQuotaDecision(
                allowed=False,
                reason=(
                    f"Agent '{self.agent_id}' rate limit exceeded "
                    f"({len(self.request_times)} / {self.rate_limit_rpm} RPM)"
                ),
                limit_type="rate_limit",
            )

        estimate = max(0.0, float(estimated_cost))
        if self.budget_usd is not None:
            committed = self.spend_usd + self.reserved_spend()
            projected = committed + estimate
            if committed >= self.budget_usd or projected >= self.budget_usd:
                return AgentQuotaDecision(
                    allowed=False,
                    reason=(
                        f"Agent '{self.agent_id}' budget would be exceeded "
                        f"(${projected:.4f} / ${self.budget_usd:.2f})"
                    ),
                    limit_type="budget",
                )

        if count_request:
            self.record_request()

        reservation_id = None
        if reserve and estimate:
            reservation_id = self.reserve(estimate, now)
        return AgentQuotaDecision(allowed=True, reservation_id=reservation_id)

    def reserve(self, amount: float, now: float | None = None) -> int:
        self.reservation_seq += 1
        self.reservations[self.reservation_seq] = (
            max(0.0, float(amount)),
            now if now is not None else time.monotonic(),
        )
        return self.reservation_seq

    def record_spend(self, cost: float, reservation_id: int | None = None) -> None:
        if reservation_id is not None:
            self.reservations.pop(reservation_id, None)
        self.spend_usd += max(0.0, float(cost))

    def release_reservation(self, reservation_id: int | None) -> None:
        if reservation_id is not None:
            self.reservations.pop(reservation_id, None)

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
        self._default_grants: list[str] = []
        self._default_models: list[str] = ["*"]
        self._default_providers: list[str] = ["*"]
        self._enabled: bool = False
        self._quota_enabled: bool = False
        self._budget_alert_callback: Any = None
        self._store: Any = None
        self._store_namespace = "gateway"
        self._shared_reservations: set[tuple[str, int]] = set()

    def on_budget_alert(self, callback: Any) -> None:
        """Subscribe to per-agent threshold crossings."""
        self._budget_alert_callback = callback

    def attach_shared_store(self, store: Any, namespace: str = "gateway") -> None:
        """Use Redis-backed counters when a fleet-wide store is configured."""
        self._store = store
        self._store_namespace = namespace or "gateway"
        if store is not None:
            for grants in self._grants.values():
                self._sync_shared_spend(grants)

    def _budget_key(self, agent_id: str) -> str:
        return f"{self._store_namespace}:agent:{agent_id}"

    def _sync_shared_spend(self, grants: AgentGrants) -> None:
        if self._store is None or grants.spend_usd <= 0:
            return
        current = self._store.budget_spend(self._budget_key(grants.agent_id))
        if current is None:
            return
        if grants.spend_usd > current:
            self._store.budget_adjust(
                self._budget_key(grants.agent_id),
                grants.spend_usd - current,
            )

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
                    "spend_usd": 2.50,
                    "rate_limit_rpm": 30,
                    "max_tokens_per_request": 4096,
                    "alert_threshold_pct": 80,
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
        # Older bundles used one switch for both tool authorization and LLM
        # limits. Keep that behavior unless the control plane explicitly
        # separates quota enforcement from tool authorization.
        self._quota_enabled = config.get("quota_enabled", self._enabled)
        self._default_grants = config.get("default_grants", [])
        self._default_models: list[str] = config.get("default_models", ["*"])
        self._default_providers: list[str] = config.get("default_providers", ["*"])

        # Preserve live counters and reservations across a hot reload. The pushed
        # spend is authoritative on restart; max() prevents a slightly older
        # control-plane aggregate from rolling back spend incurred seconds ago.
        old_grants = dict(self._grants)
        self._grants.clear()

        agents = config.get("agents", {})
        for agent_id, agent_config in agents.items():
            old = old_grants.get(agent_id)
            grants = AgentGrants(
                agent_id=agent_id,
                allowed_tools=agent_config.get("allowed_tools", []),
                allowed_models=agent_config.get("allowed_models"),
                allowed_providers=agent_config.get("allowed_providers"),
                budget_usd=agent_config.get("budget_usd"),
                spend_usd=max(
                    float(agent_config.get("spend_usd", 0.0) or 0.0),
                    old.spend_usd if old else 0.0,
                ),
                rate_limit_rpm=agent_config.get("rate_limit_rpm"),
                max_tokens_per_request=agent_config.get("max_tokens_per_request"),
                alert_threshold_pct=agent_config.get("alert_threshold_pct", 90),
                description=agent_config.get("description", ""),
            )
            if old:
                grants.request_times = old.request_times
                grants.reservations = old.reservations
                grants.reservation_seq = old.reservation_seq
                grants.alerted_thresholds = old.alerted_thresholds
            self._grants[agent_id] = grants
            self._sync_shared_spend(grants)

        log.info(
            "Agent auth configured: enabled=%s, quota_enabled=%s, %d agents registered",
            self._enabled, self._quota_enabled, len(self._grants),
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
        if not self._quota_enabled:
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
        if not self._quota_enabled:
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
        if not self._quota_enabled:
            return True, ""

        grants = self._grants.get(agent_id)
        if grants is None:
            return True, ""

        if not grants.check_budget():
            return False, f"Agent '{agent_id}' budget exhausted (${grants.spend_usd:.4f} / ${grants.budget_usd:.2f})"
        return True, ""

    def authorize_llm(
        self, agent_id: str, model: str, provider: str
    ) -> tuple[bool, str]:
        """Combined pre-call authorization for an LLM request.

        Runs model, provider, and budget checks in order. Returns the first
        failure, or (True, "") if all pass. No-op when auth is disabled.
        This is the single seam both LLM paths (/v1/messages shim and /invoke)
        call so per-agent model/provider/budget grants are actually enforced.
        """
        decision = self.check_llm(
            agent_id,
            model,
            provider,
            estimated_cost=0.0,
            reserve=False,
            count_request=False,
        )
        return decision.allowed, decision.reason

    def check_llm(
        self,
        agent_id: str,
        model: str,
        provider: str,
        *,
        estimated_cost: float = 0.0,
        reserve: bool = False,
        count_request: bool = True,
    ) -> AgentQuotaDecision:
        """Authorize and reserve an agent-level LLM request."""
        if not self._quota_enabled:
            return AgentQuotaDecision(allowed=True)

        if model:
            ok, reason = self.check_model(agent_id, model)
            if not ok:
                return AgentQuotaDecision(False, reason, "model")
        if provider:
            ok, reason = self.check_provider(agent_id, provider)
            if not ok:
                return AgentQuotaDecision(False, reason, "provider")

        grants = self._grants.get(agent_id)
        if grants is None:
            return AgentQuotaDecision(allowed=True)
        if self._store is not None:
            if count_request and grants.rate_limit_rpm is not None:
                allowed = self._store.rate_allow(
                    f"{self._store_namespace}:agent:{agent_id}",
                    grants.rate_limit_rpm,
                    60.0,
                )
                if not allowed:
                    return AgentQuotaDecision(
                        False,
                        (
                            f"Agent '{agent_id}' rate limit exceeded "
                            f"({grants.rate_limit_rpm} RPM fleet-wide)"
                        ),
                        "rate_limit",
                    )
                grants.record_request()

            reservation_id = None
            estimate = max(0.0, float(estimated_cost))
            if grants.budget_usd is not None:
                budget_key = self._budget_key(agent_id)
                if reserve and estimate:
                    if not self._store.budget_reserve(
                        budget_key, estimate, grants.budget_usd
                    ):
                        return AgentQuotaDecision(
                            False,
                            (
                                f"Agent '{agent_id}' budget would be exceeded "
                                f"(fleet-wide ${grants.budget_usd:.2f} limit)"
                            ),
                            "budget",
                        )
                    reservation_id = grants.reserve(estimate)
                    self._shared_reservations.add((agent_id, reservation_id))
                else:
                    shared_spend = self._store.budget_spend(budget_key)
                    if shared_spend is None:
                        return AgentQuotaDecision(
                            False,
                            "Shared quota store is unavailable",
                            "shared_store",
                        )
                    projected = shared_spend + estimate
                    if projected >= grants.budget_usd:
                        return AgentQuotaDecision(
                            False,
                            (
                                f"Agent '{agent_id}' budget would be exceeded "
                                f"(fleet-wide ${projected:.4f} / "
                                f"${grants.budget_usd:.2f})"
                            ),
                            "budget",
                        )
            return AgentQuotaDecision(True, reservation_id=reservation_id)
        return grants.check_quota(
            estimated_cost,
            reserve=reserve,
            count_request=count_request,
        )

    def cap_max_tokens(self, agent_id: str, requested: int) -> int:
        """Cap an LLM request to the agent's configured token ceiling."""
        grants = self._grants.get(agent_id)
        if (
            not self._quota_enabled
            or grants is None
            or grants.max_tokens_per_request is None
        ):
            return requested
        return min(requested, grants.max_tokens_per_request)

    def record_agent_spend(
        self,
        agent_id: str,
        cost_usd: float,
        reservation_id: int | None = None,
    ) -> None:
        """Record spend against an agent's budget."""
        grants = self._grants.get(agent_id)
        if grants:
            estimate = 0.0
            if reservation_id is not None:
                reservation = grants.reservations.get(reservation_id)
                estimate = reservation[0] if reservation else 0.0
            if self._store is not None:
                key = self._budget_key(agent_id)
                marker = (agent_id, reservation_id) if reservation_id is not None else None
                if marker is not None and marker in self._shared_reservations:
                    self._shared_reservations.discard(marker)
                    self._store.budget_adjust(key, cost_usd - estimate)
                elif grants.budget_usd is not None:
                    self._store.budget_adjust(key, cost_usd)
            grants.record_spend(cost_usd, reservation_id)
            if self._store is not None:
                shared_spend = self._store.budget_spend(
                    self._budget_key(agent_id)
                )
                if shared_spend is not None:
                    grants.spend_usd = max(grants.spend_usd, shared_spend)
            self._emit_budget_alerts(grants)

    def release_agent_reservation(
        self, agent_id: str, reservation_id: int | None
    ) -> None:
        grants = self._grants.get(agent_id)
        if grants:
            marker = (agent_id, reservation_id) if reservation_id is not None else None
            if (
                self._store is not None
                and marker is not None
                and marker in self._shared_reservations
            ):
                reservation = grants.reservations.get(reservation_id)
                self._store.budget_adjust(
                    self._budget_key(agent_id),
                    -(reservation[0] if reservation else 0.0),
                )
                self._shared_reservations.discard(marker)
            grants.release_reservation(reservation_id)

    def _emit_budget_alerts(self, grants: AgentGrants) -> None:
        if not grants.budget_usd:
            return
        percent = (grants.spend_usd / grants.budget_usd) * 100
        thresholds = {grants.alert_threshold_pct, 100}
        for threshold in sorted(thresholds):
            if percent < threshold or threshold in grants.alerted_thresholds:
                continue
            grants.alerted_thresholds.add(threshold)
            log.warning(
                "Agent '%s' crossed %d%% of budget ($%.4f / $%.2f)",
                grants.agent_id,
                threshold,
                grants.spend_usd,
                grants.budget_usd,
            )
            if self._budget_alert_callback:
                try:
                    self._budget_alert_callback(
                        f"{threshold}%",
                        grants.agent_id,
                        grants.spend_usd,
                        grants.budget_usd,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.debug("Agent budget alert callback failed: %s", exc)

    def reset_spend(self) -> int:
        """Start a new budget period for every configured agent."""
        for grants in self._grants.values():
            grants.spend_usd = 0.0
            grants.reservations.clear()
            grants.alerted_thresholds.clear()
            if self._store is not None:
                self._store.budget_reset(self._budget_key(grants.agent_id))
        self._shared_reservations.clear()
        return len(self._grants)

    def get_spend_snapshot(self, *, include_zero: bool = False) -> dict[str, float]:
        """Get current spend for all agents (for persistence)."""
        return {
            aid: g.spend_usd
            for aid, g in self._grants.items()
            if include_zero or g.spend_usd > 0
        }

    def restore_spend(self, snapshot: dict[str, float]) -> None:
        """Restore spend from a persisted snapshot (e.g., from Control Plane)."""
        for agent_id, spend in snapshot.items():
            if agent_id in self._grants:
                self._grants[agent_id].spend_usd = max(
                    self._grants[agent_id].spend_usd,
                    float(spend),
                )
                self._sync_shared_spend(self._grants[agent_id])
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
                "rate_limit_rpm": g.rate_limit_rpm,
                "current_rpm": g.current_rpm(),
                "max_tokens_per_request": g.max_tokens_per_request,
                "alert_threshold_pct": g.alert_threshold_pct,
                "description": g.description,
            }
            for g in self._grants.values()
        ]

    def get_status(self) -> dict[str, Any]:
        """Get auth policy status."""
        return {
            "enabled": self._enabled,
            "quota_enabled": self._quota_enabled,
            "registered_agents": len(self._grants),
            "default_grants": self._default_grants,
            "default_models": self._default_models,
            "default_providers": self._default_providers,
            "quota_scope": "fleet" if self._store is not None else "process",
        }
