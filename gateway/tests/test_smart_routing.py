"""Tests for Ostiari policy selection before AxonLLM routing."""

from __future__ import annotations

from ostiari_gateway.modules.llm_gateway.models import LLMConfig, RoutingRule
from ostiari_gateway.modules.llm_gateway.router import ModelRouter


class TestPolicyRouting:
    def test_no_policy_falls_back_to_default(self):
        cfg = LLMConfig(default_model="claude-sonnet-4-6")
        r = ModelRouter(cfg)
        assert r.select_model({"agent_id": "x", "messages":
                               [{"role": "user", "content": "write code"}]}) == "claude-sonnet-4-6"

    def test_explicit_rules_work_before_smart_routing(self):
        cfg = LLMConfig(default_model="d", routing_rules=[
            RoutingRule(condition="tier == 'premium'", model="opus")])
        r = ModelRouter(cfg)
        assert r.select_model({"tier": "premium"}) == "opus"

    def test_operator_keyword_rules_route_before_axon(self):
        cfg = LLMConfig(
            default_model="default",
            task_classification={
                "rules": {
                    "coding": ["code", "function"],
                    "analysis": ["analyze"],
                },
                "model_mapping": {
                    "coding": "code-model",
                    "analysis": "analysis-model",
                },
            },
        )
        router = ModelRouter(cfg)
        assert router.select_model({
            "messages": [{"role": "user", "content": "Implement a function in code"}],
        }) == "code-model"

    def test_category_with_most_keyword_matches_wins(self):
        cfg = LLMConfig(
            default_model="default",
            task_classification={
                "rules": {
                    "coding": ["code", "function"],
                    "analysis": ["analyze"],
                },
                "model_mapping": {
                    "coding": "code-model",
                    "analysis": "analysis-model",
                },
            },
        )
        router = ModelRouter(cfg)
        assert router.select_model({
            "messages": [{
                "role": "user",
                "content": "Analyze this code function",
            }],
        }) == "code-model"

    def test_agent_round_robin_precedes_keyword_routing(self):
        from ostiari_gateway.modules.llm_gateway.models import AgentRoutingPolicy
        cfg = LLMConfig(
            default_model="d",
            task_classification={
                "rules": {"coding": ["function"]},
                "model_mapping": {"coding": "opus"},
            },
            agent_routing={"cc": AgentRoutingPolicy(models=["rr-model"])},
        )
        r = ModelRouter(cfg)
        m = r.select_model({"agent_id": "cc", "messages":
                            [{"role": "user", "content": "write a function"}]})
        assert m == "rr-model"
