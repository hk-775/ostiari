"""Model router — selects LLM based on control plane rules + AxonLLM smart routing.

When AxonLLM is available, uses its TaskClassifier and SmartRoutingStrategy
for intent-aware model selection. Falls back to simple rule evaluation
if AxonLLM is not installed.

Supports A/B experiments: percentage-based traffic splitting between models.
"""

import hashlib
import logging
from typing import Any

from ostiari_gateway.modules.llm_gateway.models import LLMConfig, RoutingRule

log = logging.getLogger("ostiari.sidecar.llm")


class ModelRouter:
    """Selects which LLM model to use based on routing rules.

    Supports two modes:
    1. Simple rule evaluation (always available)
    2. AxonLLM smart routing with task classification (when installed)
    """

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._smart_router: Any = None
        self._task_classifier: Any = None
        self._init_smart_routing()

    def update_config(self, config: LLMConfig) -> None:
        self._config = config
        self._init_smart_routing()

    def _init_smart_routing(self) -> None:
        """Try to initialize AxonLLM's smart routing."""
        try:
            from gateway.task_classifier import TaskClassifier

            self._task_classifier = TaskClassifier()
            log.info("AxonLLM TaskClassifier available for smart routing")
        except ImportError:
            self._task_classifier = None

    def select_model(self, context: dict[str, Any]) -> str:
        """Select a model based on routing rules and context.

        Priority:
        1. A/B experiments (percentage-based split)
        2. Explicit control plane rules (condition matching)
        3. AxonLLM smart routing (task classification) if available
        4. Default model
        """
        # Check A/B experiments first
        ab_result = self._check_ab_experiments(context)
        if ab_result is not None:
            return ab_result

        # Check explicit rules
        for rule in self._config.routing_rules:
            if self._evaluate_condition(rule, context):
                log.debug("Routing rule matched: %s → %s", rule.condition, rule.model)
                return rule.model

        # Try AxonLLM smart routing based on message content
        if self._task_classifier and "messages" in context:
            messages = context["messages"]
            if messages:
                last_msg = messages[-1].get("content", "") if isinstance(messages[-1], dict) else ""
                if last_msg:
                    result = self._task_classifier.classify(last_msg)
                    model = self._task_type_to_model(result.task_type)
                    if model:
                        log.debug("Smart routing: task=%s → %s", result.task_type, model)
                        return model

        return self._config.default_model

    def _check_ab_experiments(self, context: dict[str, Any]) -> str | None:
        """Check if any A/B experiment should route this request.

        Uses consistent hashing on agent_id so the same agent always gets
        the same model (no flip-flopping between requests).
        """
        for exp in self._config.ab_experiments:
            if not exp.enabled:
                continue

            # Consistent hash: same agent_id always gets same bucket
            agent_id = context.get("agent_id", "unknown")
            hash_input = f"{exp.name}:{agent_id}"
            hash_val = int(hashlib.md5(hash_input.encode()).hexdigest(), 16) % 100

            if hash_val < exp.traffic_pct_b:
                log.debug("A/B [%s]: agent=%s → model_b=%s (%d%% bucket)",
                         exp.name, agent_id, exp.model_b, exp.traffic_pct_b)
                context["_ab_experiment"] = exp.name
                context["_ab_variant"] = "B"
                return exp.model_b
            else:
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

    def _task_type_to_model(self, task_type: str) -> str | None:
        """Map AxonLLM task types to models (configurable via routing rules)."""
        # Check if there's a rule for this task type
        for rule in self._config.routing_rules:
            if rule.condition == f"task_type == '{task_type}'":
                return rule.model
        return None

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
