"""Tests for correctness bugs B2 (A/B scoping/fall-through), B4 (unawaited axon
coroutine), and B5 (template intent-cache reusing concrete args)."""

from __future__ import annotations

import time

from ostiari_gateway.intent_cache import CachedPlan, IntentCache
from ostiari_gateway.modules.llm_gateway.executor import _reinsert_placeholders
from ostiari_gateway.modules.llm_gateway.models import ABExperiment, LLMConfig, RoutingRule
from ostiari_gateway.modules.llm_gateway.router import ModelRouter


class TestB2ABScoping:
    def test_unscoped_experiment_still_applies_to_all(self):
        cfg = LLMConfig(default_model="d", ab_experiments=[
            ABExperiment(name="e", model_a="A", model_b="B", traffic_pct_b=50)])
        r = ModelRouter(cfg)
        assert r.select_model({"agent_id": "anyone"}) in ("A", "B")

    def test_scoped_experiment_only_affects_listed_agents(self):
        cfg = LLMConfig(default_model="default-m", ab_experiments=[
            ABExperiment(name="e", model_a="A", model_b="B", traffic_pct_b=50,
                         agents=["agentX"])])
        r = ModelRouter(cfg)
        assert r.select_model({"agent_id": "agentX"}) in ("A", "B")
        # out-of-scope agent falls through to default (was hijacked before)
        assert r.select_model({"agent_id": "other"}) == "default-m"

    def test_out_of_scope_falls_through_to_rules(self):
        cfg = LLMConfig(default_model="default-m",
                        routing_rules=[RoutingRule(condition="task_type == 'x'", model="rule-m")],
                        ab_experiments=[ABExperiment(name="e", model_a="A", model_b="B",
                                                     traffic_pct_b=50, agents=["only-me"])])
        r = ModelRouter(cfg)
        assert r.select_model({"agent_id": "other", "task_type": "x"}) == "rule-m"

    def test_second_experiment_reachable_when_first_out_of_scope(self):
        cfg = LLMConfig(default_model="d", ab_experiments=[
            ABExperiment(name="e1", model_a="A1", model_b="B1", traffic_pct_b=100, agents=["a1"]),
            ABExperiment(name="e2", model_a="A2", model_b="B2", traffic_pct_b=100, agents=["a2"]),
        ])
        r = ModelRouter(cfg)
        # a2 is out of e1's scope but should hit e2 (previously e1 returned for everyone)
        assert r.select_model({"agent_id": "a2"}) == "B2"

    def test_consistent_bucketing(self):
        cfg = LLMConfig(default_model="d", ab_experiments=[
            ABExperiment(name="e", model_a="A", model_b="B", traffic_pct_b=50)])
        r = ModelRouter(cfg)
        picks = {r.select_model({"agent_id": "stable-agent"}) for _ in range(5)}
        assert len(picks) == 1  # same agent always same bucket


class TestB5TemplateCache:
    def test_reinsert_placeholders_roundtrip(self):
        plan = [{"name": "send_email", "arguments": {"to": "alice@x.com", "subject": "hi"}}]
        templated = _reinsert_placeholders(plan, {"recipient": "alice@x.com"})
        assert templated[0]["arguments"]["to"] == "{recipient}"
        assert templated[0]["arguments"]["subject"] == "hi"  # untouched

    def test_template_resolves_to_new_value_not_stale(self):
        # This is the B5 scenario: first call cached alice, second call must email bob
        plan = _reinsert_placeholders(
            [{"name": "send_email", "arguments": {"to": "alice@x.com"}}],
            {"recipient": "alice@x.com"})
        cp = CachedPlan(tool_calls=plan, model_used="m", created_at=time.monotonic(),
                        ttl_seconds=300, is_template=True)
        resolved = cp.resolve_with_variables({"recipient": "bob@y.com"})
        assert resolved[0]["arguments"]["to"] == "bob@y.com"   # NOT alice

    def test_longer_values_substituted_first(self):
        # a value that is a substring of another must not be partially clobbered
        plan = _reinsert_placeholders(
            [{"name": "t", "arguments": {"a": "foo", "b": "foobar"}}],
            {"x": "foo", "y": "foobar"})
        # "foobar" (longer) replaced first → b becomes {y}, a becomes {x}
        assert plan[0]["arguments"]["b"] == "{y}"
        assert plan[0]["arguments"]["a"] == "{x}"


class TestIntentCacheInvalidation:
    def test_invalidate_session_actually_removes(self):
        c = IntentCache()
        c.put("a", "s1", "intent one", [{"name": "t", "arguments": {}}], "m")
        c.put("a", "s1", "intent two", [{"name": "t", "arguments": {}}], "m")
        assert c.get("a", "s1", "intent one") is not None
        removed = c.invalidate_session("s1", "a")
        assert removed == 2
        assert c.get("a", "s1", "intent one") is None

    def test_invalidate_scoped_to_agent(self):
        c = IntentCache()
        c.put("a", "s1", "x", [{"name": "t", "arguments": {}}], "m")
        c.put("b", "s1", "x", [{"name": "t", "arguments": {}}], "m")
        c.invalidate_session("s1", "a")
        assert c.get("a", "s1", "x") is None
        assert c.get("b", "s1", "x") is not None   # other agent untouched
