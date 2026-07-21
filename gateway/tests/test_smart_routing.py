"""Tests for AxonLLM-embedded smart routing (task-classification -> model).

Smart routing depends on the embedded AxonLLM package (src.gateway). These
tests are skipped when it isn't installed, and the fallback behavior is
asserted unconditionally.
"""

from __future__ import annotations

import pytest
from ostiari_gateway.modules.llm_gateway.models import LLMConfig, RoutingRule
from ostiari_gateway.modules.llm_gateway.router import ModelRouter

_AXON = None
try:
    from src.gateway.task_classifier import TaskClassifier  # noqa: F401
    _AXON = True
except ImportError:
    _AXON = False

requires_axon = pytest.mark.skipif(not _AXON, reason="AxonLLM (src.gateway) not installed")


class TestFallbackWithoutAxon:
    def test_no_classifier_falls_back_to_default(self):
        """With no task_classifier, routing returns the default model."""
        cfg = LLMConfig(default_model="claude-sonnet-4-6")
        r = ModelRouter(cfg)
        r._task_classifier = None  # simulate AxonLLM absent
        assert r.select_model({"agent_id": "x", "messages":
                               [{"role": "user", "content": "write code"}]}) == "claude-sonnet-4-6"

    def test_explicit_rules_work_without_smart_routing(self):
        cfg = LLMConfig(default_model="d", routing_rules=[
            RoutingRule(condition="tier == 'premium'", model="opus")])
        r = ModelRouter(cfg)
        r._task_classifier = None
        assert r.select_model({"tier": "premium"}) == "opus"


@requires_axon
class TestSmartRouting:
    def test_classifier_is_active_when_axon_present(self):
        r = ModelRouter(LLMConfig(default_model="d"))
        assert r._task_classifier is not None

    def test_coding_prompt_routes_by_task_type(self):
        # "def foo(): ... refactor this code" classifies as 'coding' (verified);
        # asserts classification actually drives model selection.
        cfg = LLMConfig(default_model="claude-sonnet-4-6", routing_rules=[
            RoutingRule(condition="task_type == 'coding'", model="claude-opus-4-8")])
        r = ModelRouter(cfg)
        m = r.select_model({"agent_id": "x", "messages":
                            [{"role": "user", "content": "def foo(): pass  # refactor this code"}]})
        assert m == "claude-opus-4-8"

    def test_creative_prompt_routes_to_its_model(self):
        cfg = LLMConfig(default_model="claude-sonnet-4-6", routing_rules=[
            RoutingRule(condition="task_type == 'creative_writing'", model="claude-haiku-4-5")])
        r = ModelRouter(cfg)
        m = r.select_model({"agent_id": "x", "messages":
                            [{"role": "user", "content": "write a poem about the ocean"}]})
        assert m == "claude-haiku-4-5"

    def test_unmapped_task_type_uses_default(self):
        # classifier runs, but no rule maps its task_type -> default model.
        # "write a poem" classifies creative_writing; only 'coding' is mapped.
        cfg = LLMConfig(default_model="claude-sonnet-4-6", routing_rules=[
            RoutingRule(condition="task_type == 'coding'", model="opus")])
        r = ModelRouter(cfg)
        m = r.select_model({"agent_id": "x", "messages":
                            [{"role": "user", "content": "write a poem about the ocean"}]})
        assert m == "claude-sonnet-4-6"

    def test_agent_round_robin_still_precedes_smart_routing(self):
        # per-agent routing policy wins over smart routing
        from ostiari_gateway.modules.llm_gateway.models import AgentRoutingPolicy
        cfg = LLMConfig(default_model="d",
                        routing_rules=[RoutingRule(condition="task_type == 'coding'", model="opus")],
                        agent_routing={"cc": AgentRoutingPolicy(models=["rr-model"])})
        r = ModelRouter(cfg)
        m = r.select_model({"agent_id": "cc", "messages":
                            [{"role": "user", "content": "write a function"}]})
        assert m == "rr-model"
