"""Ostiari policy router for control-plane model selection.

This layer applies operator-owned policy: per-agent rotation, experiments,
explicit conditions, and keyword categories. AxonLLM performs health-aware
provider selection and smart/ensemble routing after this policy step.

Supports A/B experiments: percentage-based traffic splitting between models.
"""

import hashlib
import logging
from typing import Any

from ostiari_gateway.modules.llm_gateway.models import LLMConfig, RoutingRule

log = logging.getLogger("ostiari.sidecar.llm")


class ModelRouter:
    """Selects which LLM model to use based on routing rules.

    Smart task classification belongs to the embedded Axon router. Keeping that
    work out of this class avoids constructing a second private Axon routing
    stack and keeps Ostiari's control-plane policy deterministic.
    """

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        # Round-robin state: per-agent request counter, and per-session sticky pick.
        self._rr_counter: dict[str, int] = {}
        self._session_pick: dict[str, str] = {}

    def update_config(self, config: LLMConfig) -> None:
        self._config = config

    def select_model(self, context: dict[str, Any]) -> str:
        """Select a model based on routing rules and context.

        Priority:
        1. Per-agent routing policy (round-robin across LLMs)
        2. A/B experiments (percentage-based split)
        3. Explicit control plane rules (condition matching)
        4. Operator-defined keyword classification
        5. Default model

        The selected model is subsequently handed to AxonLLM. When the selected
        name is absent from Axon's catalog, the embedded router smart-routes the
        request through its own public routing API.
        """
        # Per-agent model-rotation policy takes precedence: an operator opting an
        # agent into round-robin means "spread this agent across these LLMs".
        rr = self._check_agent_routing(context)
        if rr is not None:
            return rr

        # Check A/B experiments
        ab_result = self._check_ab_experiments(context)
        if ab_result is not None:
            return ab_result

        # Check explicit rules
        for rule in self._config.routing_rules:
            if self._evaluate_condition(rule, context):
                log.debug("Routing rule matched: %s → %s", rule.condition, rule.model)
                return rule.model

        classified = self._check_task_classification(context)
        if classified is not None:
            return classified

        return self._config.default_model

    def _check_task_classification(self, context: dict[str, Any]) -> str | None:
        """Apply configured keyword categories to the latest user message."""
        config = self._config.task_classification
        if not config.rules or not config.model_mapping:
            return None

        messages = context.get("messages")
        if not isinstance(messages, list) or not messages:
            return None
        last = messages[-1]
        content = last.get("content", "") if isinstance(last, dict) else ""
        if isinstance(content, list):
            content = " ".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict)
            )
        prompt = str(content).lower()
        if not prompt:
            return None

        best_category = ""
        best_score = 0
        for category, keywords in config.rules.items():
            score = sum(
                1
                for keyword in keywords
                if keyword.strip() and keyword.strip().lower() in prompt
            )
            if score > best_score:
                best_category = category
                best_score = score

        model = config.model_mapping.get(best_category, "")
        if model:
            log.debug(
                "Task classification matched category=%s score=%d → %s",
                best_category,
                best_score,
                model,
            )
            return model
        return None

    def _check_agent_routing(self, context: dict[str, Any]) -> str | None:
        """Apply a per-agent round-robin model policy, if one is configured.

        Looks up the agent's policy (falling back to a "*" wildcard policy).
        Returns the chosen model, or None if no policy applies.
        """
        policies = getattr(self._config, "agent_routing", None)
        if not policies:
            return None

        agent_id = str(context.get("agent_id", "") or "")
        policy = policies.get(agent_id) or policies.get("*")
        if policy is None:
            return None

        models = list(getattr(policy, "models", []) or [])
        if not models:
            return None
        if getattr(policy, "strategy", "round_robin") != "round_robin":
            return None

        if len(models) == 1:
            return models[0]

        scope = getattr(policy, "scope", "request")
        if scope == "session":
            # Sticky per session: same X-Session-Id keeps one model; a new
            # session advances the rotation. Falls back to request scope when no
            # session id is present.
            session_id = str(context.get("session_id", "") or "")
            if session_id:
                key = f"{agent_id}:{session_id}"
                if key not in self._session_pick:
                    idx = self._rr_counter.get(agent_id, 0) % len(models)
                    self._rr_counter[agent_id] = idx + 1
                    self._session_pick[key] = models[idx]
                    log.debug("Round-robin (session) agent=%s session=%s → %s",
                              agent_id, session_id, self._session_pick[key])
                return self._session_pick[key]

        # Request scope (default): advance on every call.
        idx = self._rr_counter.get(agent_id, 0) % len(models)
        self._rr_counter[agent_id] = idx + 1
        chosen = models[idx]
        log.debug("Round-robin (request) agent=%s → %s", agent_id, chosen)
        return chosen

    def _check_ab_experiments(self, context: dict[str, Any]) -> str | None:
        """Return the A/B-assigned model for the first *in-scope* experiment.

        Consistent hashing on agent_id keeps an agent on the same bucket across
        requests. An experiment scoped to specific ``agents`` only applies to
        those; out-of-scope requests fall through to the next experiment (and
        ultimately to rules/smart/default) instead of being hijacked. Returns
        None when no experiment applies.
        """
        agent_id = context.get("agent_id", "unknown")
        for exp in self._config.ab_experiments:
            if not exp.enabled:
                continue
            # Scope: if the experiment lists agents, only those participate.
            if exp.agents and agent_id not in exp.agents:
                continue

            # Consistent hash: same agent_id always gets same bucket
            hash_input = f"{exp.name}:{agent_id}"
            hash_val = int(hashlib.md5(hash_input.encode()).hexdigest(), 16) % 100

            if hash_val < exp.traffic_pct_b:
                log.debug("A/B [%s]: agent=%s → model_b=%s (%d%% bucket)",
                         exp.name, agent_id, exp.model_b, exp.traffic_pct_b)
                context["_ab_experiment"] = exp.name
                context["_ab_variant"] = "B"
                return exp.model_b
            context["_ab_experiment"] = exp.name
            context["_ab_variant"] = "A"
            return exp.model_a

        return None

    def get_active_experiments(self) -> list[dict[str, Any]]:
        """List active A/B experiments."""
        return [
            {"name": e.name, "model_a": e.model_a, "model_b": e.model_b,
             "traffic_pct_b": e.traffic_pct_b, "enabled": e.enabled}
            for e in self._config.ab_experiments
        ]

    def get_fallback_chain(self, primary: str) -> list[str]:
        """Get the fallback chain starting after the primary model."""
        chain = self._config.fallback_chain
        if primary in chain:
            idx = chain.index(primary)
            return chain[idx + 1:]
        return chain

    def _evaluate_condition(self, rule: RoutingRule, context: dict[str, Any]) -> bool:
        """Evaluate a routing condition against the request context."""
        condition = rule.condition.strip()

        if "==" in condition:
            key, value = condition.split("==", 1)
            key = key.strip()
            value = value.strip().strip("'\"")
            return str(context.get(key, "")) == value

        if ">" in condition:
            key, value = condition.split(">", 1)
            key = key.strip()
            try:
                return float(context.get(key, 0)) > float(value.strip())
            except (ValueError, TypeError):
                return False

        if "<" in condition:
            key, value = condition.split("<", 1)
            key = key.strip()
            try:
                return float(context.get(key, 0)) < float(value.strip())
            except (ValueError, TypeError):
                return False

        # Boolean flag
        return bool(context.get(condition, False))
