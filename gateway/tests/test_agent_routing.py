"""Tests for per-agent LLM round-robin routing."""

from __future__ import annotations

from ostiari_gateway.models import ModulesConfig, SidecarConfig
from ostiari_gateway.modules.llm_gateway.models import AgentRoutingPolicy, LLMConfig
from ostiari_gateway.modules.llm_gateway.router import ModelRouter
from starlette.testclient import TestClient


class TestRoundRobinRouter:
    def test_request_scope_rotates_every_call(self):
        cfg = LLMConfig(default_model="d", agent_routing={
            "claude-code": AgentRoutingPolicy(models=["a", "b", "c"], scope="request")})
        r = ModelRouter(cfg)
        picks = [r.select_model({"agent_id": "claude-code", "messages": []}) for _ in range(7)]
        assert picks == ["a", "b", "c", "a", "b", "c", "a"]

    def test_session_scope_sticky_then_rotates(self):
        cfg = LLMConfig(default_model="d", agent_routing={
            "cc": AgentRoutingPolicy(models=["m1", "m2", "m3"], scope="session")})
        r = ModelRouter(cfg)
        # same session -> same model
        assert r.select_model({"agent_id": "cc", "session_id": "S1"}) == "m1"
        assert r.select_model({"agent_id": "cc", "session_id": "S1"}) == "m1"
        # new session -> next model
        assert r.select_model({"agent_id": "cc", "session_id": "S2"}) == "m2"
        assert r.select_model({"agent_id": "cc", "session_id": "S3"}) == "m3"

    def test_other_agents_unaffected(self):
        cfg = LLMConfig(default_model="default-model", agent_routing={
            "cc": AgentRoutingPolicy(models=["a", "b"])})
        r = ModelRouter(cfg)
        assert r.select_model({"agent_id": "someone-else", "messages": []}) == "default-model"

    def test_wildcard_policy_applies_to_any_agent(self):
        cfg = LLMConfig(default_model="d", agent_routing={
            "*": AgentRoutingPolicy(models=["x", "y"])})
        r = ModelRouter(cfg)
        assert r.select_model({"agent_id": "anyone"}) == "x"
        assert r.select_model({"agent_id": "anyone"}) == "y"

    def test_specific_policy_overrides_wildcard(self):
        cfg = LLMConfig(default_model="d", agent_routing={
            "*": AgentRoutingPolicy(models=["x"]),
            "cc": AgentRoutingPolicy(models=["a", "b"])})
        r = ModelRouter(cfg)
        assert r.select_model({"agent_id": "cc"}) in ("a", "b")
        assert r.select_model({"agent_id": "other"}) == "x"

    def test_single_model_policy_is_pin(self):
        cfg = LLMConfig(default_model="d", agent_routing={
            "cc": AgentRoutingPolicy(models=["only"])})
        r = ModelRouter(cfg)
        assert r.select_model({"agent_id": "cc"}) == "only"
        assert r.select_model({"agent_id": "cc"}) == "only"

    def test_routing_takes_precedence_over_rules(self):
        # even with a matching rule, an agent's round-robin policy wins
        from ostiari_gateway.modules.llm_gateway.models import RoutingRule
        cfg = LLMConfig(default_model="d",
                        routing_rules=[RoutingRule(condition="x == '1'", model="rule-model")],
                        agent_routing={"cc": AgentRoutingPolicy(models=["rr"])})
        r = ModelRouter(cfg)
        assert r.select_model({"agent_id": "cc", "x": "1"}) == "rr"

    def test_empty_models_falls_through(self):
        cfg = LLMConfig(default_model="fallback", agent_routing={
            "cc": AgentRoutingPolicy(models=[])})
        r = ModelRouter(cfg)
        assert r.select_model({"agent_id": "cc"}) == "fallback"


class TestGatewayEndpoint:
    def _client(self):
        from ostiari_gateway.server import create_app
        return TestClient(create_app(initial_config=SidecarConfig(
            sidecar_id="rr-test",
            modules=ModulesConfig(llm_gateway=True),
            llm={"default_model": "claude-sonnet-4-6"})))

    def test_set_and_get_agent_routing(self):
        c = self._client()
        r = c.post("/config/agent-routing", json={"agent_routing": {
            "claude-code": {"strategy": "round_robin", "models": ["claude-sonnet-4-6", "gpt-4o"],
                            "scope": "request"}}})
        assert r.status_code == 200
        got = c.get("/config/agent-routing").json()["agent_routing"]
        assert got["claude-code"]["models"] == ["claude-sonnet-4-6", "gpt-4o"]

    def test_partial_update_preserves_default_model(self):
        c = self._client()
        c.post("/config/agent-routing", json={"agent_routing": {
            "cc": {"models": ["a", "b"]}}})
        # default_model still intact (not wiped by the partial update)
        assert c.get("/models").json()["default_model"] == "claude-sonnet-4-6"

    def test_task_classification_endpoint_updates_live_router(self):
        c = self._client()
        body = {
            "rules": {"coding": ["code", "function"]},
            "model_mapping": {"coding": "gpt-4o"},
        }
        response = c.post("/config/task-classification", json=body)
        assert response.status_code == 200, response.text
        assert c.get("/config/task-classification").json() == body
        module = c.app.state.module_registry.get("llm_gateway")
        module._executor._router._task_classifier = None
        assert module._executor._router.select_model({
            "messages": [{"role": "user", "content": "write code"}],
        }) == "gpt-4o"

    def test_llm_config_get_is_live_and_redacted(self):
        c = self._client()
        response = c.get("/config/llm")
        assert response.status_code == 200
        assert response.json()["default_model"] == "claude-sonnet-4-6"
        assert "credentials" not in response.json()
